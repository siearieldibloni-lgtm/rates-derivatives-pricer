"""Carry and roll-down under the *unchanged curve* hypothesis (ADR-034).

Hypothesis: over the horizon ``[t, t + H]`` the curves are unchanged **in tenor space**: every
calibration quote (par rate by tenor) keeps its value, and the curves are re-bootstrapped at the
horizon date. Consistently, the index fixings of ``[t, t + H)`` that are not published yet are
today's quoted rates: the overnight deposit rate for the overnight index (€STR, SOFR), the index
deposit rate for an Ibor index (Euribor 6M).

With ``PV_t`` the PV today and ``PV_{t+H}`` the PV at the horizon on the unchanged curve::

    total     = PV_{t+H} + cashflows paid in (t, t+H] (undiscounted) - PV_t
    carry     = net interest accrued over [t, t+H) (fixed vs floating, at the fixings above)
    roll-down = total - carry

Carry is the income of the position (it only depends on rates already set or assumed); roll-down
is the change in clean value from ageing the trade along an unchanged curve. The alternative
"forwards realised" hypothesis (the horizon curve is today's forward curve) was rejected: it
leaves almost nothing but funding to explain.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from rate_derivatives_pricer.core.conventions.business_day import BusinessDayConvention
from rate_derivatives_pricer.core.conventions.currency import get_conventions
from rate_derivatives_pricer.core.conventions.tenor import Tenor
from rate_derivatives_pricer.core.errors import (
    ConventionError,
    MarketDataError,
    RatesPricerError,
    RiskComputationError,
)
from rate_derivatives_pricer.core.instruments.swaps import Instrument
from rate_derivatives_pricer.core.market.quotes import DepositQuote, IborDepositQuote
from rate_derivatives_pricer.core.market.snapshot import Fixing, MarketSnapshot

logger = logging.getLogger(__name__)

DEFAULT_HORIZON = "3M"
MILLISECONDS_PER_SECOND = 1000.0


class HorizonValuer(Protocol):
    """Valuations carry/roll-down needs; implemented by the caller (bootstrap + pricing engine).

    Like the risk measures' pricer, it keeps ``risk/`` independent of the pricing engines.
    """

    def present_value(self, instrument: Instrument, snapshot: MarketSnapshot) -> float:
        """PV of ``instrument`` on ``snapshot`` (as of its valuation date)."""

    def cashflows_paid(
        self, instrument: Instrument, snapshot: MarketSnapshot, start: date, end: date
    ) -> float:
        """Signed undiscounted amounts paid in ``(start, end]``, evaluated on ``snapshot``."""

    def accrued_interest(
        self, instrument: Instrument, snapshot: MarketSnapshot, start: date, end: date
    ) -> float:
        """Signed net interest accrued over ``[start, end)``, evaluated on ``snapshot``."""


@dataclass(frozen=True, slots=True)
class CarryRolldownResult:
    """Carry and roll-down of one instrument over a horizon (currency of the trade).

    Attributes:
        instrument_type: Instrument type of the trade.
        horizon: Horizon tenor (``"3M"``).
        valuation_date: Today ``t``.
        horizon_date: ``t + H``, adjusted to a business day (following).
        base_pv: ``PV_t``.
        horizon_pv: ``PV_{t+H}`` on the unchanged curve.
        cashflows: Signed cashflows paid in ``(t, t+H]``, undiscounted.
        total: ``horizon_pv + cashflows - base_pv``.
        carry: Net interest accrued over ``[t, t+H)``.
        rolldown: ``total - carry``.
        assumed_fixings: Rate assumed for each index over the horizon (decimal).
        duration_ms: Wall-clock time.
    """

    instrument_type: str
    horizon: str
    valuation_date: date
    horizon_date: date
    base_pv: float
    horizon_pv: float
    cashflows: float
    total: float
    carry: float
    rolldown: float
    assumed_fixings: dict[str, float]
    duration_ms: float


def horizon_date(snapshot: MarketSnapshot, horizon: Tenor) -> date:
    """``t + H`` on the currency calendar, adjusted following.

    Raises:
        ConventionError: If the horizon is not positive.
    """
    if horizon.length <= 0:
        raise ConventionError("carry horizon must be positive", {"horizon": str(horizon)})
    calendar = get_conventions(snapshot.currency).calendar
    return calendar.advance(snapshot.valuation_date, horizon, BusinessDayConvention.FOLLOWING)


def unchanged_curve_snapshot(
    snapshot: MarketSnapshot, horizon_day: date
) -> tuple[MarketSnapshot, dict[str, float]]:
    """Market at ``horizon_day`` under the unchanged curve hypothesis.

    Same quotes (par rates by tenor), valuation date moved to ``horizon_day``, and the fixings of
    every business day of ``[t, horizon_day)`` not yet published set to today's quoted rate of
    their index (overnight deposit, or the Ibor index deposit). An Ibor index without a deposit
    quote gets no assumed fixing: a trade needing one then fails with a missing fixing.

    Returns:
        The horizon snapshot and the assumed rate of each index (decimal).

    Raises:
        MarketDataError: If the snapshot has no overnight deposit quote.
    """
    conventions = get_conventions(snapshot.currency)
    overnight = conventions.overnight_index
    deposit = next((q for q in snapshot.quotes if isinstance(q, DepositQuote)), None)
    if deposit is None:
        raise MarketDataError(
            "carry needs the overnight deposit quote (assumed overnight fixing)",
            {"snapshot": snapshot.name, "index": overnight.name},
        )
    # Index -> (fixing calendar, assumed rate).
    assumptions = {overnight.name: (overnight.fixing_calendar, deposit.rate)}
    for quote in snapshot.quotes:
        if isinstance(quote, IborDepositQuote):
            index = conventions.ibor_index(quote.index_name)
            assumptions[index.name] = (index.fixing_calendar, quote.rate)

    known = {(fixing.index, fixing.fixing_date) for fixing in snapshot.fixings}
    added: list[Fixing] = []
    for index_name, (calendar, rate) in assumptions.items():
        for day in calendar.business_days(snapshot.valuation_date, horizon_day):
            if day < horizon_day and (index_name, day) not in known:
                added.append(Fixing(index=index_name, fixing_date=day, rate=rate))
    horizon_snapshot = MarketSnapshot(
        name=f"{snapshot.name}@{horizon_day.isoformat()}",
        currency=snapshot.currency,
        valuation_date=horizon_day,
        quotes=snapshot.quotes,
        fixings=tuple(sorted((*snapshot.fixings, *added), key=lambda f: (f.index, f.fixing_date))),
        description=f"{snapshot.description} [unchanged curve at {horizon_day.isoformat()}]",
    )
    return horizon_snapshot, {name: rate for name, (_, rate) in assumptions.items()}


class CarryRolldown:
    """Carry and roll-down of a trade over a horizon, under the unchanged curve hypothesis.

    Args:
        valuer: Valuations on a snapshot (PV, cashflows paid, accrued interest).
    """

    def __init__(self, valuer: HorizonValuer) -> None:
        self._valuer = valuer

    def compute(
        self, instrument: Instrument, snapshot: MarketSnapshot, horizon: Tenor
    ) -> CarryRolldownResult:
        """Decompose the P&L of ``instrument`` over ``horizon`` into carry and roll-down.

        Raises:
            ConventionError: If the horizon is not positive.
            MarketDataError: If the base market cannot value the trade (not wrapped).
            RiskComputationError: If the horizon market cannot be calibrated or priced.
        """
        started = time.perf_counter()
        today = snapshot.valuation_date
        end = horizon_date(snapshot, horizon)
        base_pv = self._valuer.present_value(instrument, snapshot)
        horizon_snapshot, assumed = unchanged_curve_snapshot(snapshot, end)
        try:
            horizon_pv = self._valuer.present_value(instrument, horizon_snapshot)
            cashflows = self._valuer.cashflows_paid(instrument, horizon_snapshot, today, end)
            carry = self._valuer.accrued_interest(instrument, horizon_snapshot, today, end)
        except RatesPricerError as error:
            raise RiskComputationError(
                f"carry/roll-down failed at the horizon: {error.message}",
                {"horizon": str(horizon), "cause": type(error).__name__, **error.details},
            ) from error
        total = horizon_pv + cashflows - base_pv
        duration_ms = (time.perf_counter() - started) * MILLISECONDS_PER_SECOND
        result = CarryRolldownResult(
            instrument_type=instrument.instrument_type,
            horizon=str(horizon),
            valuation_date=today,
            horizon_date=end,
            base_pv=base_pv,
            horizon_pv=horizon_pv,
            cashflows=cashflows,
            total=total,
            carry=carry,
            rolldown=total - carry,
            assumed_fixings=assumed,
            duration_ms=duration_ms,
        )
        logger.info(
            "carry_rolldown_completed",
            extra={
                "trade_type": instrument.instrument_type,
                "horizon": result.horizon,
                "horizon_date": end.isoformat(),
                "total": total,
                "carry": carry,
                "rolldown": result.rolldown,
                "duration_ms": duration_ms,
            },
        )
        return result
