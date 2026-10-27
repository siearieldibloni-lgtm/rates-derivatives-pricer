from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rate_derivatives_pricer.api.logging_config import configure_logging
from rate_derivatives_pricer.api.main import create_app
from rate_derivatives_pricer.api.settings import Settings
from tests.api.conftest import MARKET_DIR, SNAPSHOT, ois_trade

PRICE_BODY = {"market": {"snapshot": SNAPSHOT}, "trade": ois_trade()}


@contextmanager
def json_logs() -> Iterator[io.StringIO]:
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    buffer = io.StringIO()
    try:
        configure_logging("DEBUG", "json", buffer)
        yield buffer
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


# --------------------------------------------------------------------------- probes & metadata
def test_health_and_ready(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok", "checks": {}}
    ready = client.get("/ready").json()
    assert ready["status"] == "ready"
    assert all(ready["checks"].values())


def test_ready_reports_missing_components(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    service = app.state.service
    monkeypatch.setattr(service, "readiness", lambda: {"conventions": False})
    with TestClient(app) as client:
        response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"


def test_version(client: TestClient) -> None:
    body = client.get("/v1/version").json()
    assert body["request_id"]
    assert set(body["result"]) == {"api", "core", "python", "numpy", "commit"}
    assert body["diagnostics"]["versions"]["core"] == body["result"]["core"]


def test_catalog_is_generated_from_registries(client: TestClient) -> None:
    result = client.get("/v1/catalog").json()["result"]
    assert result["instrument_types"] == ["ois_swap", "vanilla_irs"]
    assert result["risk_measures"] == [
        "parallel_dv01",
        "key_rate_dv01",
        "curve_dv01",
        "convexity",
    ]
    assert {"log_linear_df", "linear_zero"} <= set(result["interpolations"])
    assert result["currencies"][0]["currency"] == "EUR"
    assert result["currencies"][0]["overnight_index"] == "ESTR"
    assert result["currencies"][0]["ibor_indices"] == ["EURIBOR6M", "EURIBOR3M"]
    assert result["pricing_methods"] == [
        {"instrument_type": "ois_swap", "method": "discounting"},
        {"instrument_type": "vanilla_irs", "method": "discounting"},
    ]
    assert {"notional", "fixed_rate", "direction"} <= set(
        result["trade_schemas"]["vanilla_irs"]["required"]
    )
    schema = result["trade_schemas"]["ois_swap"]
    assert {"notional", "fixed_rate", "direction"} <= set(schema["required"])


# --------------------------------------------------------------------------- market data
def test_market_snapshots(client: TestClient) -> None:
    assert SNAPSHOT in client.get("/v1/market-snapshots").json()["result"]["snapshots"]
    content = client.get(f"/v1/market-snapshots/{SNAPSHOT}").json()["result"]
    assert content["valuation_date"] == "2026-10-19"
    assert content["quotes"][0] == {
        "type": "deposit",
        "tenor": "ON",
        "value": 1.921,
        "unit": "percent",
        "index": None,
    }
    ibor_swap = next(q for q in content["quotes"] if q["type"] == "ibor_swap")
    assert ibor_swap["index"] in {"EURIBOR6M", "EURIBOR3M"}
    assert content["fixings"]["ESTR"]
    assert content["fixings"]["EURIBOR6M"]


def test_unknown_snapshot_is_404(client: TestClient) -> None:
    response = client.get("/v1/market-snapshots/EUR_1999-01-01")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
    # A name that is not a safe file stem is rejected before reaching the provider.
    assert client.get("/v1/market-snapshots/..%2Fsecret").status_code in {404, 422}


# --------------------------------------------------------------------------- calculations
def test_build_curves(client: TestClient) -> None:
    response = client.post("/v1/curves/build", json={"market": {"snapshot": SNAPSHOT}})
    assert response.status_code == 200
    body = response.json()
    curve = body["result"]["curves"][0]
    assert curve["curve_name"] == "EUR-ESTR"
    assert len(curve["nodes"]) == 24
    assert curve["max_abs_residual"] < 1e-12
    assert curve["grid"][0]["discount_factor"] == 1.0
    assert body["diagnostics"]["curve_cache"] == "miss"
    assert set(body["diagnostics"]["timings_ms"]) >= {"market_data", "curve_build", "total"}
    assert body["diagnostics"]["input_hashes"]["market"]


def test_price_ois_swap(client: TestClient) -> None:
    body = client.post("/v1/price", json=PRICE_BODY).json()
    result = body["result"]
    assert result["instrument_type"] == "ois_swap"
    assert result["par_rate"] == pytest.approx(0.02254, abs=1e-12)
    # Pay 2.25% vs a 2.254% par rate: small positive PV.
    assert result["present_value"] == pytest.approx(
        (result["par_rate"] - 0.0225) * result["annuity"], rel=1e-9
    )
    assert [leg["name"] for leg in result["legs"]] == ["fixed", "overnight"]
    assert len(result["cashflows"]) == 10
    assert body["diagnostics"]["warnings"] == []
    assert set(body["diagnostics"]["input_hashes"]) == {"market", "trade"}


def test_curve_cache_hit_on_identical_market(client: TestClient) -> None:
    first = client.post("/v1/price", json=PRICE_BODY).json()
    second = client.post(
        "/v1/price", json={**PRICE_BODY, "trade": ois_trade(maturity="10Y")}
    ).json()
    assert first["diagnostics"]["curve_cache"] == "miss"
    assert second["diagnostics"]["curve_cache"] == "hit"
    assert (
        first["diagnostics"]["input_hashes"]["market"]
        == second["diagnostics"]["input_hashes"]["market"]
    )
    other_interpolation = client.post(
        "/v1/price", json={**PRICE_BODY, "options": {"interpolation": "linear_zero"}}
    ).json()
    assert other_interpolation["diagnostics"]["curve_cache"] == "miss"


def test_inline_market_matches_the_stored_snapshot(client: TestClient) -> None:
    snapshot = client.get(f"/v1/market-snapshots/{SNAPSHOT}").json()["result"]
    inline = {
        "valuation_date": snapshot["valuation_date"],
        "quotes": snapshot["quotes"],
        "fixings": snapshot["fixings"],
    }
    by_value = client.post("/v1/price", json={**PRICE_BODY, "market": {"inline": inline}}).json()
    by_reference = client.post("/v1/price", json=PRICE_BODY).json()
    assert by_value["result"]["present_value"] == by_reference["result"]["present_value"]
    # Same data under another name: same fingerprint, served from the cache.
    assert by_reference["diagnostics"]["curve_cache"] == "hit"


# --------------------------------------------------------------------------- errors
@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ({"market": {"snapshot": SNAPSHOT}}, 422, "VALIDATION_ERROR"),
        ({**PRICE_BODY, "trade": {"type": "cap", "params": {}}}, 400, "UNSUPPORTED_INSTRUMENT"),
        ({**PRICE_BODY, "trade": ois_trade(notional=-1)}, 422, "VALIDATION_ERROR"),
        ({**PRICE_BODY, "trade": ois_trade(maturity=None)}, 422, "VALIDATION_ERROR"),
        ({**PRICE_BODY, "trade": ois_trade(unknown_field=1)}, 422, "VALIDATION_ERROR"),
        ({**PRICE_BODY, "trade": ois_trade(maturity="5 years")}, 422, "INVALID_TRADE_DEFINITION"),
        ({**PRICE_BODY, "trade": ois_trade(currency="XXX")}, 422, "INVALID_TRADE_DEFINITION"),
        ({**PRICE_BODY, "market": {"snapshot": "EUR_1999-01-01"}}, 404, "NOT_FOUND"),
        ({**PRICE_BODY, "options": {"interpolation": "cubic"}}, 422, "INVALID_TRADE_DEFINITION"),
        (
            {**PRICE_BODY, "trade": ois_trade(start_date="2025-12-15", maturity="2Y")},
            422,
            "MARKET_DATA_INVALID",  # no fixings before 2026-01-02 in the snapshot
        ),
    ],
)
def test_structured_errors(
    client: TestClient, body: dict[str, object], status: int, code: str
) -> None:
    response = client.post("/v1/price", json=body)
    assert response.status_code == status, response.json()
    payload = response.json()
    assert payload["error"]["code"] == code
    assert payload["request_id"] == response.headers["X-Request-ID"]


def test_calibration_failure_reports_the_pillar(client: TestClient) -> None:
    snapshot = client.get(f"/v1/market-snapshots/{SNAPSHOT}").json()["result"]
    quotes = [{**q, "value": 5000.0} if q["tenor"] == "2Y" else q for q in snapshot["quotes"]]
    inline = {"valuation_date": snapshot["valuation_date"], "quotes": quotes}
    response = client.post("/v1/curves/build", json={"market": {"inline": inline}})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "CALIBRATION_FAILED"
    assert error["details"]["pillar"] == "2Y"


def test_unknown_route_and_method(client: TestClient) -> None:
    assert client.get("/v2/price").json()["error"]["code"] == "NOT_FOUND"
    response = client.get("/v1/price")
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "METHOD_NOT_ALLOWED"


def test_unexpected_errors_are_generic_and_logged(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object) -> None:
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(app.state.service, "price", boom)
    with json_logs() as logs, TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/v1/price", json=PRICE_BODY)
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert "secret" not in response.text
    records = [json.loads(line) for line in logs.getvalue().splitlines()]
    failure = next(r for r in records if r["event"] == "request_failed")
    assert failure["request_id"] == body["request_id"]
    assert "secret internal detail" in failure["exc_info"]


# --------------------------------------------------------------------------- request_id & logs
def test_request_id_is_propagated_to_core_logs(client: TestClient) -> None:
    with json_logs() as logs:
        response = client.post("/v1/price", json=PRICE_BODY, headers={"X-Request-ID": "support-42"})
    assert response.headers["X-Request-ID"] == "support-42"
    assert response.json()["request_id"] == "support-42"
    records = [json.loads(line) for line in logs.getvalue().splitlines()]
    events = {r["event"]: r for r in records}
    # Emitted by core (bootstrap, engine), services (audit) and api (access log).
    for event in (
        "curve_build_completed",
        "pricing_completed",
        "calculation_audit",
        "request_completed",
    ):
        assert events[event]["request_id"] == "support-42", event
    audit = events["calculation_audit"]
    assert audit["market_hash"] == response.json()["diagnostics"]["input_hashes"]["market"]
    assert audit["logger"] == "services.pricer_service"
    assert events["request_completed"]["status_code"] == 200


def test_invalid_request_id_header_is_replaced(client: TestClient) -> None:
    response = client.get("/health", headers={"X-Request-ID": "bad id with spaces"})
    assert response.headers["X-Request-ID"] != "bad id with spaces"
    assert len(response.headers["X-Request-ID"]) == 32


def test_create_app_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MARKET_DATA_DIR", str(MARKET_DIR))
    monkeypatch.setenv("CURVE_CACHE_SIZE", "2")
    monkeypatch.setenv("GIT_COMMIT", "abc1234")
    with json_logs():
        app = create_app()
    assert isinstance(app.state.settings, Settings)
    with TestClient(app) as client:
        assert client.get("/v1/version").json()["result"]["commit"] == "abc1234"
    assert app.state.service.cache_stats().max_size == 2
