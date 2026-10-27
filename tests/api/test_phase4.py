"""Phase 4 endpoints: batch pricing, curve cache, log timeline, Prometheus metrics."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rate_derivatives_pricer.api.main import create_app
from rate_derivatives_pricer.api.schemas import BATCH_MAX_TRADES
from rate_derivatives_pricer.api.settings import Settings
from tests.api.conftest import MARKET_DIR, SNAPSHOT, ois_trade

TRADES_DIR = Path(__file__).resolve().parents[2] / "data" / "trades"
BATCH_SIZE = 100
MATURITIES = ("1Y", "2Y", "5Y", "10Y", "30Y")


def _batch_of_100() -> list[dict[str, Any]]:
    """100 trades: valid OIS and IRS, and every fourth trade invalid in a different way."""
    invalid: list[dict[str, Any]] = [
        {"type": "cap", "params": {}},  # unknown instrument
        ois_trade(notional=-1.0),  # schema violation
        ois_trade(currency="USD"),  # currency of another market
        ois_trade(start_date="2025-12-15"),  # needs fixings before 2026
        {"type": "vanilla_irs", "params": {**ois_trade()["params"], "index": "EURIBOR12M"}},
    ]
    trades = []
    for position in range(BATCH_SIZE):
        if position % 4 == 3:
            trade = invalid[(position // 4) % len(invalid)]
        else:
            kind = "vanilla_irs" if position % 2 else "ois_swap"
            trade = {
                "type": kind,
                "params": {
                    "notional": 1e6 * (1 + position),
                    "fixed_rate": 0.02 + position * 1e-5,
                    "direction": "pay_fixed" if position % 3 else "receive_fixed",
                    "maturity": MATURITIES[position % len(MATURITIES)],
                },
            }
        trades.append({**trade, "id": f"T{position:03d}"})
    return trades


# --------------------------------------------------------------------------- batch
def test_batch_of_100_trades_reports_errors_per_trade(client: TestClient) -> None:
    trades = _batch_of_100()
    response = client.post(
        "/v1/batch/price", json={"market": {"snapshot": SNAPSHOT}, "trades": trades}
    )
    assert response.status_code == 200
    body = response.json()
    results = body["result"]["results"]
    assert [r["trade_id"] for r in results] == [t["id"] for t in trades]
    failed = {r["trade_id"]: r["error"]["code"] for r in results if r["status"] == "error"}
    assert set(failed) == {t["id"] for i, t in enumerate(trades) if i % 4 == 3}
    assert set(failed.values()) == {
        "UNSUPPORTED_INSTRUMENT",
        "VALIDATION_ERROR",
        "INVALID_TRADE_DEFINITION",
        "MARKET_DATA_INVALID",
    }
    summary = body["result"]["summary"]
    assert (summary["trade_count"], summary["succeeded"], summary["failed"]) == (100, 75, 25)
    ok = [r for r in results if r["status"] == "ok"]
    assert all(r["error"] is None and r["trade_hash"] for r in ok)
    assert summary["total_present_value"] == pytest.approx(
        sum(r["result"]["present_value"] for r in ok), abs=1e-6
    )
    assert body["diagnostics"]["curve_cache"] == "miss"  # one bootstrap for the batch

    # Each priced trade matches the single-trade endpoint exactly.
    for trade, result in list(zip(trades, results, strict=True))[:8]:
        if result["status"] == "ok":
            single = client.post(
                "/v1/price",
                json={
                    "market": {"snapshot": SNAPSHOT},
                    "trade": {k: trade[k] for k in ("type", "params")},
                },
            ).json()
            assert result["result"] == single["result"]
            assert result["trade_hash"] == single["diagnostics"]["input_hashes"]["trade"]


def test_batch_ids_default_to_positions_and_must_be_unique(client: TestClient) -> None:
    body = {"market": {"snapshot": SNAPSHOT}, "trades": [ois_trade(), ois_trade(maturity="2Y")]}
    response = client.post("/v1/batch/price", json=body)
    assert [r["trade_id"] for r in response.json()["result"]["results"]] == ["0", "1"]

    duplicated = [{**ois_trade(), "id": "x"}, {**ois_trade(), "id": "x"}]
    response = client.post(
        "/v1/batch/price", json={"market": {"snapshot": SNAPSHOT}, "trades": duplicated}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("trade_count", [0, BATCH_MAX_TRADES + 1])
def test_batch_size_is_bounded(client: TestClient, trade_count: int) -> None:
    body = {"market": {"snapshot": SNAPSHOT}, "trades": [ois_trade()] * trade_count}
    response = client.post("/v1/batch/price", json=body)
    assert response.status_code == 422


def test_batch_with_an_invalid_market_fails_as_a_whole(client: TestClient) -> None:
    body = {"market": {"snapshot": "EUR_1999-01-01"}, "trades": [ois_trade()]}
    response = client.post("/v1/batch/price", json=body)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_batch_accepts_the_sample_trade_files(client: TestClient) -> None:
    trades = []
    for name in ("ois_samples.json", "irs_samples.json"):
        trades += json.loads((TRADES_DIR / name).read_text(encoding="utf-8"))["trades"]
    response = client.post(
        "/v1/batch/price", json={"market": {"snapshot": SNAPSHOT}, "trades": trades}
    )
    assert response.status_code == 200
    assert response.json()["result"]["summary"]["failed"] == 0


# --------------------------------------------------------------------------- cache
def test_cache_stats_and_clear(client: TestClient) -> None:
    empty = client.get("/v1/cache/stats").json()["result"]
    assert (empty["size"], empty["hit_ratio"]) == (0, None)
    for _ in range(2):
        client.post("/v1/curves/build", json={"market": {"snapshot": SNAPSHOT}})
    stats = client.get("/v1/cache/stats").json()["result"]
    assert (stats["hits"], stats["misses"], stats["size"]) == (1, 1, 1)
    assert stats["hit_ratio"] == pytest.approx(0.5)
    assert stats["max_size"] == 32

    cleared = client.delete("/v1/cache")
    assert cleared.status_code == 200
    assert cleared.json()["result"] == {"entries_dropped": 1}
    assert client.get("/v1/cache/stats").json()["result"]["size"] == 0
    again = client.post("/v1/curves/build", json={"market": {"snapshot": SNAPSHOT}})
    assert again.json()["diagnostics"]["curve_cache"] == "miss"


# --------------------------------------------------------------------------- logs
def test_a_request_id_rebuilds_the_timeline_of_a_calculation(client: TestClient) -> None:
    body = {"market": {"snapshot": SNAPSHOT}, "trade": ois_trade()}
    priced = client.post("/v1/price", json=body, headers={"X-Request-ID": "support-4242"})
    response = client.get("/v1/logs/support-4242")
    assert response.status_code == 200
    timeline = response.json()["result"]
    events = [event["event"] for event in timeline["events"]]
    # Framed by the access log, through bootstrap (core), pricing (core) and audit (services).
    assert events[0] == "request_started"
    assert events[-1] == "request_completed"
    for expected in (
        "curve_build_completed",
        "curves_build_completed",
        "curve_cache_miss",
        "pricing_completed",
        "calculation_audit",
    ):
        assert expected in events
    assert events.index("curve_cache_miss") < events.index("pricing_completed")
    assert {event["request_id"] for event in timeline["events"]} == {"support-4242"}
    timestamps = [event["timestamp"] for event in timeline["events"]]
    assert timestamps == sorted(timestamps)
    assert timeline["event_count"] == len(events)
    assert timeline["duration_ms"] >= 0.0
    audit = next(e for e in timeline["events"] if e["event"] == "calculation_audit")
    assert audit["market_hash"] == priced.json()["diagnostics"]["input_hashes"]["market"]
    assert audit["present_value"] == priced.json()["result"]["present_value"]
    assert timeline["buffer"]["capacity"] == 10_000
    # The lookup itself is another request, with its own id.
    assert response.json()["request_id"] != "support-4242"


def test_error_timelines_carry_no_stack_trace(app: FastAPI) -> None:
    def explode() -> dict[str, bool]:
        raise RuntimeError("secret internal detail")

    app.state.service.readiness = explode
    with TestClient(app, raise_server_exceptions=False) as client:
        failed = client.get("/ready", headers={"X-Request-ID": "crash-1"})
        assert failed.status_code == 500
        events = client.get("/v1/logs/crash-1").json()["result"]["events"]
    failure = next(e for e in events if e["event"] == "request_failed")
    assert failure["exception"] == "RuntimeError"
    assert "secret internal detail" not in json.dumps(events)


def test_unknown_or_malformed_request_ids(client: TestClient) -> None:
    missing = client.get("/v1/logs/never-seen")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"
    assert client.get("/v1/logs/bad%20id").status_code == 422


def test_log_buffer_is_configurable() -> None:
    app = create_app(
        Settings(market_data_dir=MARKET_DIR, log_buffer_size=5, log_buffer_level="DEBUG"),
        configure_logs=False,
    )
    with TestClient(app) as client:
        client.post(
            "/v1/curves/build",
            json={"market": {"snapshot": SNAPSHOT}},
            headers={"X-Request-ID": "tiny"},
        )
        timeline = client.get("/v1/logs/tiny").json()["result"]
    # Only the last five events survive, ending with the access log.
    assert timeline["event_count"] <= 5
    assert timeline["events"][-1]["event"] == "request_completed"
    assert timeline["buffer"]["evicted"] > 0


# --------------------------------------------------------------------------- metrics
def _samples(text: str) -> dict[str, float]:
    samples = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            name, value = line.rsplit(" ", 1)
            samples[name] = float(value)
    return samples


def test_metrics_expose_requests_errors_cache_and_batches(client: TestClient) -> None:
    client.post("/v1/curves/build", json={"market": {"snapshot": SNAPSHOT}})
    client.post("/v1/curves/build", json={"market": {"snapshot": SNAPSHOT}})
    client.get("/v1/market-snapshots/EUR_1999-01-01")
    client.get("/v1/logs/abc")
    client.get("/v1/logs/def")
    client.post(
        "/v1/batch/price",
        json={
            "market": {"snapshot": SNAPSHOT},
            "trades": [ois_trade(), {"type": "cap", "params": {}}],
        },
    )
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    samples = _samples(response.text)
    build = 'pricer_http_requests_total{method="POST",route="/v1/curves/build",status="200"}'
    assert samples[build] == 2.0
    # Labelled by route template, not by raw path: bounded cardinality.
    logs = 'pricer_http_requests_total{method="GET",route="/v1/logs/{request_id}",status="404"}'
    assert samples[logs] == 2.0
    assert samples['pricer_errors_total{code="NOT_FOUND"}'] == 3.0
    assert samples["pricer_curve_cache_hits_total"] == 2.0  # second build and the batch
    assert samples["pricer_curve_cache_misses_total"] == 1.0
    assert samples["pricer_curve_cache_size"] == 1.0
    assert samples['pricer_batch_trades_total{outcome="ok"}'] == 1.0
    assert samples['pricer_batch_trades_total{outcome="UNSUPPORTED_INSTRUMENT"}'] == 1.0
    assert any(name.startswith("pricer_build_info{") for name in samples)


def test_unmatched_routes_share_one_metric_label(client: TestClient) -> None:
    client.get("/no/such/route")
    client.get("/another/missing/route")
    samples = _samples(client.get("/metrics").text)
    assert samples['pricer_http_requests_total{method="GET",route="unmatched",status="404"}'] == 2.0
