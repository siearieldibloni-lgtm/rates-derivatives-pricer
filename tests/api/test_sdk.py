"""The SDK against the real application (FastAPI TestClient as transport)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

from rate_derivatives_pricer.sdk import (
    ApiUnavailableError,
    CalibrationFailedError,
    NotFoundError,
    PricerApiError,
    PricerClient,
    UnsupportedInstrumentError,
    ValidationFailedError,
    ois_swap,
)
from rate_derivatives_pricer.sdk.models import InlineMarketData, PriceResult
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
    with PricerClient(http_client=http_client) as sdk, pytest.raises(PricerApiError) as error:
        sdk.health()
    assert (error.value.status_code, error.value.code) == (502, "HTTP_ERROR")


def test_unreachable_api_raises_a_typed_error() -> None:
    sdk = PricerClient(base_url="http://127.0.0.1:9", timeout=0.5)
    with sdk, pytest.raises(ApiUnavailableError) as error:
        sdk.health()
    assert error.value.status_code is None
    assert error.value.code == "CONNECTION_ERROR"
