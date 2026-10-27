"""HTTP routes of API v1 (docs/API.md section 2).

Routes are thin: validate the payload (Pydantic), map it to service inputs, call the service,
wrap the result in the response envelope. Calculation endpoints are synchronous functions:
FastAPI runs them in a worker thread, so the CPU-bound bootstrap never blocks the event loop.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from rate_derivatives_pricer.api.logging_config import get_request_id
from rate_derivatives_pricer.api.schemas import (
    SNAPSHOT_NAME_PATTERN,
    TRADE_PARAMS_SCHEMAS,
    CalculationOptionsModel,
    CatalogResult,
    CurvesBuildRequest,
    CurvesBuildResult,
    Diagnostics,
    Envelope,
    ErrorResponse,
    HealthResult,
    InlineMarketModel,
    MarketModel,
    PriceRequest,
    PriceResult,
    RiskRequest,
    RiskResult,
    SnapshotListResult,
    SnapshotResult,
    TradeModel,
    VersionResult,
)
from rate_derivatives_pricer.api.settings import Settings
from rate_derivatives_pricer.core.errors import UnsupportedInstrumentError
from rate_derivatives_pricer.services.pricer_service import Diagnostics as ServiceDiagnostics
from rate_derivatives_pricer.services.pricer_service import PricerService
from rate_derivatives_pricer.services.requests import (
    CalculationOptions,
    InlineMarket,
    MarketInput,
    RiskOptions,
    SnapshotReference,
    TradeInput,
)

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status: {"model": ErrorResponse} for status in (400, 404, 422, 500)
}

router = APIRouter()


def get_service(request: Request) -> PricerService:
    """Service instance created by the application factory."""
    service: PricerService = request.app.state.service
    return service


def get_settings(request: Request) -> Settings:
    """Settings of the running application."""
    settings: Settings = request.app.state.settings
    return settings


ServiceDependency = Annotated[PricerService, Depends(get_service)]


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
