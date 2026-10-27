"""Phase 3 through HTTP and the SDK: scenarios, carry/roll-down and USD by configuration."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from rate_derivatives_pricer.sdk import (
    InvalidTradeDefinitionError,
    PricerClient,
    UnsupportedRiskMeasureError,
    UnsupportedScenarioError,
    ValidationFailedError,
    ois_swap,
    scenario,
    vanilla_irs,
)
from rate_derivatives_pricer.ui import views
from tests.api.conftest import SNAPSHOT, ois_trade
from tests.api.test_ui import APP_SCRIPT, APP_TIMEOUT_SECONDS

USD_SNAPSHOT = "USD_2026-10-19"
PRESETS = ["parallel_up", "parallel_down", "steepener", "flattener", "twist"]


def irs_trade(**overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "notional": 10_000_000,
        "fixed_rate": 0.025,
        "direction": "pay_fixed",
        "maturity": "5Y",
    }
    params.update(overrides)
    return {"type": "vanilla_irs", "params": params}


@pytest.fixture
def sdk(client: TestClient) -> PricerClient:
    return PricerClient(http_client=client)


# --------------------------------------------------------------------------- catalog and USD
def test_catalog_lists_usd_and_preset_scenarios(sdk: PricerClient) -> None:
    catalog = sdk.catalog().result
    currencies = {c.currency: c for c in catalog.currencies}
    assert currencies["USD"].overnight_index == "SOFR"
    assert currencies["USD"].calendar == "US_SOFR"
    assert currencies["USD"].ibor_indices == []
    assert {"US_GOVERNMENT_BOND", "US_SOFR"} <= set(catalog.calendars)
    assert [s.name for s in catalog.scenarios] == PRESETS
    steepener = next(s for s in catalog.scenarios if s.name == "steepener")
    assert [(p.tenor, p.shift_bp) for p in steepener.points] == [("2Y", -25.0), ("10Y", 25.0)]


def test_usd_snapshot_is_listed_and_priced(sdk: PricerClient) -> None:
    assert {SNAPSHOT, "EUR_2026-10-20", USD_SNAPSHOT} <= set(sdk.list_snapshots())
    curves = sdk.build_curves(USD_SNAPSHOT).result
    assert [c.curve_name for c in curves.curves] == ["USD-SOFR"]
    priced = sdk.price(USD_SNAPSHOT, ois_swap(1e7, 0.033, "pay_fixed", "5Y", currency="USD"))
    assert priced.result.currency == "USD"
    # Payment lag of 2 business days on the US SOFR calendar (ADR-032).
    first = priced.result.cashflows[0]
    assert first.payment_date == dt.date(2027, 10, 25)
    assert first.accrual_end == dt.date(2027, 10, 21)


def test_usd_trades_are_refused_on_a_eur_market_and_irs_in_usd(sdk: PricerClient) -> None:
    with pytest.raises(InvalidTradeDefinitionError):
        sdk.price(SNAPSHOT, ois_swap(1e7, 0.033, "pay_fixed", "5Y", currency="USD"))
    with pytest.raises(InvalidTradeDefinitionError):
        sdk.price(USD_SNAPSHOT, vanilla_irs(1e7, 0.033, "pay_fixed", "5Y", currency="USD"))


def test_usd_risk_by_configuration(sdk: PricerClient) -> None:
    risk = sdk.risk(
        USD_SNAPSHOT,
        ois_swap(1e7, 0.033, "pay_fixed", "5Y", currency="USD"),
        ["parallel_dv01", "curve_dv01"],
    ).result
    parallel, by_curve = risk.measures
    assert parallel.value > 0.0  # payer gains when rates rise
    assert [b.label for b in by_curve.buckets] == ["OIS"]
    assert by_curve.value == pytest.approx(parallel.value, rel=1e-12)


# --------------------------------------------------------------------------- scenarios
def test_scenarios_endpoint_runs_every_preset_by_default(client: TestClient) -> None:
    response = client.post(
        "/v1/risk/scenarios", json={"market": {"snapshot": SNAPSHOT}, "trade": irs_trade()}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    result = body["result"]
    assert [s["name"] for s in result["scenarios"]] == PRESETS
    assert body["diagnostics"]["curve_cache"] is None
    assert set(body["diagnostics"]["timings_ms"]) == {"market_data", "scenarios", "total"}
    by_name = {s["name"]: s for s in result["scenarios"]}
    # A payer swap gains when rates rise and loses when they fall.
    assert by_name["parallel_up"]["pnl"] > 0 > by_name["parallel_down"]["pnl"]
    for entry in result["scenarios"]:
        assert entry["pnl"] == pytest.approx(entry["present_value"] - result["present_value"])
        assert len(entry["shifts"]) == 56  # every calibration quote of the snapshot
    twist = {(s["curve"], s["label"]): s["shift_bp"] for s in by_name["twist"]["shifts"]}
    assert twist[("OIS", "5Y")] == 0.0
    assert twist[("EURIBOR6M", "30Y")] == 25.0


def test_scenarios_with_presets_and_custom_shapes_over_the_sdk(sdk: PricerClient) -> None:
    custom = scenario("estr_only_up", {"1Y": 10.0}, curves=["OIS"])
    shape = {
        "name": "bear_steepener",
        "points": [{"tenor": "2Y", "shift_bp": 0.0}, {"tenor": "10Y", "shift_bp": 30.0}],
    }
    result = sdk.scenarios(
        SNAPSHOT, vanilla_irs(1e7, 0.025, "pay_fixed", "10Y"), ["steepener"], [custom, shape]
    ).result
    assert [s.name for s in result.scenarios] == ["steepener", "estr_only_up", "bear_steepener"]
    estr_only = result.scenarios[1]
    assert estr_only.curves == ["OIS"]
    assert {s.shift_bp for s in estr_only.shifts if s.curve != "OIS"} == {0.0}
    assert result.scenarios[2].pnl > 0.0  # payer 10Y gains when the long end rises


def test_scenario_errors(client: TestClient, sdk: PricerClient) -> None:
    with pytest.raises(UnsupportedScenarioError) as unknown:
        sdk.scenarios(SNAPSHOT, ois_swap(1e7, 0.02, "pay_fixed", "2Y"), ["sideways"])
    assert unknown.value.status_code == 400
    assert unknown.value.code == "UNSUPPORTED_SCENARIO"
    assert unknown.value.details["available"] == PRESETS
    # Subclass of the risk measure error: generic handlers keep working.
    assert isinstance(unknown.value, UnsupportedRiskMeasureError)

    duplicate = scenario("steepener", {"1Y": 1.0})
    with pytest.raises(UnsupportedScenarioError):
        sdk.scenarios(SNAPSHOT, ois_swap(1e7, 0.02, "pay_fixed", "2Y"), ["steepener"], [duplicate])
    with pytest.raises(UnsupportedScenarioError):
        sdk.scenarios(
            SNAPSHOT,
            ois_swap(1e7, 0.02, "pay_fixed", "2Y"),
            [],
            [scenario("bad_tenor", {"1Q": 1.0})],
        )
    with pytest.raises(UnsupportedScenarioError):
        sdk.scenarios(
            SNAPSHOT,
            ois_swap(1e7, 0.02, "pay_fixed", "2Y"),
            [],
            [scenario("no_curve", {"1Y": 1.0}, curves=["SOFR"])],
        )
    empty_points = {
        "market": {"snapshot": SNAPSHOT},
        "trade": ois_trade(),
        "custom_scenarios": [{"name": "empty", "points": []}],
    }
    response = client.post("/v1/risk/scenarios", json=empty_points)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_scenarios_audit_log(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        client.post(
            "/v1/risk/scenarios",
            json={"market": {"snapshot": SNAPSHOT}, "trade": ois_trade(), "scenarios": ["twist"]},
        )
    audits = [r for r in caplog.records if r.getMessage() == "calculation_audit"]
    assert audits[-1].calculation == "scenarios"  # type: ignore[attr-defined]
    assert set(audits[-1].scenario_pnl) == {"twist"}  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- carry / roll-down
def test_carry_endpoint(client: TestClient) -> None:
    response = client.post(
        "/v1/risk/carry",
        json={"market": {"snapshot": SNAPSHOT}, "trade": irs_trade(), "horizon": "6M"},
    )
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["hypothesis"] == "unchanged_curve"
    assert result["horizon"] == "6M"
    assert result["horizon_date"] == "2027-04-19"
    assert set(result["assumed_fixings"]) == {"ESTR", "EURIBOR6M", "EURIBOR3M"}
    assert result["total"] == pytest.approx(result["carry"] + result["rolldown"])
    assert result["total"] == pytest.approx(
        result["horizon_present_value"] + result["cashflows"] - result["present_value"]
    )


def test_carry_over_the_sdk_in_usd(sdk: PricerClient) -> None:
    carry = sdk.carry_rolldown(
        USD_SNAPSHOT, ois_swap(1e7, 0.033, "receive_fixed", "5Y", currency="USD"), "1M"
    ).result
    assert carry.currency == "USD"
    assert carry.assumed_fixings == {"SOFR": pytest.approx(0.0362)}
    # Receiving 3.3% fixed against SOFR at 3.62%: negative carry.
    assert carry.carry < 0.0


def test_carry_errors(client: TestClient) -> None:
    body = {"market": {"snapshot": SNAPSHOT}, "trade": ois_trade(), "horizon": "0M"}
    response = client.post("/v1/risk/carry", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    snapshot = client.get(f"/v1/market-snapshots/{SNAPSHOT}").json()["result"]
    no_deposit = {
        "inline": {
            "valuation_date": snapshot["valuation_date"],
            "quotes": [q for q in snapshot["quotes"] if q["type"] == "ois_swap"],
            "fixings": snapshot["fixings"],
        }
    }
    response = client.post(
        "/v1/risk/carry", json={"market": no_deposit, "trade": ois_trade(maturity="2Y")}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MARKET_DATA_INVALID"


def test_carry_validation_error_type_on_the_sdk(sdk: PricerClient) -> None:
    with pytest.raises(ValidationFailedError):
        sdk.carry_rolldown(SNAPSHOT, ois_swap(1e7, 0.02, "pay_fixed", "2Y"), "three months")


# --------------------------------------------------------------------------- dashboard
def test_views_format_scenarios_carry_and_comparisons(sdk: PricerClient) -> None:
    trade = vanilla_irs(1e7, 0.025, "pay_fixed", "5Y")
    shocked = sdk.scenarios(SNAPSHOT, trade).result
    rows = views.scenario_rows(shocked)
    assert [row["scenario"] for row in rows] == PRESETS
    assert len(views.scenario_pnl_figure(shocked).data[0].x) == len(PRESETS)
    matrix = views.scenario_shift_matrix(shocked).data[0]
    assert list(matrix.y) == PRESETS
    assert matrix.x[0] == "ON"
    assert matrix.z[0][0] == 100.0  # parallel_up at ON

    carried = sdk.carry_rolldown(SNAPSHOT, trade, "3M").result
    summary = views.carry_summary(carried)
    assert summary["Horizon"] == "3M (2027-01-19)"
    assert list(views.carry_figure(carried).data[0].x) == ["Carry", "Roll-down", "Total"]

    base = sdk.price(SNAPSHOT, trade).result
    other = sdk.price("EUR_2026-10-20", trade).result
    comparison = views.snapshot_comparison_rows(base, other, SNAPSHOT, "EUR_2026-10-20")
    pv_row = next(row for row in comparison if row["figure"] == "PV")
    assert pv_row["change"] == pytest.approx(other.present_value - base.present_value)
    figure = views.curve_change_figure(
        sdk.build_curves(SNAPSHOT).result.curves,
        sdk.build_curves("EUR_2026-10-20").result.curves,
        SNAPSHOT,
        "EUR_2026-10-20",
    )
    assert [trace.name for trace in figure.data] == ["EUR-ESTR", "EUR-EURIBOR3M", "EUR-EURIBOR6M"]
    # The synthetic next-day market moved every quote by +0.5bp to +2bp (ADR-033).
    assert all(0.4 <= y <= 2.1 for trace in figure.data for y in trace.y)


def test_dashboard_runs_scenarios_carry_and_comparison(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(views, "make_client", lambda base_url: PricerClient(http_client=client))
    app = AppTest.from_file(str(APP_SCRIPT), default_timeout=APP_TIMEOUT_SECONDS).run()
    assert not app.exception
    app.selectbox(key="trade_type").select("vanilla_irs")
    app.multiselect(key="scenario_names").set_value(["parallel_up", "twist"])
    app.button(key="scenarios").click().run()
    assert not app.exception
    assert not app.error
    assert "Base PV" in {metric.label for metric in app.metric}

    app.selectbox(key="horizon").select("6M")
    app.button(key="carry").click().run()
    assert not app.exception
    assert not app.error
    assert {"Carry", "Roll-down", "Total"} <= {metric.label for metric in app.metric}

    assert app.selectbox(key="other_snapshot").options == ["EUR_2026-10-20"]
    app.button(key="compare").click().run()
    assert not app.exception
    assert not app.error


def test_dashboard_switches_to_usd(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(views, "make_client", lambda base_url: PricerClient(http_client=client))
    app = AppTest.from_file(str(APP_SCRIPT), default_timeout=APP_TIMEOUT_SECONDS).run()
    app.selectbox(key="snapshot").select(USD_SNAPSHOT).run()
    assert not app.exception
    assert app.selectbox(key="curve").options == ["USD-SOFR"]
    assert app.selectbox(key="trade_type").options == ["ois_swap"]
    app.button(key="price").click().run()
    assert not app.exception
    assert not app.error
    pv = next(metric for metric in app.metric if metric.label == "PV")
    assert pv.value.endswith("USD")
    app.button(key="compare").click().run()
    assert app.warning  # no other USD snapshot
