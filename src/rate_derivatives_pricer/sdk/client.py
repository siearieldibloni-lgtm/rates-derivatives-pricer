"""Typed Python client of the pricing API (docs/API.md section 8).

    client = PricerClient(base_url="http://localhost:8000")
    result = client.price(market="EUR_2026-10-19", trade=ois_swap(1e7, 0.0225, "pay_fixed", "5Y"))
    result.result.present_value, result.request_id

Responses are parsed into the API's own Pydantic schemas, so any contract change breaks here
first. API errors are raised as typed exceptions carrying the stable code and the request_id.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from types import TracebackType
from typing import Any, Literal

import httpx
from pydantic import BaseModel

from rate_derivatives_pricer.api.schemas import (
    BumpOptionsModel,
    CalculationOptionsModel,
    CatalogResult,
    CurvesBuildResult,
    Envelope,
    HealthResult,
    InlineMarketData,
    OisSwapParams,
    PriceResult,
    RiskResult,
    SnapshotListResult,
    SnapshotResult,
    TradeModel,
    VanillaIrsParams,
    VersionResult,
)

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_TIMEOUT_SECONDS = 30.0
REQUEST_ID_HEADER = "X-Request-ID"

type MarketArgument = str | InlineMarketData | Mapping[str, Any]
type OptionsArgument = CalculationOptionsModel | Mapping[str, Any] | None


class PricerApiError(Exception):
    """Error returned by the API (or raised when it cannot be reached).

    Attributes:
        status_code: HTTP status (``None`` if no response was received).
        code: Stable machine code (``"CALIBRATION_FAILED"``...).
        message: Human-readable message.
        details: Structured context (pillar, residual, validation errors...).
        request_id: Correlation id to quote to support.
    """

    def __init__(
        self,
        status_code: int | None,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(f"[{code}] {message} (request_id={request_id})")
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}
        self.request_id = request_id


class ApiUnavailableError(PricerApiError):
    """``CONNECTION_ERROR``: no response (API down, wrong URL, timeout)."""


class ValidationFailedError(PricerApiError):
    """``VALIDATION_ERROR``: malformed request."""


class MarketDataInvalidError(PricerApiError):
    """``MARKET_DATA_INVALID``: invalid market data or missing fixings."""


class InvalidTradeDefinitionError(PricerApiError):
    """``INVALID_TRADE_DEFINITION``: invalid conventions, dates or trade fields."""


class UnsupportedInstrumentError(PricerApiError):
    """``UNSUPPORTED_INSTRUMENT``: instrument type not supported."""


class UnsupportedRiskMeasureError(PricerApiError):
    """``UNSUPPORTED_RISK_MEASURE``: unknown risk measure name."""


class CalibrationFailedError(PricerApiError):
    """``CALIBRATION_FAILED``: a curve pillar could not be calibrated."""


class NotFoundError(PricerApiError):
    """``NOT_FOUND``: unknown snapshot or route."""


class ComputationFailedError(PricerApiError):
    """``COMPUTATION_FAILED``: pricing or risk computation failed."""


class InternalServerError(PricerApiError):
    """``INTERNAL_ERROR``: unexpected server error."""


ERROR_TYPES: dict[str, type[PricerApiError]] = {
    "VALIDATION_ERROR": ValidationFailedError,
    "MARKET_DATA_INVALID": MarketDataInvalidError,
    "INVALID_TRADE_DEFINITION": InvalidTradeDefinitionError,
    "UNSUPPORTED_INSTRUMENT": UnsupportedInstrumentError,
    "UNSUPPORTED_RISK_MEASURE": UnsupportedRiskMeasureError,
    "CALIBRATION_FAILED": CalibrationFailedError,
    "NOT_FOUND": NotFoundError,
    "COMPUTATION_FAILED": ComputationFailedError,
    "INTERNAL_ERROR": InternalServerError,
}


def ois_swap(
    notional: float,
    fixed_rate: float,
    direction: Literal["pay_fixed", "receive_fixed"],
    maturity: str | None = None,
    *,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    currency: str = "EUR",
) -> TradeModel:
    """Describe an OIS trade (rates in decimal, notional in the trade currency)."""
    params = OisSwapParams(
        currency=currency,
        notional=notional,
        fixed_rate=fixed_rate,
        direction=direction,
        maturity=maturity,
        start_date=start_date,
        end_date=end_date,
    )
    return TradeModel(type="ois_swap", params=params.model_dump(mode="json", exclude_none=True))


def vanilla_irs(
    notional: float,
    fixed_rate: float,
    direction: Literal["pay_fixed", "receive_fixed"],
    maturity: str | None = None,
    *,
    index: str | None = None,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    currency: str = "EUR",
) -> TradeModel:
    """Describe a vanilla IRS trade against an Ibor index (``"EURIBOR6M"`` by default)."""
    params = VanillaIrsParams(
        currency=currency,
        index=index,
        notional=notional,
        fixed_rate=fixed_rate,
        direction=direction,
        maturity=maturity,
        start_date=start_date,
        end_date=end_date,
    )
    return TradeModel(type="vanilla_irs", params=params.model_dump(mode="json", exclude_none=True))


def _market_payload(market: MarketArgument) -> dict[str, Any]:
    """``"EUR_2026-10-19"`` -> by reference; inline data -> by value."""
    if isinstance(market, str):
        return {"snapshot": market}
    if isinstance(market, InlineMarketData):
        return {"inline": market.model_dump(mode="json")}
    payload = dict(market)
    return (
        payload
        if {"snapshot"} == set(payload) or {"inline"} == set(payload)
        else {"inline": payload}
    )


def _options_payload(options: OptionsArgument) -> dict[str, Any]:
    if options is None:
        return CalculationOptionsModel().model_dump()
    if isinstance(options, BaseModel):
        return options.model_dump()
    return CalculationOptionsModel.model_validate(dict(options)).model_dump()


class PricerClient:
    """Synchronous client of the pricing API.

    Args:
        base_url: Root URL of the API.
        timeout: Request timeout in seconds.
        http_client: Pre-configured ``httpx.Client`` (e.g. a FastAPI ``TestClient``); its own
            base URL is then used.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(base_url=base_url, timeout=timeout)

    def close(self) -> None:
        """Close the underlying HTTP connection pool (if owned)."""
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> PricerClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    # ----------------------------------------------------------------- transport
    def _request(
        self,
        method: str,
        path: str,
        json: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> Any:
        headers = {REQUEST_ID_HEADER: request_id} if request_id else None
        try:
            response = self._http.request(method, path, json=json, headers=headers)
        except httpx.TransportError as transport_error:
            raise ApiUnavailableError(
                None,
                "CONNECTION_ERROR",
                f"{type(transport_error).__name__}: {transport_error}",
                request_id=request_id,
            ) from transport_error
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.is_success and body is not None:
            return body
        error = (body or {}).get("error") if isinstance(body, dict) else None
        if not isinstance(error, dict):
            raise PricerApiError(
                response.status_code,
                "HTTP_ERROR",
                response.text[:200] or response.reason_phrase,
                request_id=response.headers.get(REQUEST_ID_HEADER),
            )
        error_type = ERROR_TYPES.get(str(error.get("code")), PricerApiError)
        raise error_type(
            response.status_code,
            str(error.get("code")),
            str(error.get("message", "")),
            error.get("details") or {},
            body.get("request_id") or response.headers.get(REQUEST_ID_HEADER),
        )

    # ----------------------------------------------------------------- endpoints
    def health(self) -> HealthResult:
        """``GET /health``."""
        return HealthResult.model_validate(self._request("GET", "/health"))

    def ready(self) -> HealthResult:
        """``GET /ready``."""
        return HealthResult.model_validate(self._request("GET", "/ready"))

    def version(self) -> Envelope[VersionResult]:
        """``GET /v1/version``."""
        return Envelope[VersionResult].model_validate(self._request("GET", "/v1/version"))

    def catalog(self) -> Envelope[CatalogResult]:
        """``GET /v1/catalog``."""
        return Envelope[CatalogResult].model_validate(self._request("GET", "/v1/catalog"))

    def list_snapshots(self) -> list[str]:
        """Names of the stored snapshots."""
        envelope = Envelope[SnapshotListResult].model_validate(
            self._request("GET", "/v1/market-snapshots")
        )
        return envelope.result.snapshots

    def get_snapshot(self, name: str) -> SnapshotResult:
        """Content of a stored snapshot."""
        envelope = Envelope[SnapshotResult].model_validate(
            self._request("GET", f"/v1/market-snapshots/{name}")
        )
        return envelope.result

    def build_curves(
        self,
        market: MarketArgument,
        options: OptionsArgument = None,
        request_id: str | None = None,
    ) -> Envelope[CurvesBuildResult]:
        """``POST /v1/curves/build``.

        Args:
            market: Snapshot name, or inline market data.
            options: Interpolation, solver, currency of inline data.
            request_id: Correlation id to send (generated by the API when omitted).
        """
        body = {"market": _market_payload(market), "options": _options_payload(options)}
        return Envelope[CurvesBuildResult].model_validate(
            self._request("POST", "/v1/curves/build", json=body, request_id=request_id)
        )

    def price(
        self,
        market: MarketArgument,
        trade: TradeModel | Mapping[str, Any],
        options: OptionsArgument = None,
        request_id: str | None = None,
    ) -> Envelope[PriceResult]:
        """``POST /v1/price``.

        Args:
            market: Snapshot name, or inline market data.
            trade: Trade description, e.g. built with :func:`ois_swap`.
            options: Interpolation, solver, currency of inline data.
            request_id: Correlation id to send (generated by the API when omitted).
        """
        trade_model = (
            trade if isinstance(trade, TradeModel) else TradeModel.model_validate(dict(trade))
        )
        body = {
            "market": _market_payload(market),
            "trade": trade_model.model_dump(mode="json"),
            "options": _options_payload(options),
        }
        return Envelope[PriceResult].model_validate(
            self._request("POST", "/v1/price", json=body, request_id=request_id)
        )

    def risk(
        self,
        market: MarketArgument,
        trade: TradeModel | Mapping[str, Any],
        measures: Sequence[str],
        bump: BumpOptionsModel | Mapping[str, Any] | None = None,
        options: OptionsArgument = None,
        request_id: str | None = None,
    ) -> Envelope[RiskResult]:
        """``POST /v1/risk``.

        Args:
            market: Snapshot name, or inline market data.
            trade: Trade description, e.g. built with :func:`vanilla_irs`.
            measures: Risk measure names (``"parallel_dv01"``, ``"key_rate_dv01"``,
                ``"curve_dv01"``, ``"convexity"``; see :meth:`catalog`).
            bump: Bump size in bp and finite difference mode (1bp, central by default).
            options: Interpolation, solver, currency of inline data.
            request_id: Correlation id to send (generated by the API when omitted).
        """
        trade_model = (
            trade if isinstance(trade, TradeModel) else TradeModel.model_validate(dict(trade))
        )
        bump_model = (
            bump
            if isinstance(bump, BumpOptionsModel)
            else BumpOptionsModel.model_validate(dict(bump or {}))
        )
        body = {
            "market": _market_payload(market),
            "trade": trade_model.model_dump(mode="json"),
            "measures": list(measures),
            "bump": bump_model.model_dump(),
            "options": _options_payload(options),
        }
        return Envelope[RiskResult].model_validate(
            self._request("POST", "/v1/risk", json=body, request_id=request_id)
        )
