"""Market scenarios and scenario P&L: full revaluation under named curve shocks (ADR-033).

A :class:`Scenario` shifts the *par rates* of the calibration quotes, like the risk bumps
(ADR-029), but by an amount that depends on the quote tenor: the shift is linear in the tenor
(measured in years) between the scenario points and flat beyond the first and the last point. A
single point is therefore a parallel shift. By default every curve is shocked (OIS and Ibor
indices alike); a scenario can be restricted to some curves.

Preset scenarios (parallel +-100bp, steepener, flattener, twist) are configuration, read from
``core/risk/data/scenarios.json`` into the ``SCENARIOS`` registry. :class:`ScenarioPnL` reprices a
trade under each scenario through an abstract pricer, as every risk measure does.
"""

from __future__ import annotations

import itertools
import json
import logging
import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from typing import Any

import numpy as np

from rate_derivatives_pricer.core.conventions.tenor import Tenor, TimeUnit
from rate_derivatives_pricer.core.errors import (
    ConventionError,
    RatesPricerError,
    RiskComputationError,
    UnsupportedScenarioError,
)
from rate_derivatives_pricer.core.instruments.swaps import Instrument
from rate_derivatives_pricer.core.market.quotes import Quote
from rate_derivatives_pricer.core.market.snapshot import MarketSnapshot
from rate_derivatives_pricer.core.registry import SCENARIOS
from rate_derivatives_pricer.core.risk.bumps import BASIS_POINT
from rate_derivatives_pricer.core.risk.measures import Pricer

logger = logging.getLogger(__name__)

SCENARIOS_DATA_PACKAGE = "rate_derivatives_pricer.core.risk.data"
SCENARIOS_FILE = "scenarios.json"
MILLISECONDS_PER_SECOND = 1000.0
# Abscissa of the scenario shapes only (never used to price): a tenor measured in years.
DAYS_PER_YEAR = 365.0
DAYS_PER_WEEK = 7
MONTHS_PER_YEAR = 12


def tenor_in_years(tenor: Tenor) -> float:
    """Length of ``tenor`` in years: ``nD / 365``, ``7n / 365``, ``n / 12`` or ``n``.

    Only the abscissa of scenario shapes: it orders tenors and interpolates shifts between the
    scenario points (``"ON"`` is one day, ``"18M"`` is 1.5 years).
    """
    match tenor.unit:
        case TimeUnit.DAYS:
            return tenor.length / DAYS_PER_YEAR
        case TimeUnit.WEEKS:
            return DAYS_PER_WEEK * tenor.length / DAYS_PER_YEAR
        case TimeUnit.MONTHS:
            return tenor.length / MONTHS_PER_YEAR
        case TimeUnit.YEARS:
            return float(tenor.length)


@dataclass(frozen=True, slots=True)
class ScenarioPoint:
    """Shift of the par rates at one tenor of a scenario shape.

    Attributes:
        tenor_label: Tenor of the point (``"2Y"``).
        shift_bp: Additive shift of the par rate at this tenor, in basis points.
    """

    tenor_label: str
    shift_bp: float

    @property
    def years(self) -> float:
        """Tenor of the point in years (abscissa of the shape)."""
        return tenor_in_years(Tenor.parse(self.tenor_label))


@dataclass(frozen=True, slots=True)
class Scenario:
    """A named market shock: a piecewise-linear shift of the par rates by tenor.

    Attributes:
        name: Identifier (``"steepener"``), unique within a request.
        points: Shape of the shift, by strictly increasing tenor (at least one point).
        description: Free text for reports.
        curves: Curve labels shocked (``"OIS"``, ``"EURIBOR6M"``); ``None`` shocks every curve.

    Raises:
        UnsupportedScenarioError: If the scenario has no point, a malformed tenor, two points on
            the same tenor or a non-finite shift.
    """

    name: str
    points: tuple[ScenarioPoint, ...]
    description: str = ""
    curves: frozenset[str] | None = None

    def __post_init__(self) -> None:
        details: dict[str, Any] = {"scenario": self.name}
        if not self.name.strip():
            raise UnsupportedScenarioError("scenario needs a name", details)
        if not self.points:
            raise UnsupportedScenarioError("scenario needs at least one point", details)
        try:
            years = [point.years for point in self.points]
        except ConventionError as error:
            raise UnsupportedScenarioError(
                f"invalid scenario tenor: {error.message}", {**details, **error.details}
            ) from None
        if any(not math.isfinite(point.shift_bp) for point in self.points):
            raise UnsupportedScenarioError("scenario shifts must be finite", details)
        # Points are kept sorted by tenor; two points on the same tenor would be ambiguous.
        order = sorted(range(len(self.points)), key=years.__getitem__)
        ordered_years = [years[k] for k in order]
        if any(b <= a for a, b in itertools.pairwise(ordered_years)):
            raise UnsupportedScenarioError("scenario points must have distinct tenors", details)
        object.__setattr__(self, "points", tuple(self.points[k] for k in order))
        if self.curves is not None:
            object.__setattr__(
                self, "curves", frozenset(curve.strip().upper() for curve in self.curves)
            )

    def shift_bp(self, tenor: Tenor) -> float:
        """Shift of the par rate quoted at ``tenor``, in basis points.

        Linear interpolation in tenor years between the points, flat extrapolation beyond the
        first and the last point.
        """
        xs = np.asarray([point.years for point in self.points], dtype=np.float64)
        ys = np.asarray([point.shift_bp for point in self.points], dtype=np.float64)
        return float(np.interp(tenor_in_years(tenor), xs, ys))

    def applies_to(self, quote: Quote) -> bool:
        """Whether the scenario shocks ``quote`` (its curve is selected)."""
        return self.curves is None or quote.curve_label in self.curves

    def apply(self, snapshot: MarketSnapshot) -> MarketSnapshot:
        """New snapshot whose selected quotes are shifted (``snapshot`` is left untouched).

        Raises:
            UnsupportedScenarioError: If the scenario selects no quote of the snapshot.
        """
        quotes: list[Quote] = []
        selected = 0
        for quote in snapshot.quotes:
            if self.applies_to(quote):
                shift = self.shift_bp(quote.tenor) * BASIS_POINT
                quotes.append(quote.with_rate(quote.rate + shift))
                selected += 1
            else:
                quotes.append(quote)
        if selected == 0:
            raise UnsupportedScenarioError(
                "scenario selects no quote of the market",
                {
                    "scenario": self.name,
                    "curves": sorted(self.curves or ()),
                    "available": sorted({quote.curve_label for quote in snapshot.quotes}),
                },
            )
        return snapshot.with_quotes(tuple(quotes))

    def quote_shifts(self, snapshot: MarketSnapshot) -> tuple[QuoteShift, ...]:
        """Shift applied to each quote of ``snapshot`` (0 for a curve the scenario skips)."""
        return tuple(
            QuoteShift(
                curve=quote.curve_label,
                label=quote.tenor_label,
                instrument_type=quote.instrument_type,
                shift_bp=self.shift_bp(quote.tenor) if self.applies_to(quote) else 0.0,
            )
            for quote in snapshot.quotes
        )


@dataclass(frozen=True, slots=True)
class QuoteShift:
    """Shift a scenario applies to one calibration quote.

    Attributes:
        curve: Curve of the quote (``"OIS"``, ``"EURIBOR6M"``).
        label: Quote tenor (``"5Y"``).
        instrument_type: Quote type (``"ois_swap"``).
        shift_bp: Shift of the quoted par rate, in basis points.
    """

    curve: str
    label: str
    instrument_type: str
    shift_bp: float


def scenario_from_dict(data: Mapping[str, Any]) -> Scenario:
    """Parse ``{"name", "description"?, "curves"?, "points": [{"tenor", "shift_bp"}]}``.

    Raises:
        UnsupportedScenarioError: If a field is missing or invalid.
    """
    try:
        name = str(data["name"])
        points = tuple(
            ScenarioPoint(tenor_label=str(raw["tenor"]), shift_bp=float(raw["shift_bp"]))
            for raw in data["points"]
        )
    except (KeyError, TypeError, ValueError) as error:
        raise UnsupportedScenarioError(
            f"invalid scenario definition: {error!r}", {"scenario": str(data.get("name", ""))}
        ) from None
    curves = data.get("curves")
    return Scenario(
        name=name,
        points=points,
        description=str(data.get("description", "")),
        curves=None if curves is None else frozenset(str(curve) for curve in curves),
    )


def register_scenarios_from_package_data() -> None:
    """Load the preset scenarios of ``data/scenarios.json`` into the ``SCENARIOS`` registry."""
    raw = resources.files(SCENARIOS_DATA_PACKAGE).joinpath(SCENARIOS_FILE).read_text("utf-8")
    for entry in json.loads(raw)["scenarios"]:
        scenario = scenario_from_dict(entry)
        SCENARIOS.add(scenario.name, scenario)


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    """Revaluation of a trade under one scenario.

    Attributes:
        scenario: The scenario applied.
        present_value: PV on the shocked market.
        pnl: ``present_value - base PV`` (currency of the trade).
        shifts: Shift applied to each calibration quote.
    """

    scenario: Scenario
    present_value: float
    pnl: float
    shifts: tuple[QuoteShift, ...]


@dataclass(frozen=True, slots=True)
class ScenarioReport:
    """Scenario P&L of one instrument.

    Attributes:
        instrument_type: Instrument type of the trade.
        base_pv: PV on the unshocked market.
        results: One result per scenario, in request order.
        duration_ms: Total wall-clock time.
    """

    instrument_type: str
    base_pv: float
    results: tuple[ScenarioResult, ...]
    duration_ms: float

    def result(self, name: str) -> ScenarioResult:
        """Result of the scenario called ``name``.

        Raises:
            KeyError: If no such scenario was run.
        """
        for result in self.results:
            if result.scenario.name == name:
                return result
        raise KeyError(name)


def resolve_scenarios(
    names: Sequence[str] = (), custom: Sequence[Scenario] = ()
) -> tuple[Scenario, ...]:
    """Preset scenarios called ``names`` followed by ``custom`` ones (all presets if both empty).

    Raises:
        UnsupportedScenarioError: If a name is unknown or two scenarios share a name.
    """
    scenarios = [SCENARIOS.get(name) for name in names] + list(custom)
    if not scenarios:
        scenarios = [SCENARIOS.get(name) for name in SCENARIOS]
    seen: set[str] = set()
    for scenario in scenarios:
        if scenario.name in seen:
            raise UnsupportedScenarioError(
                "two scenarios share the same name", {"scenario": scenario.name}
            )
        seen.add(scenario.name)
    return tuple(scenarios)


class ScenarioPnL:
    """Full revaluation of a trade under market scenarios (no Taylor approximation).

    Args:
        pricer: Values an instrument on a market snapshot: ``(instrument, snapshot) -> PV``,
            typically re-bootstrap then price (the same abstraction as the risk measures).
    """

    def __init__(self, pricer: Pricer) -> None:
        self._pricer = pricer

    def compute(
        self, instrument: Instrument, snapshot: MarketSnapshot, scenarios: Sequence[Scenario]
    ) -> ScenarioReport:
        """P&L of ``instrument`` under each scenario.

        Raises:
            UnsupportedScenarioError: If a scenario selects no quote.
            RiskComputationError: If a shocked market cannot be calibrated or priced.
            MarketDataError: If the base market cannot value the trade (not wrapped).
        """
        started = time.perf_counter()
        # The base valuation is not wrapped: a trade the base market cannot value is an input
        # error, not a scenario failure.
        base_pv = self._pricer(instrument, snapshot)
        results = []
        for scenario in scenarios:
            shocked = scenario.apply(snapshot)
            try:
                present_value = self._pricer(instrument, shocked)
            except RatesPricerError as error:
                raise RiskComputationError(
                    f"scenario {scenario.name} failed: {error.message}",
                    {"scenario": scenario.name, "cause": type(error).__name__, **error.details},
                ) from error
            results.append(
                ScenarioResult(
                    scenario=scenario,
                    present_value=present_value,
                    pnl=present_value - base_pv,
                    shifts=scenario.quote_shifts(snapshot),
                )
            )
            logger.debug(
                "scenario_repriced",
                extra={"scenario": scenario.name, "pv_scenario": present_value},
            )
        duration_ms = (time.perf_counter() - started) * MILLISECONDS_PER_SECOND
        logger.info(
            "scenarios_completed",
            extra={
                "trade_type": instrument.instrument_type,
                "scenarios": [scenario.name for scenario in scenarios],
                "base_pv": base_pv,
                "duration_ms": duration_ms,
            },
        )
        return ScenarioReport(
            instrument_type=instrument.instrument_type,
            base_pv=base_pv,
            results=tuple(results),
            duration_ms=duration_ms,
        )
