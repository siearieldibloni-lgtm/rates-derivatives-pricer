"""HTTP contract of the API: Pydantic request and response schemas (docs/API.md).

This module depends on Pydantic only (no FastAPI, no core) so that the SDK can reuse it as its
typed models. Dates are ISO 8601, rates are decimals, quotes carry an explicit unit.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

QuoteUnitName = Literal["percent", "decimal", "bp"]
CacheStatusName = Literal["hit", "miss"]
SNAPSHOT_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$"


class StrictModel(BaseModel):
    """Request model rejecting unknown fields (typos must not be silently ignored)."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- market data
class QuoteModel(StrictModel):
    """Market quote of a calibration instrument."""

    type: Literal["deposit", "ois_swap"]
    tenor: str = Field(examples=["5Y"])
    value: FiniteFloat = Field(examples=[2.254])
    unit: QuoteUnitName = "decimal"


class FixingModel(StrictModel):
    """Historical index fixing."""

    date: dt.date
    value: FiniteFloat
    unit: QuoteUnitName = "decimal"


class InlineMarketData(StrictModel):
    """Market data passed by value."""

    valuation_date: dt.date
    quotes: list[QuoteModel] = Field(min_length=1)
    fixings: dict[str, list[FixingModel]] = Field(default_factory=dict)


class SnapshotReferenceModel(StrictModel):
    """Market data by reference to a stored snapshot."""

    snapshot: str = Field(pattern=SNAPSHOT_NAME_PATTERN, examples=["EUR_2026-10-19"])


class InlineMarketModel(StrictModel):
    """Market data by value."""

    inline: InlineMarketData


MarketModel = SnapshotReferenceModel | InlineMarketModel


class CalculationOptionsModel(StrictModel):
    """Curve construction options."""

    currency: str = Field(default="EUR", description="Currency of inline market data.")
    interpolation: str = Field(default="log_linear_df", examples=["log_linear_df", "linear_zero"])
    solver: str = Field(default="brent", examples=["brent", "newton"])


# --------------------------------------------------------------------------- trades
class OisSwapParams(StrictModel):
    """Parameters of an OIS (``type = "ois_swap"``); conventions come from the currency."""

    currency: str = "EUR"
    notional: float = Field(gt=0, allow_inf_nan=False, examples=[10_000_000])
    fixed_rate: FiniteFloat = Field(description="Decimal rate (0.025 = 2.5%).", examples=[0.0225])
    direction: Literal["pay_fixed", "receive_fixed"]
    maturity: str | None = Field(
        default=None, description="Tenor from the start date.", examples=["5Y"]
    )
    start_date: dt.date | None = Field(default=None, description="Spot date when omitted.")
    end_date: dt.date | None = None

    @model_validator(mode="after")
    def _one_maturity(self) -> OisSwapParams:
        if (self.maturity is None) == (self.end_date is None):
            raise ValueError("exactly one of maturity or end_date is required")
        return self


#: Parameter schema of each instrument type, also published by ``/v1/catalog``.
TRADE_PARAMS_SCHEMAS: dict[str, type[StrictModel]] = {"ois_swap": OisSwapParams}


class TradeModel(StrictModel):
    """Trade: instrument type (discriminator) and its parameters."""

    type: str = Field(examples=["ois_swap"])
    params: dict[str, Any]


# --------------------------------------------------------------------------- requests
class CurvesBuildRequest(StrictModel):
    """Body of ``POST /v1/curves/build``."""

    market: MarketModel
    options: CalculationOptionsModel = Field(default_factory=CalculationOptionsModel)


class PriceRequest(StrictModel):
    """Body of ``POST /v1/price``."""

    market: MarketModel
    trade: TradeModel
    options: CalculationOptionsModel = Field(default_factory=CalculationOptionsModel)


# --------------------------------------------------------------------------- responses
class Diagnostics(BaseModel):
    """Permanent diagnostics block of every response envelope."""

    timings_ms: dict[str, float] = Field(default_factory=dict)
    curve_cache: CacheStatusName | None = None
    versions: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    input_hashes: dict[str, str] = Field(default_factory=dict)


class Envelope[ResultT](BaseModel):
    """Successful response: ``request_id``, ``result`` and ``diagnostics``."""

    request_id: str
    result: ResultT
    diagnostics: Diagnostics


class ErrorBody(BaseModel):
    """Structured error with a stable machine code."""

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    """Error response."""

    request_id: str
    error: ErrorBody


class HealthResult(BaseModel):
    """Liveness / readiness status."""

    status: str
    checks: dict[str, bool] = Field(default_factory=dict)


class VersionResult(BaseModel):
    """Versions of the service and its runtime."""

    api: str
    core: str
    python: str
    numpy: str
    commit: str


class CurrencyInfo(BaseModel):
    """Conventions summary of a supported currency."""

    currency: str
    calendar: str
    spot_lag_days: int
    overnight_index: str


class PricingMethod(BaseModel):
    """Registered (instrument type, pricing method) pair."""

    instrument_type: str
    method: str


class CatalogResult(BaseModel):
    """Supported components, generated from the registries."""

    instrument_types: list[str]
    trade_schemas: dict[str, dict[str, Any]]
    pricing_methods: list[PricingMethod]
    interpolations: list[str]
    solvers: list[str]
    currencies: list[CurrencyInfo]
    calendars: list[str]
    day_counts: list[str]
    risk_measures: list[str]


class SnapshotListResult(BaseModel):
    """Names of the stored snapshots."""

    snapshots: list[str]


class SnapshotResult(BaseModel):
    """Content of a stored snapshot."""

    name: str
    currency: str
    valuation_date: dt.date
    description: str
    quotes: list[QuoteModel]
    fixings: dict[str, list[FixingModel]]


class SnapshotInfo(BaseModel):
    """Identity of the market data used by a calculation."""

    name: str
    currency: str
    valuation_date: dt.date


class CurveNode(BaseModel):
    """Calibrated pillar."""

    label: str
    instrument_type: str
    pillar_date: dt.date
    time: float
    market_quote: float
    implied_quote: float
    residual: float
    iterations: int
    discount_factor: float
    zero_rate: float


class CurvePoint(BaseModel):
    """Curve sample: discount factor, continuous zero rate (Act/365F), 1M forward (Act/360)."""

    date: dt.date
    time: float
    discount_factor: float
    zero_rate: float
    forward_rate: float


class CurveResult(BaseModel):
    """One bootstrapped curve with its calibration diagnostics."""

    curve_name: str
    role: str
    reference_date: dt.date
    interpolation: str
    solver: str
    max_abs_residual: float
    max_iterations: int
    nodes: list[CurveNode]
    grid: list[CurvePoint]


class CurvesBuildResult(BaseModel):
    """Result of ``POST /v1/curves/build``."""

    snapshot: SnapshotInfo
    curves: list[CurveResult]


class LegResult(BaseModel):
    """PV of one leg (signed, trade currency)."""

    name: str
    direction: Literal["pay", "receive"]
    present_value: float


class CashflowResult(BaseModel):
    """One future cashflow (signed amounts: positive = received)."""

    leg: str
    accrual_start: dt.date
    accrual_end: dt.date
    payment_date: dt.date
    year_fraction: float
    notional: float
    rate: float
    amount: float
    discount_factor: float
    present_value: float


class PriceResult(BaseModel):
    """Result of ``POST /v1/price``."""

    instrument_type: str
    currency: str
    valuation_date: dt.date
    present_value: float
    par_rate: float | None
    annuity: float
    pvbp: float
    legs: list[LegResult]
    cashflows: list[CashflowResult]
    engine: str
