"""Mappers between service inputs, core domain objects and JSON-ready result payloads."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import asdict
from datetime import date
from typing import Any

from rate_derivatives_pricer.core.conventions.day_count import Act360
from rate_derivatives_pricer.core.conventions.tenor import Tenor, TimeUnit
from rate_derivatives_pricer.core.engines.results import PricingResult
from rate_derivatives_pricer.core.market.curves import Curve
from rate_derivatives_pricer.core.market.environment import CalibrationReport, MarketEnvironment
from rate_derivatives_pricer.core.market.providers import (
    MarketDataProvider,
    snapshot_from_dict,
    snapshot_to_dict,
)
from rate_derivatives_pricer.core.market.snapshot import MarketSnapshot
from rate_derivatives_pricer.core.risk import (
    CarryRolldownResult,
    RiskReport,
    Scenario,
    ScenarioReport,
    scenario_from_dict,
)
from rate_derivatives_pricer.services.requests import (
    INLINE_SNAPSHOT_NAME,
    CalculationOptions,
    InlineMarket,
    MarketInput,
    SnapshotReference,
    TradeInput,
)

# Curve sampling for display: one point per month up to the last pillar.
CURVE_GRID_STEP = Tenor(1, TimeUnit.MONTHS)
# Forward rates of the curve grid: simply compounded, one month, Act/360 (money-market style).
CURVE_GRID_FORWARD_TENOR = Tenor(1, TimeUnit.MONTHS)
CURVE_GRID_FORWARD_DAY_COUNT = Act360()
# Carry/roll-down hypothesis reported with every result (ADR-034).
CARRY_HYPOTHESIS = "unchanged_curve"


def snapshot_from_input(
    market: MarketInput, options: CalculationOptions, provider: MarketDataProvider
) -> MarketSnapshot:
    """Resolve market data given by reference (provider lookup) or by value.

    Raises:
        MarketDataNotFoundError: If a referenced snapshot does not exist.
        MarketDataError: If the market data is invalid.
    """
    if isinstance(market, SnapshotReference):
        return provider.load_snapshot(market.snapshot)
    inline: InlineMarket = market
    return snapshot_from_dict(
        {
            "name": INLINE_SNAPSHOT_NAME,
            "currency": options.currency,
            "valuation_date": inline.valuation_date,
            "quotes": [dict(q) for q in inline.quotes],
            "fixings": {k: [dict(f) for f in v] for k, v in inline.fixings.items()},
        }
    )


def canonical_hash(payload: Any) -> str:
    """SHA-256 of a canonical JSON serialization (sorted keys, no whitespace)."""
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def market_fingerprint(
    snapshot: MarketSnapshot, options: CalculationOptions, core_version: str
) -> str:
    """Curve cache key and input fingerprint of the market (docs/API.md section 6).

    The snapshot name is excluded: identical data under two names builds the same curves.
    """
    data = snapshot_to_dict(snapshot)
    data.pop("name")
    data.pop("description")
    return canonical_hash(
        {
            "snapshot": data,
            "interpolation": options.interpolation,
            "solver": options.solver,
            "core_version": core_version,
        }
    )


def trade_fingerprint(trade: TradeInput) -> str:
    """Input fingerprint of a trade description."""
    return canonical_hash({"type": trade.type, "params": dict(trade.params)})


def _finite_or_none(value: float) -> float | None:
    """JSON has no NaN: undefined numbers become ``null``."""
    return value if math.isfinite(value) else None


def _calibration_payload(report: CalibrationReport, curve: Curve) -> dict[str, Any]:
    return {
        "curve_name": report.curve_name,
        "interpolation": report.interpolation,
        "solver": report.solver,
        "max_abs_residual": report.max_abs_residual,
        "max_iterations": report.max_iterations,
        "nodes": [
            {
                "label": pillar.label,
                "instrument_type": pillar.instrument_type,
                "pillar_date": pillar.pillar_date.isoformat(),
                "time": curve.time(pillar.pillar_date),
                "market_quote": pillar.market_quote,
                "implied_quote": pillar.implied_quote,
                "residual": pillar.residual,
                "iterations": pillar.iterations,
                "discount_factor": pillar.discount_factor,
                "zero_rate": curve.zero_rate(pillar.pillar_date),
            }
            for pillar in report.pillars
        ],
    }


def _curve_grid(curve: Curve, last_date: date) -> list[dict[str, Any]]:
    """Monthly sampling of discount factors, zero rates and 1M forwards."""
    points = []
    step = 0
    current = curve.reference_date
    while current <= last_date:
        forward_end = CURVE_GRID_FORWARD_TENOR.add_to(current)
        points.append(
            {
                "date": current.isoformat(),
                "time": curve.time(current),
                "discount_factor": curve.discount_factor(current),
                "zero_rate": curve.zero_rate(current),
                "forward_rate": curve.forward_rate(
                    current, forward_end, CURVE_GRID_FORWARD_DAY_COUNT
                ),
            }
        )
        step += 1
        current = (CURVE_GRID_STEP * step).add_to(curve.reference_date)
    return points


def curves_payload(snapshot: MarketSnapshot, env: MarketEnvironment) -> dict[str, Any]:
    """Result of ``/v1/curves/build``: calibration nodes and a sampled curve, per curve.

    The overnight (discounting) curve comes first, then one projection curve per Ibor index.
    """
    curves = []
    for report in env.calibration:
        curve = env.projection_curve(report.index_name)
        curves.append(
            {
                **_calibration_payload(report, curve),
                "role": "discount_and_projection" if report.is_discount_curve else "projection",
                "reference_date": curve.reference_date.isoformat(),
                "grid": _curve_grid(curve, report.pillars[-1].pillar_date),
            }
        )
    return {
        "snapshot": {
            "name": snapshot.name,
            "currency": snapshot.currency,
            "valuation_date": snapshot.valuation_date.isoformat(),
        },
        "curves": curves,
    }


def pricing_payload(result: PricingResult) -> dict[str, Any]:
    """Result of ``/v1/price``."""
    return {
        "instrument_type": result.instrument_type,
        "currency": result.currency,
        "valuation_date": result.valuation_date.isoformat(),
        "present_value": result.present_value,
        "par_rate": _finite_or_none(result.par_rate),
        "annuity": result.annuity,
        "pvbp": result.pvbp,
        "legs": [asdict(leg) for leg in result.legs],
        "cashflows": [
            {
                **asdict(row),
                "accrual_start": row.accrual_start.isoformat(),
                "accrual_end": row.accrual_end.isoformat(),
                "payment_date": row.payment_date.isoformat(),
            }
            for row in result.cashflows
        ],
        "engine": result.diagnostics.engine,
    }


def risk_payload(report: RiskReport, currency: str, valuation_date: date) -> dict[str, Any]:
    """Result of ``/v1/risk``: base PV, bump settings and one entry per measure."""
    return {
        "instrument_type": report.instrument_type,
        "currency": currency,
        "valuation_date": valuation_date.isoformat(),
        "present_value": report.base_pv,
        "bump": {"size_bp": report.settings.bump_size_bp, "mode": report.settings.mode.value},
        "measures": [
            {
                "measure": result.measure,
                "value": result.value,
                "unit": result.unit,
                "bump_count": result.bump_count,
                "duration_ms": result.duration_ms,
                "buckets": [asdict(bucket) for bucket in result.buckets],
            }
            for result in report.results
        ],
    }


def snapshot_payload(snapshot: MarketSnapshot) -> dict[str, Any]:
    """Content of a stored snapshot (canonical dictionary)."""
    return snapshot_to_dict(snapshot)


def scenario_input(raw: Mapping[str, Any]) -> Scenario:
    """Custom scenario of a request (``{"name", "points": [{"tenor", "shift_bp"}], ...}``).

    Raises:
        UnsupportedScenarioError: If the definition is invalid.
    """
    return scenario_from_dict(raw)


def scenarios_payload(
    report: ScenarioReport, currency: str, valuation_date: date
) -> dict[str, Any]:
    """Result of ``/v1/risk/scenarios``: base PV and one entry per scenario."""
    return {
        "instrument_type": report.instrument_type,
        "currency": currency,
        "valuation_date": valuation_date.isoformat(),
        "present_value": report.base_pv,
        "scenarios": [
            {
                "name": result.scenario.name,
                "description": result.scenario.description,
                "curves": sorted(result.scenario.curves) if result.scenario.curves else None,
                "points": [
                    {"tenor": point.tenor_label, "shift_bp": point.shift_bp}
                    for point in result.scenario.points
                ],
                "present_value": result.present_value,
                "pnl": result.pnl,
                "shifts": [asdict(shift) for shift in result.shifts],
            }
            for result in report.results
        ],
    }


def scenario_catalog_entry(scenario: Scenario) -> dict[str, Any]:
    """Preset scenario as published by ``/v1/catalog``."""
    return {
        "name": scenario.name,
        "description": scenario.description,
        "points": [
            {"tenor": point.tenor_label, "shift_bp": point.shift_bp} for point in scenario.points
        ],
    }


def carry_payload(result: CarryRolldownResult, currency: str) -> dict[str, Any]:
    """Result of ``/v1/risk/carry``."""
    return {
        "instrument_type": result.instrument_type,
        "currency": currency,
        "valuation_date": result.valuation_date.isoformat(),
        "hypothesis": CARRY_HYPOTHESIS,
        "horizon": result.horizon,
        "horizon_date": result.horizon_date.isoformat(),
        "present_value": result.base_pv,
        "horizon_present_value": result.horizon_pv,
        "cashflows": result.cashflows,
        "total": result.total,
        "carry": result.carry,
        "rolldown": result.rolldown,
        "assumed_fixings": dict(result.assumed_fixings),
    }
