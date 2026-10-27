"""Phase 3 pipeline: scenarios, carry/roll-down and USD, through the core and through SDK -> HTTP.

Results are frozen in a golden file (tests/fixtures/): same inputs and same core version must give
the same numbers. Regenerate it deliberately with ``UPDATE_GOLDEN=1 uv run pytest
tests/integration`` and explain the change in the commit message.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rate_derivatives_pricer.api.main import create_app
from rate_derivatives_pricer.api.settings import Settings
from rate_derivatives_pricer.core.conventions.tenor import Tenor
from rate_derivatives_pricer.core.engines.base import engine_for
from rate_derivatives_pricer.core.instruments.swaps import Instrument, Swap
from rate_derivatives_pricer.core.market.bootstrap import CurveBuilder
from rate_derivatives_pricer.core.market.providers import FileMarketDataProvider
from rate_derivatives_pricer.core.market.snapshot import MarketSnapshot
from rate_derivatives_pricer.core.registry import INSTRUMENTS
from rate_derivatives_pricer.core.risk import CarryRolldown, ScenarioPnL, resolve_scenarios
from rate_derivatives_pricer.sdk import PricerClient

ROOT = Path(__file__).resolve().parents[2]
MARKET_DIR = ROOT / "data" / "market"
EUR_TRADES_FILE = ROOT / "data" / "trades" / "irs_samples.json"
USD_TRADES_FILE = ROOT / "data" / "trades" / "usd_samples.json"
GOLDEN_FILE = ROOT / "tests" / "fixtures" / "golden_phase3.json"
EUR_SNAPSHOT = "EUR_2026-10-19"
USD_SNAPSHOT = "USD_2026-10-19"
# Platform noise only (Windows vs Linux libm), as for the phase 2 golden file.
GOLDEN_REL = 1e-9
GOLDEN_PV_PER_NOTIONAL = 1e-12
GOLDEN_DISCOUNT_FACTOR_ABS = 1e-13
HORIZONS = ["1M", "3M", "1Y"]
# (snapshot, trades file, trade id): one EUR IRS and one USD OIS.
RISK_TRADES = [
    (EUR_SNAPSHOT, EUR_TRADES_FILE, "IRS-6M-5Y-PAYER"),
    (USD_SNAPSHOT, USD_TRADES_FILE, "SOFR-10Y-RECEIVER"),
]


def _trades(path: Path) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))["trades"]
    return trades


def _trade(path: Path, trade_id: str) -> dict[str, Any]:
    return next(trade for trade in _trades(path) if trade["id"] == trade_id)


def _snapshot(name: str) -> MarketSnapshot:
    return FileMarketDataProvider(MARKET_DIR).load_snapshot(name)


@pytest.fixture(scope="module")
def sdk() -> Iterator[PricerClient]:
    app = create_app(Settings(market_data_dir=MARKET_DIR), configure_logs=False)
    with TestClient(app) as client, PricerClient(http_client=client) as pricer:
        yield pricer


class _Valuer:
    """What the service plugs into the risk components: request-local builder + engine."""

    def __init__(self, trade_type: str) -> None:
        self.builder = CurveBuilder()
        self.engine = engine_for(trade_type)

    def _env(self, instrument: Instrument, snapshot: MarketSnapshot) -> Any:
        horizon = instrument.last_cashflow_date if isinstance(instrument, Swap) else None
        return self.builder.build(snapshot, horizon=horizon)

    def present_value(self, instrument: Instrument, snapshot: MarketSnapshot) -> float:
        return self.engine.present_value(instrument, self._env(instrument, snapshot))

    def cashflows_paid(
        self, instrument: Instrument, snapshot: MarketSnapshot, start: date, end: date
    ) -> float:
        return self.engine.cashflows_paid(instrument, self._env(instrument, snapshot), start, end)

    def accrued_interest(
        self, instrument: Instrument, snapshot: MarketSnapshot, start: date, end: date
    ) -> float:
        return self.engine.accrued_interest(instrument, self._env(instrument, snapshot), start, end)


def _core_scenarios(snapshot_name: str, trade: dict[str, Any]) -> dict[str, float]:
    snapshot = _snapshot(snapshot_name)
    instrument = INSTRUMENTS.get(trade["type"])(trade["params"], snapshot.valuation_date)
    report = ScenarioPnL(_Valuer(trade["type"]).present_value).compute(
        instrument, snapshot, resolve_scenarios()
    )
    return {result.scenario.name: result.pnl for result in report.results}


def _core_carry(snapshot_name: str, trade: dict[str, Any], horizon: str) -> dict[str, float]:
    snapshot = _snapshot(snapshot_name)
    instrument = INSTRUMENTS.get(trade["type"])(trade["params"], snapshot.valuation_date)
    result = CarryRolldown(_Valuer(trade["type"])).compute(
        instrument, snapshot, Tenor.parse(horizon)
    )
    return {"carry": result.carry, "rolldown": result.rolldown, "total": result.total}


def _core_results() -> dict[str, Any]:
    snapshot = _snapshot(USD_SNAPSHOT)
    env = CurveBuilder().build(snapshot)
    usd = {}
    for trade in _trades(USD_TRADES_FILE):
        instrument = INSTRUMENTS.get(trade["type"])(trade["params"], snapshot.valuation_date)
        result = engine_for(trade["type"]).price(instrument, env)
        usd[trade["id"]] = {
            "present_value": result.present_value,
            "par_rate": result.par_rate,
            "annuity": result.annuity,
        }
    return {
        "sofr_discount_factors": {p.label: p.discount_factor for p in env.calibration[0].pillars},
        "usd_trades": usd,
        "scenarios": {
            trade_id: _core_scenarios(name, _trade(path, trade_id))
            for name, path, trade_id in RISK_TRADES
        },
        "carry": {
            trade_id: {h: _core_carry(name, _trade(path, trade_id), h) for h in HORIZONS}
            for name, path, trade_id in RISK_TRADES
        },
    }


@pytest.mark.parametrize(("snapshot_name", "path", "trade_id"), RISK_TRADES)
def test_sdk_and_core_give_identical_scenarios_and_carry(
    sdk: PricerClient, snapshot_name: str, path: Path, trade_id: str
) -> None:
    trade = _trade(path, trade_id)
    request = {"type": trade["type"], "params": trade["params"]}
    shocked = sdk.scenarios(snapshot_name, request).result
    assert {s.name: s.pnl for s in shocked.scenarios} == _core_scenarios(snapshot_name, trade)
    carried = sdk.carry_rolldown(snapshot_name, request, "3M").result
    expected = _core_carry(snapshot_name, trade, "3M")
    assert (carried.carry, carried.rolldown, carried.total) == (
        expected["carry"],
        expected["rolldown"],
        expected["total"],
    )


def test_sdk_and_core_give_identical_usd_prices(sdk: PricerClient) -> None:
    expected = _core_results()["usd_trades"]
    for trade in _trades(USD_TRADES_FILE):
        result = sdk.price(USD_SNAPSHOT, {"type": trade["type"], "params": trade["params"]}).result
        assert result.present_value == expected[trade["id"]]["present_value"], trade["id"]
        assert result.par_rate == expected[trade["id"]]["par_rate"], trade["id"]


def test_results_match_the_golden_file() -> None:
    actual = _core_results()
    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_FILE.write_text(json.dumps(actual, indent=2) + "\n", encoding="utf-8")
    golden = json.loads(GOLDEN_FILE.read_text(encoding="utf-8"))

    assert golden["sofr_discount_factors"].keys() == actual["sofr_discount_factors"].keys()
    for label, discount_factor in golden["sofr_discount_factors"].items():
        assert actual["sofr_discount_factors"][label] == pytest.approx(
            discount_factor, abs=GOLDEN_DISCOUNT_FACTOR_ABS
        ), label

    notionals = {t["id"]: float(t["params"]["notional"]) for t in _trades(USD_TRADES_FILE)}
    notionals.update({t["id"]: float(t["params"]["notional"]) for t in _trades(EUR_TRADES_FILE)})
    for trade_id, figures in golden["usd_trades"].items():
        for name, value in figures.items():
            tolerance = (
                pytest.approx(value, abs=GOLDEN_PV_PER_NOTIONAL * notionals[trade_id])
                if name == "present_value"
                else pytest.approx(value, rel=GOLDEN_REL)
            )
            assert actual["usd_trades"][trade_id][name] == tolerance, (trade_id, name)

    # P&L figures are differences of PVs: compared per unit of notional.
    for section in ("scenarios", "carry"):
        assert golden[section].keys() == actual[section].keys()
    for trade_id, pnls in golden["scenarios"].items():
        for name, value in pnls.items():
            assert actual["scenarios"][trade_id][name] == pytest.approx(
                value, abs=GOLDEN_PV_PER_NOTIONAL * notionals[trade_id]
            ), (trade_id, name)
    for trade_id, horizons in golden["carry"].items():
        for horizon, figures in horizons.items():
            for name, value in figures.items():
                assert actual["carry"][trade_id][horizon][name] == pytest.approx(
                    value, abs=GOLDEN_PV_PER_NOTIONAL * notionals[trade_id]
                ), (trade_id, horizon, name)
