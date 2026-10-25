"""Sequential bootstrap of the overnight (€STR) discount curve from a market snapshot.

Phase 1 builds a single curve: the OIS curve both discounts and projects the overnight index.
Instruments are sorted by pillar date; for each pillar ``i`` the solver finds the discount
factor ``P_i`` such that the instrument's implied quote equals its market quote, nodes
``P_1 .. P_{i-1}`` being already known. With a local interpolation (log-linear on DF) the
instrument only depends on nodes up to its own pillar, so one pass is exact.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date

from rate_derivatives_pricer.core.conventions.currency import get_conventions
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

    @property
    def options(self) -> CurveBuildOptions:
        """Build options."""
        return self._options

    def build(self, snapshot: MarketSnapshot) -> MarketEnvironment:
        """Bootstrap the curves of ``snapshot``.

        Raises:
            ConventionError: If the snapshot currency has no conventions.
            MarketDataError: If quotes are inconsistent (e.g. two quotes on the same pillar).
            CalibrationError: If a pillar cannot be calibrated.
        """
        started = time.perf_counter()
        conventions = get_conventions(snapshot.currency)
        index = conventions.overnight_index
        valuation_date = snapshot.valuation_date
        fixings = snapshot.fixings_by_index()
        curve_name = f"{conventions.currency}-{index.name}"

        instruments = sorted(
            (calibration_instrument_for(q, conventions, valuation_date) for q in snapshot.quotes),
            key=lambda instrument: instrument.pillar_date,
        )
        _check_pillars(instruments, valuation_date, snapshot.name)

        template = DiscountCurve(
            curve_reference_date=valuation_date,
            pillar_dates=(),
            pillar_discount_factors=(),
            interpolator=self._interpolator,
            day_count=conventions.curve_day_count,
            name=curve_name,
        )

        def environment(curve: DiscountCurve) -> MarketEnvironment:
            # Mono-curve: the OIS curve discounts and projects the overnight index.
            return MarketEnvironment(
                valuation_date=valuation_date,
                currency=conventions.currency,
                discount_curve=curve,
                projection_curves={index.name: curve},
                fixings=fixings,
            )

        pillar_dates: list[date] = []
        discount_factors: list[float] = []
        reports: list[PillarReport] = []
        for instrument in instruments:
            curve, report = self._calibrate_pillar(
                instrument, template, pillar_dates, discount_factors, environment
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
        return replace(environment(template), calibration=(calibration,))

    def _calibrate_pillar(
        self,
        instrument: CalibrationInstrument,
        template: DiscountCurve,
        pillar_dates: list[date],
        discount_factors: list[float],
        environment: _EnvironmentFactory,
    ) -> tuple[DiscountCurve, PillarReport]:
        """Solve the discount factor of one pillar given the previous nodes."""
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
        report = PillarReport(
            label=instrument.label,
            instrument_type=instrument.quote.instrument_type,
            pillar_date=pillar,
            market_quote=market_quote,
            implied_quote=market_quote + result.residual,
            residual=result.residual,
            iterations=result.iterations,
            discount_factor=result.root,
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
        return candidate(result.root), report


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
