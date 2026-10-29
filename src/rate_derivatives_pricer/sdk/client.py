"""Typed Python client of the pricing API (docs/API.md section 8).

    client = PricerClient(base_url="http://localhost:8000")
    result = client.price(market="EUR_2026-10-19", trade=ois_swap(1e7, 0.0225, "pay_fixed", "5Y"))
    result.result.present_value, result.request_id

Responses are parsed into the API's own Pydantic schemas, so any contract change breaks here
first. API errors are raised as typed exceptions carrying the stable code and the request_id.

Production behaviour (ADR-039): configurable timeouts; retries with exponential backoff on
connection failures and on 502/503/504 only (every endpoint is stateless, so a retry is safe);
one ``request_id`` per call, generated client-side and kept across retries so that the server
logs of every attempt share it; :meth:`PricerClient.check_version` refuses an incompatible API.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from types import TracebackType
from typing import Any, Literal

import httpx
from pydantic import BaseModel

import rate_derivatives_pricer
from rate_derivatives_pricer.api.schemas import (
    BatchPriceResult,
    BatchTradeModel,
    BumpOptionsModel,
    CacheClearResult,
    CacheStatsResult,
    CalculationOptionsModel,
    CarryResult,
    CatalogResult,
    CurvesBuildResult,
    Envelope,
    HealthResult,
    InlineMarketData,
    LogsResult,
    OisSwapParams,
    PriceResult,
    RiskResult,
    ScenarioModel,
    ScenarioPointModel,
    ScenariosResult,
    SnapshotListResult,
    SnapshotResult,
    TradeModel,
    VanillaIrsParams,
    VersionResult,
)

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_BACKOFF_SECONDS = 0.5
REQUEST_ID_HEADER = "X-Request-ID"
# Gateway / overload statuses worth a retry; every other error is final (a retry would fail too).
RETRYABLE_STATUS_CODES = frozenset({502, 503, 504})
# Transport failures worth a retry: the request never reached the server, or the connection
# dropped. A read timeout is not retried: the server may still be computing.
RETRYABLE_TRANSPORT_ERRORS: tuple[type[httpx.TransportError], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.RemoteProtocolError,
)

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


class UnsupportedScenarioError(UnsupportedRiskMeasureError):
    """``UNSUPPORTED_SCENARIO``: unknown preset scenario or ill-defined custom scenario."""


class CalibrationFailedError(PricerApiError):
    """``CALIBRATION_FAILED``: a curve pillar could not be calibrated."""


class NotFoundError(PricerApiError):
    """``NOT_FOUND``: unknown snapshot or route."""


class ComputationFailedError(PricerApiError):
    """``COMPUTATION_FAILED``: pricing or risk computation failed."""


class InternalServerError(PricerApiError):
    """``INTERNAL_ERROR``: unexpected server error."""


class IncompatibleApiVersionError(PricerApiError):
    """``INCOMPATIBLE_VERSION``: the API version does not match the SDK (see ``check_version``)."""


def versions_compatible(sdk_version: str, api_version: str) -> bool:
    """Semantic versioning compatibility of the SDK with an API version.

    Same major version; while the major is 0 (initial development), the minor must match too.
    """
    sdk_parts = sdk_version.split(".")
    api_parts = api_version.split(".")
    significant = 2 if sdk_parts[0] == "0" else 1
    return sdk_parts[:significant] == api_parts[:significant]


ERROR_TYPES: dict[str, type[PricerApiError]] = {
    "VALIDATION_ERROR": ValidationFailedError,
    "MARKET_DATA_INVALID": MarketDataInvalidError,
    "INVALID_TRADE_DEFINITION": InvalidTradeDefinitionError,
    "UNSUPPORTED_INSTRUMENT": UnsupportedInstrumentError,
    "UNSUPPORTED_RISK_MEASURE": UnsupportedRiskMeasureError,
    "UNSUPPORTED_SCENARIO": UnsupportedScenarioError,
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


def scenario(
    name: str,
    points: Mapping[str, float] | Sequence[tuple[str, float]],
    description: str = "",
    curves: Sequence[str] | None = None,
) -> ScenarioModel:
    """Describe a custom scenario: par-rate shifts in bp by tenor, linear in between.

    Example: ``scenario("bear_steepener", {"2Y": 0.0, "10Y": 30.0})``.
    """
    pairs = points.items() if isinstance(points, Mapping) else points
    return ScenarioModel(
        name=name,
        description=description,
        curves=list(curves) if curves is not None else None,
        points=[ScenarioPointModel(tenor=tenor, shift_bp=shift) for tenor, shift in pairs],
    )


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
        timeout: Request timeout in seconds, or an ``httpx.Timeout`` (connect/read split).
        http_client: Pre-configured ``httpx.Client`` (e.g. a FastAPI ``TestClient``); its own
            base URL and timeout are then used.
        max_retries: Retries after the first attempt on a retryable failure (0 disables them).
        backoff_seconds: Delay before the first retry, doubled at each further retry.
        verify_version: Call :meth:`check_version` at construction (fail fast on a mismatch).
        sleep: Wait function between retries (injectable in tests).
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float | httpx.Timeout = DEFAULT_TIMEOUT_SECONDS,
        http_client: httpx.Client | None = None,
        *,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        verify_version: bool = False,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_retries < 0 or backoff_seconds < 0:
            raise ValueError("max_retries and backoff_seconds must be non-negative")
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(base_url=base_url, timeout=timeout)
        self._max_retries = max_retries
        self._backoff_seconds = backoff_seconds
        self._sleep = sleep
        if verify_version:
            try:
                self.check_version()
            except Exception:
                self.close()
                raise

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
        # One id for every attempt: the server logs of a retried call stay correlated.
        request_id = request_id or uuid.uuid4().hex
        response = self._send_with_retries(method, path, json, request_id)
        return self._parse(response)

    def _send_with_retries(
        self, method: str, path: str, json: dict[str, Any] | None, request_id: str
    ) -> httpx.Response:
        """Send the request, retrying retryable failures with exponential backoff."""
        headers = {REQUEST_ID_HEADER: request_id}
        attempt = 0
        while True:
            try:
                response = self._http.request(method, path, json=json, headers=headers)
            except httpx.TransportError as transport_error:
                if attempt >= self._max_retries or not isinstance(
                    transport_error, RETRYABLE_TRANSPORT_ERRORS
                ):
                    raise ApiUnavailableError(
                        None,
                        "CONNECTION_ERROR",
                        f"{type(transport_error).__name__}: {transport_error}",
                        request_id=request_id,
                    ) from transport_error
                reason = type(transport_error).__name__
            else:
                if attempt >= self._max_retries or (
                    response.status_code not in RETRYABLE_STATUS_CODES
                ):
                    return response
                reason = f"HTTP {response.status_code}"
            delay = self._backoff_seconds * 2**attempt
            attempt += 1
            logger.warning(
                "api_request_retried",
                extra={
                    "method": method,
                    "path": path,
                    "attempt": attempt,
                    "reason": reason,
                    "delay_seconds": delay,
                    "sent_request_id": request_id,
                },
            )
            self._sleep(delay)

    @staticmethod
    def _parse(response: httpx.Response) -> Any:
        """JSON body of a successful response; typed exception otherwise."""
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
    def check_version(self) -> VersionResult:
        """Check that the API speaks a version of the contract this SDK understands.

        Raises:
            IncompatibleApiVersionError: The API version is not compatible with the SDK's
                (see :func:`versions_compatible`).
        """
        envelope = self.version()
        sdk_version = rate_derivatives_pricer.__version__
        if not versions_compatible(sdk_version, envelope.result.api):
            raise IncompatibleApiVersionError(
                None,
                "INCOMPATIBLE_VERSION",
                f"API version {envelope.result.api} is not compatible with SDK {sdk_version}",
                {"api_version": envelope.result.api, "sdk_version": sdk_version},
                envelope.request_id,
            )
        return envelope.result

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
            request_id: Correlation id to send (generated by the SDK when omitted).
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
            request_id: Correlation id to send (generated by the SDK when omitted).
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
            request_id: Correlation id to send (generated by the SDK when omitted).
        """
        trade_model = _trade_model(trade)
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

    def scenarios(
        self,
        market: MarketArgument,
        trade: TradeModel | Mapping[str, Any],
        scenarios: Sequence[str] = (),
        custom_scenarios: Sequence[ScenarioModel | Mapping[str, Any]] = (),
        options: OptionsArgument = None,
        request_id: str | None = None,
    ) -> Envelope[ScenariosResult]:
        """``POST /v1/risk/scenarios``: full revaluation under named scenarios.

        Args:
            market: Snapshot name, or inline market data.
            trade: Trade description.
            scenarios: Preset names (``"parallel_up"``, ``"steepener"``...; see :meth:`catalog`).
            custom_scenarios: Custom shapes, e.g. built with :func:`scenario`.
            options: Interpolation, solver, currency of inline data.
            request_id: Correlation id to send (generated by the SDK when omitted).

        Every preset runs when neither ``scenarios`` nor ``custom_scenarios`` is given.
        """
        body = {
            "market": _market_payload(market),
            "trade": _trade_model(trade).model_dump(mode="json"),
            "scenarios": list(scenarios),
            "custom_scenarios": [
                (
                    s if isinstance(s, ScenarioModel) else ScenarioModel.model_validate(dict(s))
                ).model_dump(mode="json", exclude_none=True)
                for s in custom_scenarios
            ],
            "options": _options_payload(options),
        }
        return Envelope[ScenariosResult].model_validate(
            self._request("POST", "/v1/risk/scenarios", json=body, request_id=request_id)
        )

    def carry_rolldown(
        self,
        market: MarketArgument,
        trade: TradeModel | Mapping[str, Any],
        horizon: str = "3M",
        options: OptionsArgument = None,
        request_id: str | None = None,
    ) -> Envelope[CarryResult]:
        """``POST /v1/risk/carry``: carry and roll-down under the unchanged curve hypothesis.

        Args:
            market: Snapshot name, or inline market data.
            trade: Trade description.
            horizon: Horizon tenor (``"1M"``, ``"3M"``, ``"1Y"``).
            options: Interpolation, solver, currency of inline data.
            request_id: Correlation id to send (generated by the SDK when omitted).
        """
        body = {
            "market": _market_payload(market),
            "trade": _trade_model(trade).model_dump(mode="json"),
            "horizon": horizon,
            "options": _options_payload(options),
        }
        return Envelope[CarryResult].model_validate(
            self._request("POST", "/v1/risk/carry", json=body, request_id=request_id)
        )

    def batch_price(
        self,
        market: MarketArgument,
        trades: Sequence[TradeModel | Mapping[str, Any]],
        trade_ids: Sequence[str] | None = None,
        options: OptionsArgument = None,
        request_id: str | None = None,
    ) -> Envelope[BatchPriceResult]:
        """``POST /v1/batch/price``: price several trades on one market, errors per trade.

        Args:
            market: Snapshot name, or inline market data.
            trades: Trade descriptions; a mapping may carry its own ``"id"``.
            trade_ids: Identifiers echoed in the results (positions ``"0"``, ``"1"``... when
                omitted and the trades carry none).
            options: Interpolation, solver, currency of inline data.
            request_id: Correlation id to send (generated by the SDK when omitted).

        A failed trade comes back with ``status == "error"`` and its structured ``error``; only
        an invalid market (or request) raises.
        """
        if trade_ids is not None and len(trade_ids) != len(trades):
            raise ValueError("trade_ids must have one entry per trade")
        models = []
        for position, trade in enumerate(trades):
            content = (
                trade.model_dump(mode="json") if isinstance(trade, TradeModel) else dict(trade)
            )
            if trade_ids is not None:
                content["id"] = trade_ids[position]
            models.append(BatchTradeModel.model_validate(content))
        body = {
            "market": _market_payload(market),
            "trades": [model.model_dump(mode="json", exclude_none=True) for model in models],
            "options": _options_payload(options),
        }
        return Envelope[BatchPriceResult].model_validate(
            self._request("POST", "/v1/batch/price", json=body, request_id=request_id)
        )

    def cache_stats(self) -> CacheStatsResult:
        """``GET /v1/cache/stats``: curve cache counters of the worker that answers."""
        return (
            Envelope[CacheStatsResult]
            .model_validate(self._request("GET", "/v1/cache/stats"))
            .result
        )

    def clear_cache(self) -> CacheClearResult:
        """``DELETE /v1/cache``: empty the curve cache of the worker that answers."""
        return (
            Envelope[CacheClearResult].model_validate(self._request("DELETE", "/v1/cache")).result
        )

    def logs(self, request_id: str) -> LogsResult:
        """``GET /v1/logs/{request_id}``: timeline of the log events of a past request.

        Raises:
            NotFoundError: No event buffered for this id (unknown, evicted or other worker).
        """
        return (
            Envelope[LogsResult]
            .model_validate(self._request("GET", f"/v1/logs/{request_id}"))
            .result
        )

    def metrics(self) -> str:
        """``GET /metrics``: Prometheus exposition text."""
        response = self._send_with_retries("GET", "/metrics", None, uuid.uuid4().hex)
        if not response.is_success:
            self._parse(response)
        return response.text


def _trade_model(trade: TradeModel | Mapping[str, Any]) -> TradeModel:
    return trade if isinstance(trade, TradeModel) else TradeModel.model_validate(dict(trade))
