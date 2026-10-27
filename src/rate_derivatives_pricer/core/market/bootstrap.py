"""Sequential multi-curve bootstrap from a market snapshot.

Two stages, as in the market:

1. the overnight (€STR) curve from deposit and OIS quotes. It discounts every cashflow and
   projects the overnight index;
2. one projection curve per Ibor index (Euribor 3M, 6M) from its deposit and vanilla swap
   quotes, with all cashflows discounted on the stage-1 curve.

Within a curve, instruments are sorted by pillar date; for each pillar ``i`` the solver finds the
discount factor ``P_i`` such that the instrument's implied quote equals its market quote, nodes
``P_1 .. P_{i-1}`` being already known. With a local interpolation (log-linear on DF) the
instrument only depends on nodes up to its own pillar, so one pass is exact.
"""

from __future__ import annotations

import logging
import math
import time
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from rate_derivatives_pricer.core.conventions.currency import (
    CurrencyConventions,
    get_conventions,
)
from rate_derivatives_pricer.core.errors import CalibrationError, ConventionError, MarketDataError
from rate_derivatives_pricer.core.market.calibration import (
    CalibrationInstrument,
    calibration_instrument_for,
)
from rate_derivatives_pricer.core.market.curves import DiscountCurve
from rate_derivatives_pricer.core.market.environment import (
    CalibrationReport,
    MarketEnvironment,
    PillarReport,
)
from rate_derivatives_pricer.core.market.quotes import IborQuote, Quote
from rate_derivatives_pricer.core.market.snapshot import MarketSnapshot
from rate_derivatives_pricer.core.market.solvers import SOLVERS, Solver
from rate_derivatives_pricer.core.registry import INTERPOLATORS

logger = logging.getLogger(__name__)

# Builds the environment in which a calibration instrument is valued, from a candidate curve.
type _EnvironmentFactory = Callable[[DiscountCurve], MarketEnvironment]

DEFAULT_INTERPOLATION = "log_linear_df"
DEFAULT_SOLVER = "brent"
# Search bracket for each new node, as continuously compounded forward rates from the previous
# node: P_i in [P_{i-1} exp(-MAX dt), P_{i-1} exp(-MIN dt)]. Wide enough for any real market.
BRACKET_MIN_FORWARD_RATE = -0.5
BRACKET_MAX_FORWARD_RATE = 1.0
# A pillar whose final residual exceeds this (decimal rate) is a calibration failure.
CALIBRATION_RESIDUAL_TOLERANCE = 1e-12
MILLISECONDS_PER_SECOND = 1000.0
# Upper bound on the solved pillars a builder remembers (a risk run on a 30Y trade needs ~10^4).
MAX_MEMOISED_PILLARS = 50_000


@dataclass(frozen=True, slots=True)
class CurveBuildOptions:
    """Options of the curve construction.

    Attributes:
        interpolation: Interpolator registry key (``"log_linear_df"``, ``"linear_zero"``).
        solver: Root finder name (``"brent"``, ``"newton"``).
    """

    interpolation: str = DEFAULT_INTERPOLATION
    solver: str = DEFAULT_SOLVER


class CurveBuilder:
    """Builds a :class:`MarketEnvironment` from a :class:`MarketSnapshot`.

    A builder remembers every pillar it has solved. A pillar solution is a pure function of the
    quote, the nodes already solved on the same curve and the discount nodes the instrument
    touches, and the memo key holds exactly those inputs, so a hit returns the very same number
    the solver would. Rebuilding a bumped snapshot with the same builder therefore only re-solves
    the pillars the bump can change (the others are bit-identical), which is what makes
    bump-and-reprice affordable.

    Args:
        options: Interpolation and solver choices.

    Raises:
        ConventionError: If the interpolation or the solver is unknown.
    """

    def __init__(self, options: CurveBuildOptions | None = None) -> None:
        self._options = options or CurveBuildOptions()
        self._interpolator = INTERPOLATORS.get(self._options.interpolation)()
        try:
            self._solver: Solver = SOLVERS[self._options.solver]()
        except KeyError:
            raise ConventionError(
                f"unknown solver: {self._options.solver!r}",
                {"solver": self._options.solver, "available": sorted(SOLVERS)},
            ) from None
        # Solved pillars: key -> (discount factor, residual, solver iterations).
        self._pillar_memo: dict[tuple[object, ...], tuple[float, float, int]] = {}
        # Calibration instruments (schedules) by identity: they do not depend on the quoted rate.
        self._instrument_memo: dict[tuple[object, ...], CalibrationInstrument] = {}

    @property
    def options(self) -> CurveBuildOptions:
        """Build options."""
        return self._options

    def build(self, snapshot: MarketSnapshot, horizon: date | None = None) -> MarketEnvironment:
        """Bootstrap the curves of ``snapshot``.

        The environment discounts on the overnight curve and projects each index on its own
        curve; ``calibration`` lists the overnight report first, then one per Ibor index.

        Args:
            snapshot: Market data to calibrate on.
            horizon: Only for repricing a trade whose cashflows all fall on or before this date
                (risk runs). Each curve then stops at its first pillar on or after ``horizon``
                instead of calibrating the whole quote set; discount factors are exact up to
                ``horizon`` and **must not be used beyond it**. ``None`` builds the full curves.

        Raises:
            ConventionError: If the snapshot currency has no conventions, or an index is unknown.
            MarketDataError: If quotes are inconsistent (e.g. two quotes on the same pillar).
            CalibrationError: If a pillar cannot be calibrated.
        """
        started = time.perf_counter()
        conventions = get_conventions(snapshot.currency)
        index = conventions.overnight_index
        valuation_date = snapshot.valuation_date
        fixings = snapshot.fixings_by_index()

        overnight_quotes = [q for q in snapshot.quotes if not isinstance(q, IborQuote)]
        ibor_quotes: dict[str, list[Quote]] = {}
        for quote in snapshot.quotes:
            if isinstance(quote, IborQuote):
                ibor_quotes.setdefault(quote.index_name, []).append(quote)
        if not overnight_quotes:
            raise MarketDataError(
                "snapshot has no overnight (deposit or OIS) quote", {"snapshot": snapshot.name}
            )

        overnight_instruments = self._instruments(snapshot, conventions, overnight_quotes)
        ibor_instruments = {
            name: self._instruments(snapshot, conventions, quotes)
            for name, quotes in sorted(ibor_quotes.items())
        }
        if horizon is not None:
            ibor_instruments = {
                name: _until(instruments, horizon) for name, instruments in ibor_instruments.items()
            }
            # Ibor pillars are discounted on the overnight curve: it must reach as far as they do.
            overnight_horizon = max(
                [
                    horizon,
                    *(instruments[-1].pillar_date for instruments in ibor_instruments.values()),
                ]
            )
            overnight_instruments = _until(overnight_instruments, overnight_horizon)

        # Calibration instruments are valued without fixings: a quote defines a relation between
        # curves only (a spot-starting swap whose first Ibor fixing is today's would otherwise see
        # the published fixing instead of the curve).
        def overnight_environment(curve: DiscountCurve) -> MarketEnvironment:
            return MarketEnvironment(
                valuation_date=valuation_date,
                currency=conventions.currency,
                discount_curve=curve,
                projection_curves={index.name: curve},
            )

        # Stage 1: overnight curve. It discounts and projects the overnight index.
        overnight_curve, overnight_report = self._bootstrap_curve(
            snapshot,
            conventions,
            overnight_instruments,
            f"{conventions.currency}-{index.name}",
            index.name,
            overnight_environment,
            is_discount_curve=True,
        )
        reports = [overnight_report]
        projection_curves = {index.name: overnight_curve}

        # Stage 2: one projection curve per Ibor index, discounted on the overnight curve. Ibor
        # curves do not depend on each other, so the order only fixes the report order.
        for index_name, instruments in ibor_instruments.items():
            ibor_index = conventions.ibor_index(index_name)

            def ibor_environment(
                curve: DiscountCurve, key: str = ibor_index.name
            ) -> MarketEnvironment:
                return MarketEnvironment(
                    valuation_date=valuation_date,
                    currency=conventions.currency,
                    discount_curve=overnight_curve,
                    projection_curves={**projection_curves, key: curve},
                )

            curve, report = self._bootstrap_curve(
                snapshot,
                conventions,
                instruments,
                f"{conventions.currency}-{ibor_index.name}",
                ibor_index.name,
                ibor_environment,
                discount_curve=overnight_curve,
            )
            projection_curves[ibor_index.name] = curve
            reports.append(report)

        logger.info(
            "curves_build_completed",
            extra={
                "snapshot_name": snapshot.name,
                "currency": conventions.currency,
                "curve_count": len(reports),
                "max_abs_residual": max(report.max_abs_residual for report in reports),
                "duration_ms": (time.perf_counter() - started) * MILLISECONDS_PER_SECOND,
            },
        )
        return MarketEnvironment(
            valuation_date=valuation_date,
            currency=conventions.currency,
            discount_curve=overnight_curve,
            projection_curves=projection_curves,
            fixings=fixings,
            calibration=tuple(reports),
        )

    def _instrument(
        self, snapshot: MarketSnapshot, conventions: CurrencyConventions, quote: Quote
    ) -> CalibrationInstrument:
        """Calibration instrument of ``quote``, its schedules built once per builder."""
        key = (snapshot.currency, snapshot.valuation_date, quote.key)
        instrument = self._instrument_memo.get(key)
        if instrument is None:
            instrument = calibration_instrument_for(quote, conventions, snapshot.valuation_date)
            self._instrument_memo[key] = instrument
            return instrument
        return instrument.with_quote(quote)

    def _instruments(
        self, snapshot: MarketSnapshot, conventions: CurrencyConventions, quotes: list[Quote]
    ) -> list[CalibrationInstrument]:
        """Calibration instruments of ``quotes`` by increasing pillar date (pillars checked)."""
        instruments = sorted(
            (self._instrument(snapshot, conventions, q) for q in quotes),
            key=lambda instrument: instrument.pillar_date,
        )
        _check_pillars(instruments, snapshot.valuation_date, snapshot.name)
        return instruments

    def _bootstrap_curve(
        self,
        snapshot: MarketSnapshot,
        conventions: CurrencyConventions,
        instruments: list[CalibrationInstrument],
        curve_name: str,
        index_name: str,
        environment: _EnvironmentFactory,
        is_discount_curve: bool = False,
        discount_curve: DiscountCurve | None = None,
    ) -> tuple[DiscountCurve, CalibrationReport]:
        """Bootstrap one curve from its calibration instruments, pillar by pillar.

        Args:
            snapshot: Market data (valuation date, name for error reporting).
            conventions: Currency conventions (curve time measure).
            instruments: Instruments calibrating this curve, by increasing pillar date.
            curve_name: Curve identifier (``"EUR-ESTR"``).
            index_name: Key of the curve in the environment's projection curves.
            environment: Builds the environment in which instruments are valued from a
                candidate version of this curve (other curves are already fixed).
            is_discount_curve: Whether this curve discounts cashflows.
            discount_curve: Already calibrated curve the instruments are discounted on, when it
                is not the curve being built.
        """
        started = time.perf_counter()
        valuation_date = snapshot.valuation_date

        template = DiscountCurve(
            curve_reference_date=valuation_date,
            pillar_dates=(),
            pillar_discount_factors=(),
            interpolator=self._interpolator,
            day_count=conventions.curve_day_count,
            name=curve_name,
        )
        pillar_dates: list[date] = []
        discount_factors: list[float] = []
        reports: list[PillarReport] = []
        for instrument in instruments:
            curve, report = self._calibrate_pillar(
                instrument, template, pillar_dates, discount_factors, environment, discount_curve
            )
            pillar_dates.append(instrument.pillar_date)
            discount_factors.append(report.discount_factor)
            reports.append(report)
            template = curve

        calibration = CalibrationReport(
            curve_name=curve_name,
            interpolation=self._interpolator.name,
            solver=self._solver.name,
            pillars=tuple(reports),
            index_name=index_name,
            is_discount_curve=is_discount_curve,
        )
        logger.info(
            "curve_build_completed",
            extra={
                "curve": curve_name,
                "snapshot_name": snapshot.name,
                "currency": conventions.currency,
                "interpolation": calibration.interpolation,
                "solver": calibration.solver,
                "pillar_count": len(reports),
                "max_iterations": calibration.max_iterations,
                "max_abs_residual": calibration.max_abs_residual,
                "duration_ms": (time.perf_counter() - started) * MILLISECONDS_PER_SECOND,
            },
        )
        return template, calibration

    def _calibrate_pillar(
        self,
        instrument: CalibrationInstrument,
        template: DiscountCurve,
        pillar_dates: list[date],
        discount_factors: list[float],
        environment: _EnvironmentFactory,
        discount_curve: DiscountCurve | None,
    ) -> tuple[DiscountCurve, PillarReport]:
        """Solve the discount factor of one pillar given the previous nodes (or recall it)."""
        market_quote = instrument.quote.rate
        pillar = instrument.pillar_date
        previous_df = discount_factors[-1] if discount_factors else 1.0
        previous_time = template.time(pillar_dates[-1]) if pillar_dates else 0.0
        dt = template.time(pillar) - previous_time
        dates = (*pillar_dates, pillar)

        def candidate(discount_factor: float) -> DiscountCurve:
            return template.with_pillars(dates, (*discount_factors, discount_factor))

        def objective(discount_factor: float) -> float:
            return instrument.implied_quote(environment(candidate(discount_factor))) - market_quote

        # Everything the solution depends on: the instrument and its quote, the nodes already
        # solved on this curve and, for an instrument discounted on another curve, the nodes of
        # that curve up to the pillar (plus the one bracketing it, as interpolation is local).
        memo_key = (
            template.name,
            template.curve_reference_date,
            instrument.quote.key,
            pillar,
            market_quote,
            tuple(pillar_dates),
            tuple(discount_factors),
            self._discount_signature(discount_curve, pillar),
        )
        remembered = self._pillar_memo.get(memo_key)
        if remembered is not None:
            root, residual, iterations = remembered
        else:
            result = self._solver.solve(
                objective,
                lower=previous_df * math.exp(-BRACKET_MAX_FORWARD_RATE * dt),
                upper=previous_df * math.exp(-BRACKET_MIN_FORWARD_RATE * dt),
                initial_guess=previous_df * math.exp(-market_quote * dt),
            )
            if not result.converged or not abs(result.residual) <= CALIBRATION_RESIDUAL_TOLERANCE:
                raise CalibrationError(
                    f"bootstrap failed on pillar {instrument.label}",
                    pillar=instrument.label,
                    residual=result.residual,
                )
            root, residual, iterations = result.root, result.residual, result.iterations
            if len(self._pillar_memo) >= MAX_MEMOISED_PILLARS:
                self._pillar_memo.clear()
            self._pillar_memo[memo_key] = (root, residual, iterations)
        report = PillarReport(
            label=instrument.label,
            instrument_type=instrument.quote.instrument_type,
            pillar_date=pillar,
            market_quote=market_quote,
            implied_quote=market_quote + residual,
            residual=residual,
            iterations=iterations,
            discount_factor=root,
        )
        logger.debug(
            "curve_pillar_calibrated",
            extra={
                "curve": template.name,
                "pillar": report.label,
                "instrument_type": report.instrument_type,
                "pillar_date": pillar.isoformat(),
                "market_quote": market_quote,
                "iterations": report.iterations,
                "residual": report.residual,
                "discount_factor": report.discount_factor,
            },
        )
        return candidate(root), report

    def _discount_signature(
        self, discount_curve: DiscountCurve | None, up_to: date
    ) -> tuple[object, ...]:
        """Nodes of ``discount_curve`` an instrument ending on ``up_to`` can depend on."""
        if discount_curve is None:
            return ()
        dates, factors = discount_curve.pillar_dates, discount_curve.pillar_discount_factors
        if not self._interpolator.local:
            return (dates, factors)  # a non-local scheme: every node can matter
        count = bisect_right(dates, up_to) + 1  # nodes through the pillar, plus the next one
        return (dates[:count], factors[:count])


def _until(instruments: list[CalibrationInstrument], horizon: date) -> list[CalibrationInstrument]:
    """Instruments up to and including the first whose pillar is on or after ``horizon``."""
    for position, instrument in enumerate(instruments):
        if instrument.pillar_date >= horizon:
            return instruments[: position + 1]
    return instruments


def _check_pillars(
    instruments: list[CalibrationInstrument], valuation_date: date, snapshot_name: str
) -> None:
    """Pillars must be after the valuation date and pairwise distinct."""
    previous: CalibrationInstrument | None = None
    for instrument in instruments:
        if instrument.pillar_date <= valuation_date:  # pragma: no cover - defensive
            raise MarketDataError(
                "calibration pillar on or before the valuation date",
                {"snapshot": snapshot_name, "pillar": instrument.label},
            )
        if previous is not None and previous.pillar_date == instrument.pillar_date:
            raise MarketDataError(
                "two calibration instruments share the same pillar date",
                {
                    "snapshot": snapshot_name,
                    "pillars": [previous.label, instrument.label],
                    "pillar_date": instrument.pillar_date.isoformat(),
                },
            )
        previous = instrument
