"""Phase 2 pipeline: multi-curve pricing and risk, through the core and through SDK -> HTTP.

Results are frozen in a golden file (tests/fixtures/): same inputs and same core version must give
the same numbers. Regenerate it deliberately with ``UPDATE_GOLDEN=1 uv run pytest
tests/integration`` and explain the change in the commit message.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rate_derivatives_pricer.api.main import create_app
from rate_derivatives_pricer.api.settings import Settings
from rate_derivatives_pricer.core.engines.base import engine_for
from rate_derivatives_pricer.core.instruments.swaps import Instrument, Swap
from rate_derivatives_pricer.core.market.bootstrap import CurveBuilder
from rate_derivatives_pricer.core.market.providers import FileMarketDataProvider
from rate_derivatives_pricer.core.market.snapshot import MarketSnapshot
from rate_derivatives_pricer.core.registry import INSTRUMENTS
from rate_derivatives_pricer.core.risk import RiskEngine
from rate_derivatives_pricer.sdk import PricerClient

ROOT = Path(__file__).resolve().parents[2]
MARKET_DIR = ROOT / "data" / "market"
TRADES_FILE = ROOT / "data" / "trades" / "irs_samples.json"
GOLDEN_FILE = ROOT / "tests" / "fixtures" / "golden_phase2_EUR_2026-10-19.json"
SNAPSHOT = "EUR_2026-10-19"
# Platform noise only (Windows vs Linux libm). Relative for rates and sensitivities, per unit of
# notional for PVs (ADR-024), absolute for discount factors.
GOLDEN_REL = 1e-9
GOLDEN_PV_PER_NOTIONAL = 1e-12
GOLDEN_DISCOUNT_FACTOR_ABS = 1e-13
MEASURES = ["parallel_dv01", "key_rate_dv01", "curve_dv01", "convexity"]
# Trades whose full risk (a few seconds each) is part of the golden file.
RISK_TRADE_IDS = ["IRS-3M-2Y-RECEIVER", "IRS-6M-5Y-PAYER"]

SAMPLE_TRADES: list[dict[str, Any]] = json.loads(TRADES_FILE.read_text(encoding="utf-8"))["trades"]


@pytest.fixture(scope="module")
def sdk() -> Iterator[PricerClient]:
    app = create_app(Settings(market_data_dir=MARKET_DIR), configure_logs=False)
    with TestClient(app) as client, PricerClient(http_client=client) as pricer:
        yield pricer


def _snapshot() -> MarketSnapshot:
    return FileMarketDataProvider(MARKET_DIR).load_snapshot(SNAPSHOT)


def _trade(trade_id: str) -> dict[str, Any]:
    return next(trade for trade in SAMPLE_TRADES if trade["id"] == trade_id)


def _core_risk(trade: dict[str, Any]) -> dict[str, Any]:
    """Risk through the core, with the pricer the service builds (curves up to the horizon)."""
    snapshot = _snapshot()
    builder = CurveBuilder()
    engine = engine_for(trade["type"])
    instrument = INSTRUMENTS.get(trade["type"])(trade["params"], snapshot.valuation_date)

    def pricer(priced: Instrument, bumped: MarketSnapshot) -> float:
        horizon = priced.last_cashflow_date if isinstance(priced, Swap) else None
        return engine.present_value(priced, builder.build(bumped, horizon=horizon))

    report = RiskEngine(pricer).compute(instrument, snapshot, MEASURES)
    return {
        result.measure: {
            "value": result.value,
            "buckets": {f"{b.curve}/{b.label}": b.value for b in result.buckets},
        }
        for result in report.results
    }


def _core_results() -> dict[str, Any]:
    snapshot = _snapshot()
    env = CurveBuilder().build(snapshot)
    trades = {}
    for trade in SAMPLE_TRADES:
        instrument = INSTRUMENTS.get(trade["type"])(trade["params"], snapshot.valuation_date)
        result = engine_for(trade["type"]).price(instrument, env)
        trades[trade["id"]] = {
            "present_value": result.present_value,
            "par_rate": result.par_rate,
            "annuity": result.annuity,
        }
    return {
        "discount_factors": {
            report.curve_name: {p.label: p.discount_factor for p in report.pillars}
            for report in env.calibration[1:]
        },
        "trades": trades,
        "risk": {trade_id: _core_risk(_trade(trade_id)) for trade_id in RISK_TRADE_IDS},
    }


def test_sdk_and_core_give_identical_prices(sdk: PricerClient) -> None:
    expected = _core_results()["trades"]
    for trade in SAMPLE_TRADES:
        result = sdk.price(SNAPSHOT, {"type": trade["type"], "params": trade["params"]}).result
        assert result.present_value == expected[trade["id"]]["present_value"], trade["id"]
        assert result.par_rate == expected[trade["id"]]["par_rate"], trade["id"]
        assert result.annuity == expected[trade["id"]]["annuity"], trade["id"]


def test_sdk_and_core_give_identical_risk(sdk: PricerClient) -> None:
    trade = _trade("IRS-6M-5Y-PAYER")
    expected = _core_risk(trade)
    risk = sdk.risk(SNAPSHOT, {"type": trade["type"], "params": trade["params"]}, MEASURES)
    assert [m.measure for m in risk.result.measures] == MEASURES
    for measure in risk.result.measures:
        assert measure.value == expected[measure.measure]["value"], measure.measure
        assert {f"{b.curve}/{b.label}": b.value for b in measure.buckets} == (
            expected[measure.measure]["buckets"]
        ), measure.measure
    # The base PV of the risk run is the PV of the pricing endpoint.
    priced = sdk.price(SNAPSHOT, {"type": trade["type"], "params": trade["params"]}).result
    assert risk.result.present_value == pytest.approx(priced.present_value, rel=1e-13)


def test_curve_nodes_through_the_sdk_reprice_the_ibor_quotes(sdk: PricerClient) -> None:
    quotes = {
        (q.index, q.tenor): q.value / 100
        for q in sdk.get_snapshot(SNAPSHOT).quotes
        if q.type in {"ibor_deposit", "ibor_swap"}
    }
    for curve in sdk.build_curves(SNAPSHOT).result.curves[1:]:
        index = curve.curve_name.removeprefix("EUR-")
        assert len(curve.nodes) == sum(1 for (i, _) in quotes if i == index)
        for node in curve.nodes:
            assert node.implied_quote == pytest.approx(quotes[(index, node.label)], abs=1e-13)


def test_results_match_the_golden_file() -> None:
    actual = _core_results()
    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_FILE.write_text(json.dumps(actual, indent=2) + "\n", encoding="utf-8")
    golden = json.loads(GOLDEN_FILE.read_text(encoding="utf-8"))

    assert golden["discount_factors"].keys() == actual["discount_factors"].keys()
    for curve, nodes in golden["discount_factors"].items():
        assert nodes.keys() == actual["discount_factors"][curve].keys()
        for label, discount_factor in nodes.items():
            assert actual["discount_factors"][curve][label] == pytest.approx(
                discount_factor, abs=GOLDEN_DISCOUNT_FACTOR_ABS
            ), (curve, label)

    notionals = {trade["id"]: float(trade["params"]["notional"]) for trade in SAMPLE_TRADES}
    for trade_id, figures in golden["trades"].items():
        for name, value in figures.items():
            tolerance = (
                pytest.approx(value, abs=GOLDEN_PV_PER_NOTIONAL * notionals[trade_id])
                if name == "present_value"
                else pytest.approx(value, rel=GOLDEN_REL)
            )
            assert actual["trades"][trade_id][name] == tolerance, (trade_id, name)

    for trade_id, measures in golden["risk"].items():
        scale = abs(measures["parallel_dv01"]["value"])
        for name, golden_measure in measures.items():
            actual_measure = actual["risk"][trade_id][name]
            assert actual_measure["value"] == pytest.approx(
                golden_measure["value"], rel=GOLDEN_REL, abs=GOLDEN_REL * scale
            ), (trade_id, name)
            assert actual_measure["buckets"].keys() == golden_measure["buckets"].keys()
            for bucket, value in golden_measure["buckets"].items():
                assert actual_measure["buckets"][bucket] == pytest.approx(
                    value, abs=GOLDEN_REL * scale
                ), (trade_id, name, bucket)
