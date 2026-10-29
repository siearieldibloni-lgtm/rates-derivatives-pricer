"""HTTP routes of API v1 (docs/API.md section 2).

Routes are thin: validate the payload (Pydantic), map it to service inputs, call the service,
wrap the result in the response envelope. Calculation endpoints are synchronous functions:
FastAPI runs them in a worker thread, so the CPU-bound bootstrap never blocks the event loop.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from rate_derivatives_pricer.api.errors import (
    INTERNAL_ERROR,
    INTERNAL_ERROR_MESSAGE,
    VALIDATION_ERROR,
    core_error_status,
)
from rate_derivatives_pricer.api.log_buffer import LogRingBuffer
from rate_derivatives_pricer.api.logging_config import get_request_id
from rate_derivatives_pricer.api.metrics import ApiMetrics
from rate_derivatives_pricer.api.middleware import REQUEST_ID_PATTERN
from rate_derivatives_pricer.api.schemas import (
    SNAPSHOT_NAME_PATTERN,
    TRADE_PARAMS_SCHEMAS,
    BatchItemResult,
    BatchPriceRequest,
    BatchPriceResult,
    BatchSummary,
    CacheClearResult,
    CacheStatsResult,
    CalculationOptionsModel,
    CarryRequest,
    CarryResult,
    CatalogResult,
    CurvesBuildRequest,
    CurvesBuildResult,
    Diagnostics,
    Envelope,
    ErrorBody,
    ErrorResponse,
    HealthResult,
    InlineMarketModel,
    LogBufferInfo,
    LogEvent,
    LogsResult,
    MarketModel,
    PriceRequest,
    PriceResult,
    RiskRequest,
    RiskResult,
    ScenariosRequest,
    ScenariosResult,
    SnapshotListResult,
    SnapshotResult,
    TradeModel,
    VersionResult,
)
from rate_derivatives_pricer.api.settings import Settings
from rate_derivatives_pricer.core.errors import RatesPricerError, UnsupportedInstrumentError
from rate_derivatives_pricer.services.pricer_service import Diagnostics as ServiceDiagnostics
from rate_derivatives_pricer.services.pricer_service import PricerService
from rate_derivatives_pricer.services.requests import (
    BatchTrade,
    CalculationOptions,
    CarryOptions,
    InlineMarket,
    MarketInput,
    RiskOptions,
    ScenarioOptions,
    SnapshotReference,
    TradeInput,
)

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status: {"model": ErrorResponse} for status in (400, 404, 422, 500)
}

router = APIRouter()
logger = logging.getLogger(__name__)
MILLISECONDS_PER_SECOND = 1000.0


def get_service(request: Request) -> PricerService:
    """Service instance created by the application factory."""
    service: PricerService = request.app.state.service
    return service


def get_settings(request: Request) -> Settings:
    """Settings of the running application."""
    settings: Settings = request.app.state.settings
    return settings


def get_log_buffer(request: Request) -> LogRingBuffer:
    """In-memory log buffer of the running application."""
    log_buffer: LogRingBuffer = request.app.state.log_buffer
    return log_buffer


def get_metrics(request: Request) -> ApiMetrics:
    """Prometheus metrics of the running application."""
    metrics: ApiMetrics = request.app.state.metrics
    return metrics


ServiceDependency = Annotated[PricerService, Depends(get_service)]
MetricsDependency = Annotated[ApiMetrics, Depends(get_metrics)]


def _diagnostics(
    service: PricerService, diagnostics: ServiceDiagnostics | None = None
) -> Diagnostics:
    versions = {"api": service.version_info()["api"], "core": service.core_version}
    if diagnostics is None:
        return Diagnostics(versions=versions)
    return Diagnostics(
        timings_ms=diagnostics.timings_ms,
        curve_cache=diagnostics.curve_cache,
        versions=versions,
        warnings=list(diagnostics.warnings),
        input_hashes=diagnostics.input_hashes,
    )


def _market_input(market: MarketModel) -> MarketInput:
    if isinstance(market, InlineMarketModel):
        data = market.inline.model_dump(mode="json", exclude_none=True)
        return InlineMarket(
            valuation_date=data["valuation_date"],
            quotes=tuple(data["quotes"]),
            fixings={index: tuple(series) for index, series in data["fixings"].items()},
        )
    return SnapshotReference(snapshot=market.snapshot)


def _options(options: CalculationOptionsModel) -> CalculationOptions:
    return CalculationOptions(
        currency=options.currency, interpolation=options.interpolation, solver=options.solver
    )


def _trade_input(trade: TradeModel) -> TradeInput:
    """Validate the trade parameters with the schema of the trade type.

    Raises:
        UnsupportedInstrumentError: Unknown trade type (HTTP 400).
        RequestValidationError: Invalid parameters (HTTP 422).
    """
    schema = TRADE_PARAMS_SCHEMAS.get(trade.type)
    if schema is None:
        raise UnsupportedInstrumentError(
            f"unsupported instrument type: {trade.type!r}",
            {"instrument_type": trade.type, "available": sorted(TRADE_PARAMS_SCHEMAS)},
        )
    try:
        params = schema.model_validate(trade.params)
    except ValidationError as error:
        raise RequestValidationError(
            [{**e, "loc": ("body", "trade", "params", *e["loc"])} for e in error.errors()]
        ) from None
    return TradeInput(type=trade.type, params=params.model_dump(mode="json", exclude_none=True))


# --------------------------------------------------------------------------- probes
@router.get("/health", response_model=HealthResult, tags=["probes"])
def health() -> HealthResult:
    """Liveness: the process answers."""
    return HealthResult(status="ok")


@router.get(
    "/ready", response_model=HealthResult, tags=["probes"], responses={503: {"model": HealthResult}}
)
def ready(service: ServiceDependency) -> HealthResult | JSONResponse:
    """Readiness: conventions and registries are loaded."""
    checks = service.readiness()
    result = HealthResult(status="ready" if all(checks.values()) else "not_ready", checks=checks)
    if result.status != "ready":
        return JSONResponse(status_code=503, content=result.model_dump())
    return result


# --------------------------------------------------------------------------- metadata
@router.get("/v1/version", response_model=Envelope[VersionResult], tags=["metadata"])
def version(
    service: ServiceDependency, settings: Annotated[Settings, Depends(get_settings)]
) -> Envelope[VersionResult]:
    """Versions of the API, the core and the runtime, and the deployed commit."""
    return Envelope[VersionResult](
        request_id=get_request_id(),
        result=VersionResult(**service.version_info(), commit=settings.git_commit),
        diagnostics=_diagnostics(service),
    )


@router.get("/v1/catalog", response_model=Envelope[CatalogResult], tags=["metadata"])
def catalog(service: ServiceDependency) -> Envelope[CatalogResult]:
    """Instrument types, interpolations, currencies... generated from the registries."""
    content = service.catalog()
    content["trade_schemas"] = {
        name: schema.model_json_schema()
        for name, schema in TRADE_PARAMS_SCHEMAS.items()
        if name in content["instrument_types"]
    }
    return Envelope[CatalogResult](
        request_id=get_request_id(),
        result=CatalogResult.model_validate(content),
        diagnostics=_diagnostics(service),
    )


# --------------------------------------------------------------------------- market data
@router.get(
    "/v1/market-snapshots",
    response_model=Envelope[SnapshotListResult],
    tags=["market data"],
)
def list_snapshots(service: ServiceDependency) -> Envelope[SnapshotListResult]:
    """Names of the stored snapshots (files of the market data folder)."""
    return Envelope[SnapshotListResult](
        request_id=get_request_id(),
        result=SnapshotListResult(snapshots=service.list_snapshots()),
        diagnostics=_diagnostics(service),
    )


@router.get(
    "/v1/market-snapshots/{name}",
    response_model=Envelope[SnapshotResult],
    responses=ERROR_RESPONSES,
    tags=["market data"],
)
def get_snapshot(
    service: ServiceDependency,
    name: Annotated[str, Path(pattern=SNAPSHOT_NAME_PATTERN)],
) -> Envelope[SnapshotResult]:
    """Content of a stored snapshot."""
    return Envelope[SnapshotResult](
        request_id=get_request_id(),
        result=SnapshotResult.model_validate(service.get_snapshot(name)),
        diagnostics=_diagnostics(service),
    )


# --------------------------------------------------------------------------- calculations
@router.post(
    "/v1/curves/build",
    response_model=Envelope[CurvesBuildResult],
    responses=ERROR_RESPONSES,
    tags=["calculations"],
)
def build_curves(
    body: CurvesBuildRequest, service: ServiceDependency
) -> Envelope[CurvesBuildResult]:
    """Bootstrap the curves of a market: nodes, DFs, zero and forward rates, convergence."""
    outcome = service.build_curves(_market_input(body.market), _options(body.options))
    return Envelope[CurvesBuildResult](
        request_id=get_request_id(),
        result=CurvesBuildResult.model_validate(outcome.result),
        diagnostics=_diagnostics(service, outcome.diagnostics),
    )


@router.post(
    "/v1/price",
    response_model=Envelope[PriceResult],
    responses=ERROR_RESPONSES,
    tags=["calculations"],
)
def price(body: PriceRequest, service: ServiceDependency) -> Envelope[PriceResult]:
    """Price a trade: PV, PV per leg, cashflows, par rate, annuity."""
    trade = _trade_input(body.trade)
    outcome = service.price(_market_input(body.market), trade, _options(body.options))
    return Envelope[PriceResult](
        request_id=get_request_id(),
        result=PriceResult.model_validate(outcome.result),
        diagnostics=_diagnostics(service, outcome.diagnostics),
    )


@router.post(
    "/v1/risk",
    response_model=Envelope[RiskResult],
    responses=ERROR_RESPONSES,
    tags=["calculations"],
)
def risk(body: RiskRequest, service: ServiceDependency) -> Envelope[RiskResult]:
    """Risk of a trade by bump-and-reprice: DV01 (parallel, per bucket, per curve), convexity."""
    trade = _trade_input(body.trade)
    outcome = service.risk(
        _market_input(body.market),
        trade,
        RiskOptions(
            measures=tuple(body.measures),
            bump_size_bp=body.bump.size_bp,
            mode=body.bump.mode,
        ),
        _options(body.options),
    )
    return Envelope[RiskResult](
        request_id=get_request_id(),
        result=RiskResult.model_validate(outcome.result),
        diagnostics=_diagnostics(service, outcome.diagnostics),
    )


@router.post(
    "/v1/risk/scenarios",
    response_model=Envelope[ScenariosResult],
    responses=ERROR_RESPONSES,
    tags=["calculations"],
)
def scenarios(body: ScenariosRequest, service: ServiceDependency) -> Envelope[ScenariosResult]:
    """Full revaluation under named scenarios: parallel, steepener, flattener, twist, custom."""
    trade = _trade_input(body.trade)
    outcome = service.scenarios(
        _market_input(body.market),
        trade,
        ScenarioOptions(
            names=tuple(body.scenarios),
            custom=tuple(
                scenario.model_dump(mode="json", exclude_none=True)
                for scenario in body.custom_scenarios
            ),
        ),
        _options(body.options),
    )
    return Envelope[ScenariosResult](
        request_id=get_request_id(),
        result=ScenariosResult.model_validate(outcome.result),
        diagnostics=_diagnostics(service, outcome.diagnostics),
    )


@router.post(
    "/v1/risk/carry",
    response_model=Envelope[CarryResult],
    responses=ERROR_RESPONSES,
    tags=["calculations"],
)
def carry(body: CarryRequest, service: ServiceDependency) -> Envelope[CarryResult]:
    """Carry and roll-down over a horizon, under the unchanged curve hypothesis."""
    trade = _trade_input(body.trade)
    outcome = service.carry_rolldown(
        _market_input(body.market),
        trade,
        CarryOptions(horizon=body.horizon),
        _options(body.options),
    )
    return Envelope[CarryResult](
        request_id=get_request_id(),
        result=CarryResult.model_validate(outcome.result),
        diagnostics=_diagnostics(service, outcome.diagnostics),
    )


# --------------------------------------------------------------------------- batch
def _error_body(error: Exception) -> ErrorBody:
    """Structured error of one batch trade, with the code a single request would get."""
    if isinstance(error, RequestValidationError):
        errors = [
            {"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
            for e in error.errors()
        ]
        return ErrorBody(
            code=VALIDATION_ERROR, message="trade validation failed", details={"errors": errors}
        )
    if isinstance(error, RatesPricerError):
        _, code = core_error_status(error)
        return ErrorBody(code=code, message=error.message, details=jsonable_encoder(error.details))
    # The trace is already logged by the service; the client only gets the generic message.
    return ErrorBody(code=INTERNAL_ERROR, message=INTERNAL_ERROR_MESSAGE)


@router.post(
    "/v1/batch/price",
    response_model=Envelope[BatchPriceResult],
    responses=ERROR_RESPONSES,
    tags=["calculations"],
)
def batch_price(
    body: BatchPriceRequest, service: ServiceDependency, metrics: MetricsDependency
) -> Envelope[BatchPriceResult]:
    """Price several trades on one market, with a result or an error per trade.

    The market is bootstrapped once. An invalid trade only fails its own entry (HTTP 200 with
    partial results); an invalid market fails the whole request, as for ``/v1/price``.
    """
    trade_ids = [trade.id or str(position) for position, trade in enumerate(body.trades)]
    # Parameters are validated per trade: a typo in one trade must not reject the others.
    rejected: dict[str, ErrorBody] = {}
    valid: list[BatchTrade] = []
    for trade_id, trade in zip(trade_ids, body.trades, strict=True):
        try:
            valid.append(BatchTrade(trade_id=trade_id, trade=_trade_input(trade)))
        except (RequestValidationError, UnsupportedInstrumentError) as error:
            rejected[trade_id] = _error_body(error)
            logger.warning(
                "batch_trade_rejected",
                extra={"trade_id": trade_id, "error_code": rejected[trade_id].code},
            )

    outcome = service.price_batch(_market_input(body.market), valid, _options(body.options))
    priced = {item.trade_id: item for item in outcome.items}
    results: list[BatchItemResult] = []
    for trade_id in trade_ids:
        if trade_id in rejected:
            results.append(
                BatchItemResult(trade_id=trade_id, status="error", error=rejected[trade_id])
            )
            continue
        item = priced[trade_id]
        results.append(
            BatchItemResult(
                trade_id=trade_id,
                status="ok" if item.error is None else "error",
                result=PriceResult.model_validate(item.result) if item.result else None,
                error=_error_body(item.error) if item.error is not None else None,
                trade_hash=item.trade_hash,
                duration_ms=item.duration_ms,
            )
        )
    for result in results:
        metrics.count_batch_trade(result.error.code if result.error else "ok")

    succeeded = [r.result for r in results if r.result is not None]
    summary = BatchSummary(
        trade_count=len(results),
        succeeded=len(succeeded),
        failed=len(results) - len(succeeded),
        total_present_value=sum(result.present_value for result in succeeded),
    )
    logger.info(
        "batch_completed",
        extra={"trade_count": summary.trade_count, "failed": summary.failed},
    )
    return Envelope[BatchPriceResult](
        request_id=get_request_id(),
        result=BatchPriceResult(
            valuation_date=outcome.valuation_date,
            currency=outcome.currency,
            summary=summary,
            results=results,
        ),
        diagnostics=_diagnostics(service, outcome.diagnostics),
    )


# --------------------------------------------------------------------------- operations
@router.get("/v1/cache/stats", response_model=Envelope[CacheStatsResult], tags=["operations"])
def cache_stats(service: ServiceDependency) -> Envelope[CacheStatsResult]:
    """Curve cache counters of the process that answers (each worker has its own cache)."""
    stats = service.cache_stats()
    lookups = stats.hits + stats.misses
    return Envelope[CacheStatsResult](
        request_id=get_request_id(),
        result=CacheStatsResult(
            hits=stats.hits,
            misses=stats.misses,
            evictions=stats.evictions,
            size=stats.size,
            max_size=stats.max_size,
            ttl_seconds=stats.ttl_seconds,
            hit_ratio=stats.hits / lookups if lookups else None,
        ),
        diagnostics=_diagnostics(service),
    )


@router.delete("/v1/cache", response_model=Envelope[CacheClearResult], tags=["operations"])
def clear_cache(service: ServiceDependency) -> Envelope[CacheClearResult]:
    """Empty the curve cache and reset its counters (process that answers only)."""
    return Envelope[CacheClearResult](
        request_id=get_request_id(),
        result=CacheClearResult(entries_dropped=service.clear_cache()),
        diagnostics=_diagnostics(service),
    )


@router.get(
    "/v1/logs/{request_id}",
    response_model=Envelope[LogsResult],
    responses=ERROR_RESPONSES,
    tags=["operations"],
)
def request_logs(
    service: ServiceDependency,
    log_buffer: Annotated[LogRingBuffer, Depends(get_log_buffer)],
    request_id: Annotated[str, Path(pattern=REQUEST_ID_PATTERN)],
) -> Envelope[LogsResult]:
    """Timeline of the log events of one request, from the in-memory ring buffer.

    Raises:
        HTTPException: 404 ``NOT_FOUND`` when no event is buffered for ``request_id`` (unknown,
            evicted, or served by another worker).
    """
    events = log_buffer.events_for(request_id)
    if not events:
        raise StarletteHTTPException(
            404, f"no log event buffered for request_id {request_id!r} (unknown or evicted)"
        )
    first = datetime.fromisoformat(events[0]["timestamp"])
    last = datetime.fromisoformat(events[-1]["timestamp"])
    return Envelope[LogsResult](
        request_id=get_request_id(),
        result=LogsResult(
            request_id=request_id,
            event_count=len(events),
            duration_ms=(last - first).total_seconds() * MILLISECONDS_PER_SECOND,
            events=[LogEvent.model_validate(event) for event in events],
            buffer=LogBufferInfo(**log_buffer.stats()),
        ),
        diagnostics=_diagnostics(service),
    )


@router.get("/metrics", tags=["probes"], response_class=Response)
def metrics_endpoint(metrics: MetricsDependency) -> Response:
    """Prometheus metrics (text exposition format)."""
    content, content_type = metrics.render()
    return Response(content=content, media_type=content_type)
