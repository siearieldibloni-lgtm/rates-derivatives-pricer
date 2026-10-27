"""Full pipeline: quotes -> curve -> PV, through the core and through SDK -> HTTP -> API.

Results are also frozen in a golden file (tests/fixtures/): same inputs and same core version
must give the same numbers. Regenerate it deliberately with ``UPDATE_GOLDEN=1 uv run pytest
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
from rate_derivatives_pricer.core.market.bootstrap import CurveBuilder
from rate_derivatives_pricer.core.market.providers import FileMarketDataProvider
from rate_derivatives_pricer.core.registry import INSTRUMENTS
from rate_derivatives_pricer.sdk import PricerClient

ROOT = Path(__file__).resolve().parents[2]
MARKET_DIR = ROOT / "data" / "market"
TRADES_FILE = ROOT / "data" / "trades" / "ois_samples.json"
GOLDEN_FILE = ROOT / "tests" / "fixtures" / "golden_EUR_2026-10-19.json"
SNAPSHOT = "EUR_2026-10-19"
# Golden comparisons allow for floating-point noise across platforms (Windows vs Linux libm) only.
# Relative for par rates and annuities, per unit of notional for PVs.
GOLDEN_REL = 1e-12
GOLDEN_DISCOUNT_FACTOR_ABS = 1e-14

SAMPLE_TRADES: list[dict[str, Any]] = json.loads(TRADES_FILE.read_text(encoding="utf-8"))["trades"]


@pytest.fixture(scope="module")
def sdk() -> Iterator[PricerClient]:
    app = create_app(Settings(market_data_dir=MARKET_DIR), configure_logs=False)
    with TestClient(app) as client, PricerClient(http_client=client) as pricer:
        yield pricer


def _core_results() -> dict[str, Any]:
    snapshot = FileMarketDataProvider(MARKET_DIR).load_snapshot(SNAPSHOT)
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
        "discount_factors": {p.label: p.discount_factor for p in env.calibration[0].pillars},
        "trades": trades,
    }


def test_sdk_and_core_give_identical_results(sdk: PricerClient) -> None:
    expected = _core_results()["trades"]
    for trade in SAMPLE_TRADES:
        result = sdk.price(SNAPSHOT, {"type": trade["type"], "params": trade["params"]}).result
        assert result.present_value == expected[trade["id"]]["present_value"], trade["id"]
        assert result.par_rate == expected[trade["id"]]["par_rate"], trade["id"]


def test_curve_build_through_the_sdk_reprices_quotes(sdk: PricerClient) -> None:
    curve = sdk.build_curves(SNAPSHOT).result.curves[0]
    assert curve.curve_name == "EUR-ESTR"
    # Only the overnight quotes calibrate this curve; Ibor quotes reuse the same tenor labels.
    quotes = {
        q.tenor: q.value / 100
        for q in sdk.get_snapshot(SNAPSHOT).quotes
        if q.type in {"deposit", "ois_swap"}
    }
    for node in curve.nodes:
        assert node.implied_quote == pytest.approx(quotes[node.label], abs=1e-13)


def test_results_match_the_golden_file() -> None:
    actual = _core_results()
    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_FILE.write_text(json.dumps(actual, indent=2) + "\n", encoding="utf-8")
    golden = json.loads(GOLDEN_FILE.read_text(encoding="utf-8"))
    assert golden["discount_factors"].keys() == actual["discount_factors"].keys()
    for label, discount_factor in golden["discount_factors"].items():
        assert actual["discount_factors"][label] == pytest.approx(
            discount_factor, abs=GOLDEN_DISCOUNT_FACTOR_ABS
        ), label
    notionals = {trade["id"]: float(trade["params"]["notional"]) for trade in SAMPLE_TRADES}
    for trade_id, figures in golden["trades"].items():
        for name, value in figures.items():
            # A PV close to zero is a difference of two large legs: compare it per unit of
            # notional (ADR-024), otherwise last-bit exp/log differences between platforms
            # dominate. Par rates and annuities are compared relatively.
            tolerance = (
                pytest.approx(value, abs=GOLDEN_REL * notionals[trade_id])
                if name == "present_value"
                else pytest.approx(value, rel=GOLDEN_REL)
            )
            assert actual["trades"][trade_id][name] == tolerance, (trade_id, name)
