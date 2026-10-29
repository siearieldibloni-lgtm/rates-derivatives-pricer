"""The SDK against the real application (FastAPI TestClient as transport)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from rate_derivatives_pricer.sdk import (
    ApiUnavailableError,
    CalibrationFailedError,
    IncompatibleApiVersionError,
    NotFoundError,
    PricerApiError,
    PricerClient,
    UnsupportedInstrumentError,
    ValidationFailedError,
    ois_swap,
    versions_compatible,
)
from rate_derivatives_pricer.sdk.models import InlineMarketData, PriceResult, TradeModel
from tests.api.conftest import SNAPSHOT


@pytest.fixture
def sdk(client: TestClient) -> Iterator[PricerClient]:
    with PricerClient(http_client=client) as pricer:
        yield pricer


def test_metadata_endpoints(sdk: PricerClient) -> None:
    assert sdk.health().status == "ok"
    assert sdk.ready().status == "ready"
    assert sdk.version().result.core
    assert "ois_swap" in sdk.catalog().result.instrument_types
    assert SNAPSHOT in sdk.list_snapshots()
    assert sdk.get_snapshot(SNAPSHOT).valuation_date == date(2026, 10, 19)


def test_price_returns_typed_models(sdk: PricerClient) -> None:
    trade = ois_swap(10_000_000, 0.0225, "receive_fixed", "5Y")
    envelope = sdk.price(SNAPSHOT, trade, request_id="sdk-test-1")
    assert envelope.request_id == "sdk-test-1"
    assert isinstance(envelope.result, PriceResult)
    assert envelope.result.par_rate == pytest.approx(0.02254, abs=1e-12)
    assert envelope.result.present_value < 0  # receive 2.25% < par
    assert envelope.diagnostics.curve_cache == "miss"


def test_build_curves_with_options_and_inline_market(sdk: PricerClient) -> None:
    stored = sdk.get_snapshot(SNAPSHOT)
    inline = InlineMarketData(
        valuation_date=stored.valuation_date, quotes=stored.quotes, fixings=stored.fixings
    )
    by_model = sdk.build_curves(inline, {"interpolation": "linear_zero"})
    by_dict = sdk.build_curves(inline.model_dump(mode="json"), {"interpolation": "linear_zero"})
    assert by_model.result.curves[0].interpolation == "linear_zero"
    assert by_dict.diagnostics.curve_cache == "hit"
    plain_trade = {
        "type": "ois_swap",
        "params": {"notional": 1e6, "fixed_rate": 0.02, "direction": "pay_fixed", "maturity": "1Y"},
    }
    assert sdk.price({"inline": inline.model_dump(mode="json")}, plain_trade).result.cashflows


def test_api_errors_become_typed_exceptions(sdk: PricerClient) -> None:
    with pytest.raises(NotFoundError) as not_found:
        sdk.get_snapshot("EUR_1999-01-01")
    assert not_found.value.status_code == 404
    assert not_found.value.request_id
    with pytest.raises(UnsupportedInstrumentError):
        sdk.price(SNAPSHOT, {"type": "cap", "params": {}})
    with pytest.raises(ValidationFailedError) as invalid:
        sdk.price(SNAPSHOT, {"type": "ois_swap", "params": {"notional": 1}})
    assert invalid.value.details["errors"]
    stored = sdk.get_snapshot(SNAPSHOT)
    broken = [
        q.model_copy(update={"value": 5000.0}) if q.tenor == "2Y" else q for q in stored.quotes
    ]
    with pytest.raises(CalibrationFailedError) as calibration:
        sdk.build_curves(InlineMarketData(valuation_date=stored.valuation_date, quotes=broken))
    assert calibration.value.details["pillar"] == "2Y"
    assert "CALIBRATION_FAILED" in str(calibration.value)


def test_non_json_errors() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(502, text="Bad Gateway"))
    http_client = httpx.Client(transport=transport, base_url="http://x")
    sdk = PricerClient(http_client=http_client, max_retries=0)
    with sdk, pytest.raises(PricerApiError) as error:
        sdk.health()
    assert (error.value.status_code, error.value.code) == (502, "HTTP_ERROR")


def test_unreachable_api_raises_a_typed_error() -> None:
    sdk = PricerClient(base_url="http://127.0.0.1:9", timeout=0.5, max_retries=0)
    with sdk, pytest.raises(ApiUnavailableError) as error:
        sdk.health()
    assert error.value.status_code is None
    assert error.value.code == "CONNECTION_ERROR"


# --------------------------------------------------------------------------- phase 4
class FlakyTransport:
    """Answers with the queued statuses (or connection errors), then 200, recording requests."""

    def __init__(self, failures: list[int | type[Exception]]) -> None:
        self.failures = failures
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.failures:
            failure = self.failures.pop(0)
            if isinstance(failure, int):
                return httpx.Response(failure, text="unavailable")
            raise failure("simulated", request=request)  # type: ignore[call-arg]
        return httpx.Response(200, json={"status": "ok", "checks": {}})


def _flaky_sdk(transport: FlakyTransport, max_retries: int, delays: list[float]) -> PricerClient:
    http_client = httpx.Client(transport=httpx.MockTransport(transport), base_url="http://x")
    return PricerClient(
        http_client=http_client, max_retries=max_retries, backoff_seconds=0.1, sleep=delays.append
    )


def test_retries_with_backoff_keep_one_request_id() -> None:
    transport = FlakyTransport([503, httpx.ConnectError, 504])
    delays: list[float] = []
    assert _flaky_sdk(transport, 3, delays).health().status == "ok"
    assert delays == pytest.approx([0.1, 0.2, 0.4])  # exponential backoff
    ids = {request.headers["X-Request-ID"] for request in transport.requests}
    assert len(transport.requests) == 4
    assert len(ids) == 1  # every attempt carries the same correlation id


def test_retries_give_up_after_the_budget() -> None:
    delays: list[float] = []
    with pytest.raises(PricerApiError) as unavailable:
        _flaky_sdk(FlakyTransport([503, 503, 503]), 2, delays).health()
    assert unavailable.value.status_code == 503
    assert len(delays) == 2
    with pytest.raises(ApiUnavailableError) as refused:
        _flaky_sdk(FlakyTransport([httpx.ConnectError] * 2), 1, []).health()
    assert refused.value.request_id  # generated client-side, quotable to support


@pytest.mark.parametrize("failure", [500, 422, httpx.ReadTimeout])
def test_final_failures_are_not_retried(failure: int | type[Exception]) -> None:
    transport = FlakyTransport([failure])
    delays: list[float] = []
    with pytest.raises(PricerApiError):
        _flaky_sdk(transport, 3, delays).health()
    assert len(transport.requests) == 1
    assert delays == []


def test_retry_settings_are_validated() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        PricerClient(max_retries=-1)


def test_version_check(sdk: PricerClient, client: TestClient) -> None:
    assert sdk.check_version().api == sdk.version().result.api
    assert PricerClient(http_client=client, verify_version=True).version().result.core
    assert versions_compatible("0.1.0", "0.1.7")
    assert not versions_compatible("0.1.0", "0.2.0")
    assert versions_compatible("1.2.0", "1.9.3")
    assert not versions_compatible("1.2.0", "2.0.0")


def test_incompatible_api_version_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def version(request: httpx.Request) -> httpx.Response:
        result = {"api": "9.0.0", "core": "9.0.0", "python": "3", "numpy": "2", "commit": "x"}
        return httpx.Response(200, json={"request_id": "r", "result": result, "diagnostics": {}})

    http_client = httpx.Client(transport=httpx.MockTransport(version), base_url="http://x")
    with pytest.raises(IncompatibleApiVersionError) as error:
        PricerClient(http_client=http_client, verify_version=True)
    assert error.value.details["api_version"] == "9.0.0"


def test_batch_cache_logs_and_metrics(sdk: PricerClient) -> None:
    swap = ois_swap(1e7, 0.0225, "pay_fixed", "5Y")
    trades: list[TradeModel | dict[str, Any]] = [swap, {"type": "cap", "params": {}}]
    batch = sdk.batch_price(SNAPSHOT, trades, trade_ids=["good", "bad"], request_id="batch-1")
    assert [(r.trade_id, r.status) for r in batch.result.results] == [
        ("good", "ok"),
        ("bad", "error"),
    ]
    assert batch.result.results[1].error is not None
    assert batch.result.results[1].error.code == "UNSUPPORTED_INSTRUMENT"
    assert batch.result.summary.failed == 1
    by_position = sdk.batch_price(SNAPSHOT, [{**swap.model_dump(), "id": "own"}])
    assert by_position.result.results[0].trade_id == "own"
    with pytest.raises(ValueError, match="one entry per trade"):
        sdk.batch_price(SNAPSHOT, trades, trade_ids=["only-one"])

    assert sdk.cache_stats().size == 1
    timeline = sdk.logs("batch-1")
    assert timeline.events[-1].event == "request_completed"
    assert "batch_trade_rejected" in {event.event for event in timeline.events}
    with pytest.raises(NotFoundError):
        sdk.logs("never-seen")
    assert "pricer_batch_trades_total" in sdk.metrics()
    assert sdk.clear_cache().entries_dropped == 1
    assert sdk.cache_stats().size == 0


def test_metrics_errors_are_typed() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(500, json={}))
    http_client = httpx.Client(transport=transport, base_url="http://x")
    with pytest.raises(PricerApiError):
        PricerClient(http_client=http_client, max_retries=0).metrics()
