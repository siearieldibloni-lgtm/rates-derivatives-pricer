"""Phase 2 through HTTP, the SDK and the dashboard: IRS pricing, Euribor curves, risk."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from rate_derivatives_pricer.sdk import (
    InvalidTradeDefinitionError,
    MarketDataInvalidError,
    PricerClient,
    UnsupportedInstrumentError,
    UnsupportedRiskMeasureError,
    ValidationFailedError,
    ois_swap,
    vanilla_irs,
)
from rate_derivatives_pricer.sdk.models import InlineMarketData
from rate_derivatives_pricer.ui import views
from tests.api.conftest import SNAPSHOT
from tests.api.test_ui import APP_SCRIPT, APP_TIMEOUT_SECONDS


def irs_trade(**overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "notional": 10_000_000,
        "fixed_rate": 0.025,
        "direction": "pay_fixed",
        "maturity": "2Y",
    }
    params.update(overrides)
    return {"type": "vanilla_irs", "params": params}


def risk_body(trade: dict[str, Any], measures: list[str], **extra: Any) -> dict[str, Any]:
    return {"market": {"snapshot": SNAPSHOT}, "trade": trade, "measures": measures, **extra}


@pytest.fixture
def sdk(client: TestClient) -> PricerClient:
    return PricerClient(http_client=client)


def inline_market(client: TestClient, with_fixings: bool = True) -> dict[str, Any]:
    """The stored snapshot passed by value, optionally without its fixings."""
    snapshot = client.get(f"/v1/market-snapshots/{SNAPSHOT}").json()["result"]
    return {
        "inline": {
            "valuation_date": snapshot["valuation_date"],
            "quotes": snapshot["quotes"],
            "fixings": snapshot["fixings"] if with_fixings else {},
        }
    }


# --------------------------------------------------------------------------- curves and pricing
def test_curves_build_returns_the_overnight_curve_then_one_curve_per_index(
    client: TestClient,
) -> None:
    body = client.post("/v1/curves/build", json={"market": {"snapshot": SNAPSHOT}}).json()
    curves = body["result"]["curves"]
    assert [c["curve_name"] for c in curves] == ["EUR-ESTR", "EUR-EURIBOR3M", "EUR-EURIBOR6M"]
    assert [c["role"] for c in curves] == ["discount_and_projection", "projection", "projection"]
    assert all(c["max_abs_residual"] < 1e-13 for c in curves)
    euribor_6m = curves[2]
    assert [n["label"] for n in euribor_6m["nodes"]][:3] == ["6M", "1Y", "2Y"]
    assert euribor_6m["nodes"][0]["instrument_type"] == "ibor_deposit"
    assert euribor_6m["grid"][0]["discount_factor"] == 1.0


def test_price_a_vanilla_irs(client: TestClient) -> None:
    response = client.post(
        "/v1/price", json={"market": {"snapshot": SNAPSHOT}, "trade": irs_trade()}
    )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["instrument_type"] == "vanilla_irs"
    assert [leg["name"] for leg in result["legs"]] == ["fixed", "floating"]
    assert result["present_value"] == pytest.approx(
        sum(leg["present_value"] for leg in result["legs"])
    )
    assert 0.02 < result["par_rate"] < 0.03
    assert len([c for c in result["cashflows"] if c["leg"] == "floating"]) == 4
    # The 3M index pays quarterly: twice as many floating coupons, a different par rate.
    quarterly = client.post(
        "/v1/price",
        json={"market": {"snapshot": SNAPSHOT}, "trade": irs_trade(index="EURIBOR3M")},
    ).json()["result"]
    assert len([c for c in quarterly["cashflows"] if c["leg"] == "floating"]) == 8
    assert quarterly["par_rate"] != result["par_rate"]


def test_irs_validation_and_domain_errors(client: TestClient) -> None:
    def post(trade: dict[str, Any]) -> Any:
        return client.post("/v1/price", json={"market": {"snapshot": SNAPSHOT}, "trade": trade})

    unknown_index = post(irs_trade(index="EURIBOR12M"))
    assert unknown_index.status_code == 422
    assert unknown_index.json()["error"]["code"] == "INVALID_TRADE_DEFINITION"
    assert (
        post(irs_trade(maturity=None, end_date=None)).json()["error"]["code"] == "VALIDATION_ERROR"
    )
    typo = post(irs_trade(fixedrate=0.02))
    assert typo.json()["error"]["code"] == "VALIDATION_ERROR"
    # A swap that started in the past needs the fixings of its coupons that are not yet paid.
    seasoned = client.post(
        "/v1/price",
        json={
            "market": inline_market(client, with_fixings=False),
            "trade": irs_trade(start_date="2026-04-21", maturity="5Y"),
        },
    )
    assert seasoned.status_code == 422
    assert seasoned.json()["error"]["code"] == "MARKET_DATA_INVALID"
    assert seasoned.json()["error"]["details"]["index"] == "EURIBOR6M"


def test_inline_market_with_ibor_quotes(client: TestClient) -> None:
    inline = client.post(
        "/v1/price", json={"market": inline_market(client), "trade": irs_trade()}
    ).json()
    stored = client.post(
        "/v1/price", json={"market": {"snapshot": SNAPSHOT}, "trade": irs_trade()}
    ).json()
    assert inline["result"]["present_value"] == stored["result"]["present_value"]
    # Same data by value or by reference: same fingerprint, so the second call hits the cache.
    assert (
        inline["diagnostics"]["input_hashes"]["market"]
        == (stored["diagnostics"]["input_hashes"]["market"])
    )
    assert (inline["diagnostics"]["curve_cache"], stored["diagnostics"]["curve_cache"]) == (
        "miss",
        "hit",
    )


def test_quote_index_is_required_for_ibor_quotes_and_forbidden_otherwise(
    client: TestClient,
) -> None:
    def build(quote: dict[str, Any]) -> Any:
        market = {"inline": {"valuation_date": "2026-10-19", "quotes": [quote]}}
        return client.post("/v1/curves/build", json={"market": market})

    missing = build({"type": "ibor_swap", "tenor": "2Y", "value": 2.0, "unit": "percent"})
    extra = build(
        {"type": "ois_swap", "tenor": "2Y", "value": 2.0, "unit": "percent", "index": "EURIBOR6M"}
    )
    assert missing.status_code == extra.status_code == 422
    assert missing.json()["error"]["code"] == extra.json()["error"]["code"] == "VALIDATION_ERROR"


# --------------------------------------------------------------------------- risk
def test_risk_endpoint_returns_the_envelope_and_the_measures(client: TestClient) -> None:
    response = client.post(
        "/v1/risk",
        json=risk_body(
            irs_trade(maturity="1Y"),
            ["parallel_dv01", "curve_dv01", "convexity"],
            bump={"size_bp": 1.0, "mode": "central"},
        ),
        headers={"X-Request-ID": "risk-test-1"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["request_id"] == "risk-test-1"
    result = body["result"]
    assert (result["instrument_type"], result["currency"]) == ("vanilla_irs", "EUR")
    assert result["bump"] == {"size_bp": 1.0, "mode": "central"}
    measures = {m["measure"]: m for m in result["measures"]}
    assert list(measures) == ["parallel_dv01", "curve_dv01", "convexity"]
    assert measures["parallel_dv01"]["unit"] == "currency per bp"
    assert measures["convexity"]["unit"] == "currency per bp^2"
    curve_buckets = {b["curve"]: b["value"] for b in measures["curve_dv01"]["buckets"]}
    assert set(curve_buckets) == {"OIS", "EURIBOR3M", "EURIBOR6M"}
    assert sum(curve_buckets.values()) == pytest.approx(
        measures["parallel_dv01"]["value"], rel=1e-6
    )
    diagnostics = body["diagnostics"]
    assert {"market_data", "risk", "total"} <= set(diagnostics["timings_ms"])
    assert set(diagnostics["input_hashes"]) == {"market", "trade"}


def test_key_rate_buckets_over_http(sdk: PricerClient) -> None:
    risk = sdk.risk(SNAPSHOT, vanilla_irs(1e7, 0.025, "pay_fixed", "1Y"), ["key_rate_dv01"])
    measure = risk.result.measures[0]
    assert measure.measure == "key_rate_dv01"
    assert measure.bump_count == 2 * len(measure.buckets)
    assert [b.curve for b in measure.buckets][:3] == ["OIS", "OIS", "OIS"]
    assert measure.buckets[0].label == "ON"
    exposed = {(b.curve, b.label): b.value for b in measure.buckets}
    assert exposed[("EURIBOR6M", "1Y")] > 0
    assert exposed[("EURIBOR6M", "30Y")] == 0.0
    assert measure.value == pytest.approx(sum(b.value for b in measure.buckets))


def test_risk_of_an_ois_swap_over_http(sdk: PricerClient) -> None:
    risk = sdk.risk(
        SNAPSHOT,
        ois_swap(1e7, 0.02, "pay_fixed", "2Y"),
        ["parallel_dv01", "curve_dv01"],
        {"size_bp": 0.5, "mode": "up"},
    )
    assert risk.result.bump.mode == "up"
    parallel, by_curve = risk.result.measures
    assert parallel.value > 0
    assert {b.curve: b.value for b in by_curve.buckets}["EURIBOR6M"] == 0.0


def _inline_data(client: TestClient, with_fixings: bool = True) -> InlineMarketData:
    return InlineMarketData.model_validate(inline_market(client, with_fixings)["inline"])


def test_risk_errors(client: TestClient, sdk: PricerClient) -> None:
    unknown = client.post("/v1/risk", json=risk_body(irs_trade(), ["vega"]))
    assert unknown.status_code == 400
    assert unknown.json()["error"]["code"] == "UNSUPPORTED_RISK_MEASURE"
    assert "key_rate_dv01" in unknown.json()["error"]["details"]["available"]
    empty = client.post("/v1/risk", json=risk_body(irs_trade(), []))
    assert empty.status_code == 422
    assert empty.json()["error"]["code"] == "VALIDATION_ERROR"
    bad_bump = client.post(
        "/v1/risk", json=risk_body(irs_trade(), ["parallel_dv01"], bump={"size_bp": 0})
    )
    assert bad_bump.json()["error"]["code"] == "VALIDATION_ERROR"
    unsupported = client.post(
        "/v1/risk",
        json=risk_body({"type": "swaption", "params": {}}, ["parallel_dv01"]),
    )
    assert unsupported.json()["error"]["code"] == "UNSUPPORTED_INSTRUMENT"
    missing_fixing = client.post(
        "/v1/risk",
        json={
            "market": inline_market(client, with_fixings=False),
            "trade": irs_trade(start_date="2026-04-21", maturity="5Y"),
            "measures": ["parallel_dv01"],
        },
    )
    assert missing_fixing.status_code == 422
    assert missing_fixing.json()["error"]["code"] == "MARKET_DATA_INVALID"

    with pytest.raises(UnsupportedRiskMeasureError) as error:
        sdk.risk(SNAPSHOT, vanilla_irs(1e7, 0.025, "pay_fixed", "1Y"), ["vega"])
    assert error.value.status_code == 400
    with pytest.raises(UnsupportedInstrumentError):
        sdk.risk(SNAPSHOT, {"type": "swaption", "params": {}}, ["parallel_dv01"])
    with pytest.raises(InvalidTradeDefinitionError):
        sdk.risk(SNAPSHOT, vanilla_irs(1e7, 0.025, "pay_fixed", "1Y", index="NOPE"), ["convexity"])
    with pytest.raises(MarketDataInvalidError):
        sdk.risk(
            _inline_data(client, with_fixings=False),
            vanilla_irs(1e7, 0.025, "pay_fixed", "5Y", start_date=dt.date(2026, 4, 21)),
            ["convexity"],
        )
    with pytest.raises(ValidationFailedError):
        sdk.risk(SNAPSHOT, vanilla_irs(1e7, 0.025, "pay_fixed", "1Y"), [])


def test_risk_does_not_pollute_the_curve_cache(client: TestClient) -> None:
    service = client.app.state.service  # type: ignore[attr-defined]
    before = service.cache_stats()
    client.post("/v1/risk", json=risk_body(irs_trade(maturity="1Y"), ["parallel_dv01"]))
    after = service.cache_stats()
    assert (after.size, after.hits, after.misses) == (before.size, before.hits, before.misses)


def test_risk_audit_log(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="rate_derivatives_pricer"):
        client.post("/v1/risk", json=risk_body(irs_trade(maturity="1Y"), ["parallel_dv01"]))
    audit = next(r for r in caplog.records if r.getMessage() == "calculation_audit")
    assert audit.calculation == "risk"  # type: ignore[attr-defined]
    assert audit.risk_options["measures"] == ["parallel_dv01"]  # type: ignore[attr-defined]
    assert len(audit.market_hash) == 64  # type: ignore[attr-defined]
    assert "parallel_dv01" in audit.risk_values  # type: ignore[attr-defined]
    completed = [r for r in caplog.records if r.getMessage() == "risk_completed"]
    assert len(completed) == 1
    assert completed[0].valuations == 3  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- SDK and dashboard
def test_sdk_builds_inline_markets_for_risk(sdk: PricerClient, client: TestClient) -> None:
    inline = _inline_data(client)
    by_value = sdk.risk(inline, vanilla_irs(1e7, 0.025, "pay_fixed", "1Y"), ["parallel_dv01"])
    by_name = sdk.risk(SNAPSHOT, vanilla_irs(1e7, 0.025, "pay_fixed", "1Y"), ["parallel_dv01"])
    assert by_value.result.measures[0].value == by_name.result.measures[0].value


def test_views_format_risk_results(sdk: PricerClient) -> None:
    curves = sdk.build_curves(SNAPSHOT).result.curves
    assert [row["curve"] for row in views.curve_rows(curves)] == [c.curve_name for c in curves]
    risk = sdk.risk(
        SNAPSHOT, vanilla_irs(1e7, 0.025, "pay_fixed", "1Y"), ["key_rate_dv01", "parallel_dv01"]
    ).result
    key_rate = risk.measures[0]
    figure = views.risk_figure(key_rate)
    assert {trace.name for trace in figure.data} == {"OIS", "EURIBOR3M", "EURIBOR6M"}
    rows = views.risk_bucket_rows(key_rate)
    assert len(rows) == len(key_rate.buckets)
    assert rows[0]["bucket"] == "ON"
    summary = views.risk_summary(risk)
    assert summary["PV"].endswith("EUR")
    assert summary["parallel_dv01"].endswith("currency per bp")


def test_dashboard_prices_an_irs_and_computes_its_risk(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(views, "make_client", lambda base_url: PricerClient(http_client=client))
    app = AppTest.from_file(str(APP_SCRIPT), default_timeout=APP_TIMEOUT_SECONDS).run()
    assert not app.exception
    assert app.selectbox(key="curve").options == ["EUR-ESTR", "EUR-EURIBOR3M", "EUR-EURIBOR6M"]

    app.selectbox(key="curve").select("EUR-EURIBOR6M").run()
    assert "EUR-EURIBOR6M" in app.subheader[0].value

    app.selectbox(key="trade_type").select("vanilla_irs")
    app.selectbox(key="index").select("EURIBOR6M")
    app.multiselect(key="measures").set_value(["parallel_dv01", "curve_dv01"])
    app.button(key="price").click().run()
    assert not app.exception
    assert not app.error
    assert {"PV", "Par rate", "Annuity", "PVBP"} <= {metric.label for metric in app.metric}

    app.button(key="risk").click().run()
    assert not app.exception
    assert not app.error
    assert {"parallel_dv01", "curve_dv01"} <= {metric.label for metric in app.metric}
