"""Pricing service: orchestrates market data, curve cache, bootstrap and pricing.

Every calculation returns a :class:`ServiceResult` carrying a JSON-ready result and the
diagnostics of the response envelope (timings, cache status, versions, warnings), and emits one
``calculation_audit`` log event with the input fingerprints needed to replay it (docs/API.md 7.3).
"""

from __future__ import annotations

import logging
import platform
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

import numpy as np

import rate_derivatives_pricer
from rate_derivatives_pricer.core.conventions.tenor import Tenor
from rate_derivatives_pricer.core.engines.base import (
    DEFAULT_PRICING_METHOD,
    PricingEngine,
    engine_for,
)
from rate_derivatives_pricer.core.engines.results import PricingResult
from rate_derivatives_pricer.core.errors import ConventionError, RatesPricerError
from rate_derivatives_pricer.core.instruments.swaps import Instrument, Swap
from rate_derivatives_pricer.core.market.bootstrap import CurveBuilder, CurveBuildOptions
from rate_derivatives_pricer.core.market.environment import MarketEnvironment
from rate_derivatives_pricer.core.market.providers import MarketDataProvider
from rate_derivatives_pricer.core.market.snapshot import MarketSnapshot
from rate_derivatives_pricer.core.market.solvers import SOLVERS
from rate_derivatives_pricer.core.registry import (
    CALENDARS,
    CONVENTIONS,
    DAY_COUNTS,
    ENGINES,
    INSTRUMENTS,
    INTERPOLATORS,
    RISK_MEASURES,
    SCENARIOS,
    InstrumentFactory,
)
from rate_derivatives_pricer.core.risk import (
    BumpMode,
    CarryRolldown,
    Pricer,
    RiskEngine,
    RiskSettings,
    ScenarioPnL,
    resolve_scenarios,
)
from rate_derivatives_pricer.services.curve_cache import CacheStats, CurveCache
from rate_derivatives_pricer.services.mappers import (
    carry_payload,
    curves_payload,
    market_fingerprint,
    pricing_payload,
    risk_payload,
    scenario_catalog_entry,
    scenario_input,
    scenarios_payload,
    snapshot_from_input,
    snapshot_payload,
    trade_fingerprint,
)
from rate_derivatives_pricer.services.requests import (
    BatchTrade,
    CalculationOptions,
    CarryOptions,
    MarketInput,
    RiskOptions,
    ScenarioOptions,
    TradeInput,
)

logger = logging.getLogger(__name__)

MILLISECONDS_PER_SECOND = 1000.0
type CacheStatus = Literal["hit", "miss"]


@dataclass(frozen=True, slots=True)
class Diagnostics:
    """Diagnostics block of the response envelope.

    Attributes:
        timings_ms: Duration of each step in milliseconds.
        curve_cache: ``"hit"`` or ``"miss"`` (``None`` when no curve was needed).
        warnings: Non-fatal issues.
        input_hashes: Fingerprints of the inputs (market, trade).
    """

    timings_ms: dict[str, float] = field(default_factory=dict)
    curve_cache: CacheStatus | None = None
    warnings: tuple[str, ...] = ()
    input_hashes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ServiceResult:
    """JSON-ready result plus diagnostics."""

    result: dict[str, Any]
    diagnostics: Diagnostics


@dataclass(frozen=True, slots=True)
class BatchItem:
    """Outcome of one trade of a batch: a JSON-ready pricing result, or the error it raised.

    Attributes:
        trade_id: Identifier of the trade in the request.
        result: Pricing payload (as ``/v1/price``), ``None`` on failure.
        error: Exception raised by this trade only, ``None`` on success. A
            :class:`RatesPricerError` is translated by the API like any request error; anything
            else is reported as an internal error (the trace is logged, never returned).
        trade_hash: Fingerprint of the trade.
        duration_ms: Time spent on this trade.
    """

    trade_id: str
    result: dict[str, Any] | None
    error: Exception | None
    trade_hash: str
    duration_ms: float


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    """Per-trade outcomes of a batch, in request order, and the diagnostics of the batch.

    Attributes:
        valuation_date: Valuation date of the shared market.
        currency: Currency of the shared market.
        items: One entry per trade, in request order.
        diagnostics: Timings, cache status, warnings (prefixed by the trade id), market hash.
    """

    valuation_date: date
    currency: str
    items: tuple[BatchItem, ...]
    diagnostics: Diagnostics


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * MILLISECONDS_PER_SECOND


class PricerService:
    """Stateless calculation façade over the quant core (state lives in the cache only).

    Args:
        provider: Source of stored market snapshots.
        cache: Cache of bootstrapped environments.
    """

    def __init__(self, provider: MarketDataProvider, cache: CurveCache) -> None:
        self._provider = provider
        self._cache = cache

    @property
    def core_version(self) -> str:
        """Version of the quant core."""
        return rate_derivatives_pricer.__version__

    # ----------------------------------------------------------------- metadata
    def version_info(self) -> dict[str, str]:
        """Versions of the service, the core and the runtime."""
        return {
            "api": rate_derivatives_pricer.__version__,
            "core": self.core_version,
            "python": platform.python_version(),
            "numpy": np.__version__,
        }

    def readiness(self) -> dict[str, bool]:
        """Readiness checks: conventions and registries loaded."""
        return {
            "conventions": len(CONVENTIONS) > 0,
            "instruments": len(INSTRUMENTS) > 0,
            "engines": len(ENGINES) > 0,
            "interpolators": len(INTERPOLATORS) > 0,
        }

    def catalog(self) -> dict[str, Any]:
        """Supported components, generated from the registries."""
        return {
            "instrument_types": list(INSTRUMENTS.keys()),
            "pricing_methods": [
                {"instrument_type": instrument, "method": method} for instrument, method in ENGINES
            ],
            "interpolations": list(INTERPOLATORS.keys()),
            "solvers": sorted(SOLVERS),
            "currencies": [
                {
                    "currency": code,
                    "calendar": conventions.calendar.name,
                    "spot_lag_days": conventions.spot_lag_days,
                    "overnight_index": conventions.overnight_index.name,
                    "ibor_indices": [index.name for index in conventions.ibor_indices],
                }
                for code, conventions in ((c, CONVENTIONS.get(c)) for c in CONVENTIONS)
            ],
            "calendars": list(CALENDARS.keys()),
            "day_counts": list(DAY_COUNTS.keys()),
            "risk_measures": list(RISK_MEASURES.keys()),
            "scenarios": [scenario_catalog_entry(SCENARIOS.get(name)) for name in SCENARIOS],
        }

    def cache_stats(self) -> CacheStats:
        """Curve cache counters."""
        return self._cache.stats()

    def clear_cache(self) -> int:
        """Empty the curve cache (``DELETE /v1/cache``); return the number of entries dropped."""
        dropped = self._cache.clear()
        logger.info("curve_cache_cleared", extra={"entries_dropped": dropped})
        return dropped

    # ----------------------------------------------------------------- market data
    def list_snapshots(self) -> list[str]:
        """Names of the stored snapshots."""
        return list(self._provider.list_snapshots())

    def get_snapshot(self, name: str) -> dict[str, Any]:
        """Content of a stored snapshot.

        Raises:
            MarketDataNotFoundError: If the snapshot does not exist.
        """
        return snapshot_payload(self._provider.load_snapshot(name))

    # ----------------------------------------------------------------- calculations
    def _environment(
        self, market: MarketInput, options: CalculationOptions, timings: dict[str, float]
    ) -> tuple[MarketSnapshot, MarketEnvironment, CacheStatus, str]:
        """Resolve the snapshot and return its environment, from the cache when possible."""
        started = time.perf_counter()
        snapshot = snapshot_from_input(market, options, self._provider)
        timings["market_data"] = _elapsed_ms(started)

        key = market_fingerprint(snapshot, options, self.core_version)
        cached = self._cache.get(key)
        if cached is not None:
            logger.info("curve_cache_hit", extra={"snapshot_name": snapshot.name, "cache_key": key})
            timings["curve_build"] = 0.0
            return snapshot, cached, "hit", key

        started = time.perf_counter()
        builder = CurveBuilder(
            CurveBuildOptions(interpolation=options.interpolation, solver=options.solver)
        )
        env = builder.build(snapshot)
        timings["curve_build"] = _elapsed_ms(started)
        self._cache.put(key, env)
        logger.info(
            "curve_cache_miss",
            extra={
                "snapshot_name": snapshot.name,
                "cache_key": key,
                "duration_ms": timings["curve_build"],
            },
        )
        return snapshot, env, "miss", key

    def build_curves(self, market: MarketInput, options: CalculationOptions) -> ServiceResult:
        """Bootstrap the curves of a market (``/v1/curves/build``).

        Raises:
            MarketDataError: Invalid or missing market data.
            ConventionError: Unknown currency, interpolation or solver.
            CalibrationError: A pillar could not be calibrated.
        """
        started = time.perf_counter()
        timings: dict[str, float] = {}
        snapshot, env, cache_status, market_hash = self._environment(market, options, timings)
        result = curves_payload(snapshot, env)
        timings["total"] = _elapsed_ms(started)
        report = env.calibration[0]
        logger.info(
            "calculation_audit",
            extra={
                "calculation": "curves_build",
                "market_hash": market_hash,
                "snapshot_name": snapshot.name,
                "options": {"interpolation": options.interpolation, "solver": options.solver},
                "versions": self.version_info(),
                "curve_cache": cache_status,
                "pillar_count": len(report.pillars),
                "max_abs_residual": report.max_abs_residual,
                "duration_ms": timings["total"],
            },
        )
        return ServiceResult(
            result=result,
            diagnostics=Diagnostics(
                timings_ms=timings, curve_cache=cache_status, input_hashes={"market": market_hash}
            ),
        )

    def price(
        self, market: MarketInput, trade: TradeInput, options: CalculationOptions
    ) -> ServiceResult:
        """Price a trade (``/v1/price``).

        Raises:
            UnsupportedInstrumentError: Unknown instrument type or no engine for it.
            ConventionError | ScheduleError: Invalid trade definition (or currency mismatch).
            MarketDataError: Invalid market data or missing fixings.
            CalibrationError: A pillar could not be calibrated.
        """
        started = time.perf_counter()
        timings: dict[str, float] = {}
        # Fail fast on an unknown type, before any market data work.
        factory = INSTRUMENTS.get(trade.type)
        engine = engine_for(trade.type, DEFAULT_PRICING_METHOD)

        snapshot, env, cache_status, market_hash = self._environment(market, options, timings)
        pricing_started = time.perf_counter()
        pricing = _price_trade(factory, engine, trade, snapshot, env)
        timings["pricing"] = _elapsed_ms(pricing_started)
        timings["total"] = _elapsed_ms(started)

        trade_hash = trade_fingerprint(trade)
        logger.info(
            "calculation_audit",
            extra={
                "calculation": "price",
                "market_hash": market_hash,
                "trade_hash": trade_hash,
                "snapshot_name": snapshot.name,
                "trade_type": trade.type,
                "currency": pricing.currency,
                "options": {"interpolation": options.interpolation, "solver": options.solver},
                "versions": self.version_info(),
                "curve_cache": cache_status,
                "present_value": pricing.present_value,
                "par_rate": pricing.par_rate,
                "duration_ms": timings["total"],
            },
        )
        return ServiceResult(
            result=pricing_payload(pricing),
            diagnostics=Diagnostics(
                timings_ms=timings,
                curve_cache=cache_status,
                warnings=pricing.diagnostics.warnings,
                input_hashes={"market": market_hash, "trade": trade_hash},
            ),
        )

    def price_batch(
        self, market: MarketInput, trades: Sequence[BatchTrade], options: CalculationOptions
    ) -> BatchOutcome:
        """Price several trades on one market (``/v1/batch/price``), with errors per trade.

        The market is resolved and bootstrapped once (through the curve cache). A trade that
        fails (unknown type, invalid definition, missing fixings, unexpected error) only fails
        its own entry: the other trades are still priced.

        Raises:
            MarketDataError: Invalid or missing market data (shared by every trade).
            ConventionError: Unknown currency, interpolation or solver.
            CalibrationError: A pillar could not be calibrated.
        """
        started = time.perf_counter()
        timings: dict[str, float] = {}
        snapshot, env, cache_status, market_hash = self._environment(market, options, timings)

        pricing_started = time.perf_counter()
        items: list[BatchItem] = []
        warnings: list[str] = []
        for entry in trades:
            trade_started = time.perf_counter()
            trade_hash = trade_fingerprint(entry.trade)
            result: dict[str, Any] | None = None
            error: Exception | None = None
            try:
                factory = INSTRUMENTS.get(entry.trade.type)
                engine = engine_for(entry.trade.type, DEFAULT_PRICING_METHOD)
                pricing = _price_trade(factory, engine, entry.trade, snapshot, env)
            except RatesPricerError as rejected:
                error = rejected
                logger.warning(
                    "batch_trade_rejected",
                    extra={
                        "trade_id": entry.trade_id,
                        "trade_hash": trade_hash,
                        "error_type": type(rejected).__name__,
                        "error": rejected.message,
                    },
                )
            except Exception as unexpected:
                # One broken trade must not take the batch down: log the trace, keep going.
                error = unexpected
                logger.exception(
                    "batch_trade_failed",
                    extra={"trade_id": entry.trade_id, "trade_hash": trade_hash},
                )
            else:
                result = pricing_payload(pricing)
                warnings.extend(f"{entry.trade_id}: {w}" for w in pricing.diagnostics.warnings)
                logger.info(
                    "batch_trade_priced",
                    extra={
                        "trade_id": entry.trade_id,
                        "trade_hash": trade_hash,
                        "trade_type": entry.trade.type,
                        "present_value": pricing.present_value,
                    },
                )
            items.append(
                BatchItem(
                    trade_id=entry.trade_id,
                    result=result,
                    error=error,
                    trade_hash=trade_hash,
                    duration_ms=_elapsed_ms(trade_started),
                )
            )
        timings["pricing"] = _elapsed_ms(pricing_started)
        timings["total"] = _elapsed_ms(started)

        failed = sum(1 for item in items if item.error is not None)
        logger.info(
            "calculation_audit",
            extra={
                "calculation": "batch_price",
                "market_hash": market_hash,
                "trade_hashes": {item.trade_id: item.trade_hash for item in items},
                "snapshot_name": snapshot.name,
                "currency": snapshot.currency,
                "options": {"interpolation": options.interpolation, "solver": options.solver},
                "versions": self.version_info(),
                "curve_cache": cache_status,
                "trade_count": len(items),
                "succeeded": len(items) - failed,
                "failed": failed,
                "duration_ms": timings["total"],
            },
        )
        return BatchOutcome(
            valuation_date=snapshot.valuation_date,
            currency=snapshot.currency,
            items=tuple(items),
            diagnostics=Diagnostics(
                timings_ms=timings,
                curve_cache=cache_status,
                warnings=tuple(warnings),
                input_hashes={"market": market_hash},
            ),
        )

    def risk(
        self,
        market: MarketInput,
        trade: TradeInput,
        risk_options: RiskOptions,
        options: CalculationOptions,
    ) -> ServiceResult:
        """Risk measures of a trade by bump-and-reprice (``/v1/risk``).

        Every bump shifts the calibration quotes, re-bootstraps the curves and reprices the
        trade. The curves are built only as far as the trade needs and share one builder, so
        pillars a bump cannot move are not solved again. Bumped curves bypass the curve cache.

        Raises:
            UnsupportedInstrumentError: Unknown instrument type.
            UnsupportedRiskMeasureError: Unknown risk measure.
            ConventionError | ScheduleError: Invalid trade definition (or currency mismatch).
            MarketDataError: Invalid market data or missing fixings.
            RiskComputationError: A bumped market could not be calibrated or priced.
        """
        started = time.perf_counter()
        # Fail fast on unknown names, before any market data work.
        INSTRUMENTS.get(trade.type)
        engine_for(trade.type, DEFAULT_PRICING_METHOD)
        for name in risk_options.measures:
            RISK_MEASURES.get(name)
        try:
            mode = BumpMode(risk_options.mode)
        except ValueError:
            raise ConventionError(
                f"unknown bump mode: {risk_options.mode!r}",
                {"mode": risk_options.mode, "available": [m.value for m in BumpMode]},
            ) from None

        snapshot, instrument, engine, builder, timings = self._shocked_market_setup(
            market, trade, options
        )
        pricer = _horizon_pricer(engine, builder)
        risk_started = time.perf_counter()
        report = RiskEngine(pricer).compute(
            instrument,
            snapshot,
            risk_options.measures,
            RiskSettings(bump_size_bp=risk_options.bump_size_bp, mode=mode),
        )
        timings["risk"] = _elapsed_ms(risk_started)
        timings["total"] = _elapsed_ms(started)

        market_hash = market_fingerprint(snapshot, options, self.core_version)
        trade_hash = trade_fingerprint(trade)
        logger.info(
            "calculation_audit",
            extra={
                "calculation": "risk",
                "market_hash": market_hash,
                "trade_hash": trade_hash,
                "snapshot_name": snapshot.name,
                "trade_type": trade.type,
                "currency": instrument.currency,
                "options": {"interpolation": options.interpolation, "solver": options.solver},
                "risk_options": {
                    "measures": list(risk_options.measures),
                    "bump_size_bp": risk_options.bump_size_bp,
                    "mode": mode.value,
                },
                "versions": self.version_info(),
                "present_value": report.base_pv,
                "risk_values": {result.measure: result.value for result in report.results},
                "duration_ms": timings["total"],
            },
        )
        return ServiceResult(
            result=risk_payload(report, instrument.currency, snapshot.valuation_date),
            diagnostics=Diagnostics(
                timings_ms=timings,
                input_hashes={"market": market_hash, "trade": trade_hash},
            ),
        )

    def _shocked_market_setup(
        self, market: MarketInput, trade: TradeInput, options: CalculationOptions
    ) -> tuple[MarketSnapshot, Instrument, PricingEngine, CurveBuilder, dict[str, float]]:
        """Snapshot, instrument, engine and a request-local builder for shocked markets.

        Shocked markets bypass the curve cache; one builder serves every shock of the request,
        so pillars a shock cannot move are solved once (ADR-028).
        """
        timings: dict[str, float] = {}
        factory = INSTRUMENTS.get(trade.type)
        engine = engine_for(trade.type, DEFAULT_PRICING_METHOD)
        started = time.perf_counter()
        snapshot = snapshot_from_input(market, options, self._provider)
        timings["market_data"] = _elapsed_ms(started)
        instrument = factory(dict(trade.params), snapshot.valuation_date)
        if instrument.currency != snapshot.currency:
            raise ConventionError(
                "trade currency differs from the market data currency (no FX in v1)",
                {"trade_currency": instrument.currency, "market_currency": snapshot.currency},
            )
        builder = CurveBuilder(
            CurveBuildOptions(interpolation=options.interpolation, solver=options.solver)
        )
        return snapshot, instrument, engine, builder, timings

    def scenarios(
        self,
        market: MarketInput,
        trade: TradeInput,
        scenario_options: ScenarioOptions,
        options: CalculationOptions,
    ) -> ServiceResult:
        """Full revaluation of a trade under preset and custom scenarios (``/v1/risk/scenarios``).

        Raises:
            UnsupportedInstrumentError: Unknown instrument type.
            UnsupportedScenarioError: Unknown preset, invalid custom scenario, duplicate name,
                or a scenario selecting no quote.
            ConventionError | ScheduleError: Invalid trade definition (or currency mismatch).
            MarketDataError: Invalid market data or missing fixings.
            RiskComputationError: A shocked market could not be calibrated or priced.
        """
        started = time.perf_counter()
        # Fail fast on invalid scenarios, before any market data work.
        scenarios = resolve_scenarios(
            scenario_options.names, [scenario_input(raw) for raw in scenario_options.custom]
        )
        snapshot, instrument, engine, builder, timings = self._shocked_market_setup(
            market, trade, options
        )
        scenarios_started = time.perf_counter()
        report = ScenarioPnL(_horizon_pricer(engine, builder)).compute(
            instrument, snapshot, scenarios
        )
        timings["scenarios"] = _elapsed_ms(scenarios_started)
        timings["total"] = _elapsed_ms(started)

        market_hash = market_fingerprint(snapshot, options, self.core_version)
        trade_hash = trade_fingerprint(trade)
        logger.info(
            "calculation_audit",
            extra={
                "calculation": "scenarios",
                "market_hash": market_hash,
                "trade_hash": trade_hash,
                "snapshot_name": snapshot.name,
                "trade_type": trade.type,
                "currency": instrument.currency,
                "options": {"interpolation": options.interpolation, "solver": options.solver},
                "versions": self.version_info(),
                "present_value": report.base_pv,
                "scenario_pnl": {r.scenario.name: r.pnl for r in report.results},
                "duration_ms": timings["total"],
            },
        )
        return ServiceResult(
            result=scenarios_payload(report, instrument.currency, snapshot.valuation_date),
            diagnostics=Diagnostics(
                timings_ms=timings,
                input_hashes={"market": market_hash, "trade": trade_hash},
            ),
        )

    def carry_rolldown(
        self,
        market: MarketInput,
        trade: TradeInput,
        carry_options: CarryOptions,
        options: CalculationOptions,
    ) -> ServiceResult:
        """Carry and roll-down of a trade over a horizon (``/v1/risk/carry``, ADR-034).

        Raises:
            UnsupportedInstrumentError: Unknown instrument type, or an engine without carry.
            ConventionError | ScheduleError: Invalid trade definition, horizon or currency.
            MarketDataError: Invalid market data, missing fixings or overnight deposit quote.
            RiskComputationError: The horizon market could not be calibrated or priced.
        """
        started = time.perf_counter()
        horizon = Tenor.parse(carry_options.horizon)
        snapshot, instrument, engine, builder, timings = self._shocked_market_setup(
            market, trade, options
        )
        valuer = _EngineHorizonValuer(engine, builder)
        carry_started = time.perf_counter()
        result = CarryRolldown(valuer).compute(instrument, snapshot, horizon)
        timings["carry"] = _elapsed_ms(carry_started)
        timings["total"] = _elapsed_ms(started)

        market_hash = market_fingerprint(snapshot, options, self.core_version)
        trade_hash = trade_fingerprint(trade)
        logger.info(
            "calculation_audit",
            extra={
                "calculation": "carry_rolldown",
                "market_hash": market_hash,
                "trade_hash": trade_hash,
                "snapshot_name": snapshot.name,
                "trade_type": trade.type,
                "currency": instrument.currency,
                "options": {"interpolation": options.interpolation, "solver": options.solver},
                "horizon": result.horizon,
                "versions": self.version_info(),
                "present_value": result.base_pv,
                "carry": result.carry,
                "rolldown": result.rolldown,
                "duration_ms": timings["total"],
            },
        )
        return ServiceResult(
            result=carry_payload(result, instrument.currency),
            diagnostics=Diagnostics(
                timings_ms=timings,
                input_hashes={"market": market_hash, "trade": trade_hash},
            ),
        )


def _price_trade(
    factory: InstrumentFactory,
    engine: PricingEngine,
    trade: TradeInput,
    snapshot: MarketSnapshot,
    env: MarketEnvironment,
) -> PricingResult:
    """Build the instrument of ``trade`` on ``snapshot`` and price it on ``env``.

    Raises:
        ConventionError: Invalid trade definition, or a trade currency other than the market
            currency (no FX in v1).
        ScheduleError | MarketDataError: Invalid dates or missing fixings.
    """
    instrument = factory(dict(trade.params), snapshot.valuation_date)
    if instrument.currency != snapshot.currency:
        raise ConventionError(
            "trade currency differs from the market data currency (no FX in v1)",
            {"trade_currency": instrument.currency, "market_currency": snapshot.currency},
        )
    return engine.price(instrument, env)


def _horizon_pricer(engine: PricingEngine, builder: CurveBuilder) -> Pricer:
    """Pricer of shocked markets: curves built only up to the trade's last cashflow (ADR-028)."""

    def pricer(instrument: Instrument, snapshot: MarketSnapshot) -> float:
        horizon = instrument.last_cashflow_date if isinstance(instrument, Swap) else None
        return engine.present_value(instrument, builder.build(snapshot, horizon=horizon))

    return pricer


class _EngineHorizonValuer:
    """:class:`HorizonValuer` built from a pricing engine and a request-local curve builder."""

    def __init__(self, engine: PricingEngine, builder: CurveBuilder) -> None:
        self._engine = engine
        self._builder = builder

    def _environment(self, instrument: Instrument, snapshot: MarketSnapshot) -> MarketEnvironment:
        horizon = instrument.last_cashflow_date if isinstance(instrument, Swap) else None
        return self._builder.build(snapshot, horizon=horizon)

    def present_value(self, instrument: Instrument, snapshot: MarketSnapshot) -> float:
        """PV on ``snapshot``."""
        return self._engine.present_value(instrument, self._environment(instrument, snapshot))

    def cashflows_paid(
        self, instrument: Instrument, snapshot: MarketSnapshot, start: date, end: date
    ) -> float:
        """Signed undiscounted amounts paid in ``(start, end]``."""
        env = self._environment(instrument, snapshot)
        return self._engine.cashflows_paid(instrument, env, start, end)

    def accrued_interest(
        self, instrument: Instrument, snapshot: MarketSnapshot, start: date, end: date
    ) -> float:
        """Signed net interest accrued over ``[start, end)``."""
        env = self._environment(instrument, snapshot)
        return self._engine.accrued_interest(instrument, env, start, end)
