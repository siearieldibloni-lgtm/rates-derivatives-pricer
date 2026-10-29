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
IBOR_QUOTE_TYPES = ("ibor_deposit", "ibor_swap")


class QuoteModel(StrictModel):
    """Market quote of a calibration instrument.

    ``deposit`` and ``ois_swap`` calibrate the overnight curve. ``ibor_deposit`` and
    ``ibor_swap`` calibrate the projection curve of the Ibor ``index`` they name.
    """

    type: Literal["deposit", "ois_swap", "ibor_deposit", "ibor_swap"]
    tenor: str = Field(examples=["5Y"])
    value: FiniteFloat = Field(examples=[2.254])
    unit: QuoteUnitName = "decimal"
    index: str | None = Field(
        default=None, description="Ibor index of an ibor_* quote.", examples=["EURIBOR6M"]
    )

    @model_validator(mode="after")
    def _index_matches_type(self) -> QuoteModel:
        if self.type in IBOR_QUOTE_TYPES and self.index is None:
            raise ValueError(f"quote type {self.type} requires an index")
        if self.type not in IBOR_QUOTE_TYPES and self.index is not None:
            raise ValueError(f"quote type {self.type} takes no index")
        return self


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


class VanillaIrsParams(StrictModel):
    """Parameters of a vanilla IRS (``type = "vanilla_irs"``): fixed leg vs an Ibor index."""

    currency: str = "EUR"
    index: str | None = Field(
        default=None,
        description="Ibor index of the floating leg; the currency's default when omitted.",
        examples=["EURIBOR6M", "EURIBOR3M"],
    )
    notional: float = Field(gt=0, allow_inf_nan=False, examples=[10_000_000])
    fixed_rate: FiniteFloat = Field(description="Decimal rate (0.025 = 2.5%).", examples=[0.025])
    direction: Literal["pay_fixed", "receive_fixed"]
    maturity: str | None = Field(
        default=None, description="Tenor from the start date.", examples=["5Y"]
    )
    start_date: dt.date | None = Field(default=None, description="Spot date when omitted.")
    end_date: dt.date | None = None

    @model_validator(mode="after")
    def _one_maturity(self) -> VanillaIrsParams:
        if (self.maturity is None) == (self.end_date is None):
            raise ValueError("exactly one of maturity or end_date is required")
        return self


#: Parameter schema of each instrument type, also published by ``/v1/catalog``.
TRADE_PARAMS_SCHEMAS: dict[str, type[StrictModel]] = {
    "ois_swap": OisSwapParams,
    "vanilla_irs": VanillaIrsParams,
}


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


class BumpOptionsModel(StrictModel):
    """How the market is shocked for the risk measures."""

    size_bp: float = Field(
        default=1.0, gt=0, allow_inf_nan=False, description="Bump size in basis points."
    )
    mode: Literal["up", "down", "central"] = Field(
        default="central", description="Finite difference scheme."
    )


class RiskRequest(StrictModel):
    """Body of ``POST /v1/risk``."""

    market: MarketModel
    trade: TradeModel
    measures: list[str] = Field(
        min_length=1, examples=[["parallel_dv01", "key_rate_dv01"]], description="See /v1/catalog."
    )
    bump: BumpOptionsModel = Field(default_factory=BumpOptionsModel)
    options: CalculationOptionsModel = Field(default_factory=CalculationOptionsModel)


TENOR_PATTERN = r"^[1-9][0-9]*[DdWwMmYy]$"


class ScenarioPointModel(StrictModel):
    """Shift of the par rates at one tenor (linear in between, flat beyond the ends)."""

    tenor: str = Field(examples=["2Y"])
    shift_bp: FiniteFloat = Field(description="Additive par-rate shift in bp.", examples=[-25.0])


class ScenarioModel(StrictModel):
    """Custom scenario: a piecewise-linear shift of the par rates by tenor (ADR-033)."""

    name: str = Field(min_length=1, examples=["bear_steepener"])
    description: str = ""
    curves: list[str] | None = Field(
        default=None,
        description='Curves shocked ("OIS", "EURIBOR6M"...); every curve when omitted.',
    )
    points: list[ScenarioPointModel] = Field(min_length=1)


class ScenariosRequest(StrictModel):
    """Body of ``POST /v1/risk/scenarios``. Every preset runs when no scenario is given."""

    market: MarketModel
    trade: TradeModel
    scenarios: list[str] = Field(
        default_factory=list,
        examples=[["parallel_up", "steepener"]],
        description="Preset scenarios, see /v1/catalog.",
    )
    custom_scenarios: list[ScenarioModel] = Field(default_factory=list)
    options: CalculationOptionsModel = Field(default_factory=CalculationOptionsModel)


class CarryRequest(StrictModel):
    """Body of ``POST /v1/risk/carry`` (unchanged curve hypothesis, ADR-034)."""

    market: MarketModel
    trade: TradeModel
    horizon: str = Field(default="3M", pattern=TENOR_PATTERN, examples=["3M", "1Y"])
    options: CalculationOptionsModel = Field(default_factory=CalculationOptionsModel)


#: Maximum number of trades of one ``/v1/batch/price`` request (bounded memory and latency).
BATCH_MAX_TRADES = 1000
TRADE_ID_MAX_LENGTH = 128


class BatchTradeModel(TradeModel):
    """Trade of a batch, with an optional identifier echoed in its result (index otherwise)."""

    id: str | None = Field(
        default=None, min_length=1, max_length=TRADE_ID_MAX_LENGTH, examples=["swap-001"]
    )


class BatchPriceRequest(StrictModel):
    """Body of ``POST /v1/batch/price``: several trades priced on one market."""

    market: MarketModel
    trades: list[BatchTradeModel] = Field(min_length=1, max_length=BATCH_MAX_TRADES)
    options: CalculationOptionsModel = Field(default_factory=CalculationOptionsModel)

    @model_validator(mode="after")
    def _unique_ids(self) -> BatchPriceRequest:
        ids = [trade.id for trade in self.trades if trade.id is not None]
        duplicates = sorted({trade_id for trade_id in ids if ids.count(trade_id) > 1})
        if duplicates:
            raise ValueError(f"duplicate trade ids: {duplicates}")
        return self


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
    ibor_indices: list[str] = Field(default_factory=list)


class PricingMethod(BaseModel):
    """Registered (instrument type, pricing method) pair."""

    instrument_type: str
    method: str


class ScenarioInfo(BaseModel):
    """Preset scenario."""

    name: str
    description: str
    points: list[ScenarioPointModel]


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
    scenarios: list[ScenarioInfo] = Field(default_factory=list)


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


class RiskBucketResult(BaseModel):
    """Sensitivity of one bucket: a calibration quote, or a whole curve."""

    curve: str
    label: str
    instrument_type: str
    value: float


class RiskMeasureResult(BaseModel):
    """One risk measure: headline value, unit and breakdown by bucket."""

    measure: str
    value: float
    unit: str
    bump_count: int
    duration_ms: float
    buckets: list[RiskBucketResult]


class RiskResult(BaseModel):
    """Result of ``POST /v1/risk``."""

    instrument_type: str
    currency: str
    valuation_date: dt.date
    present_value: float
    bump: BumpOptionsModel
    measures: list[RiskMeasureResult]


class QuoteShiftResult(BaseModel):
    """Shift a scenario applied to one calibration quote."""

    curve: str
    label: str
    instrument_type: str
    shift_bp: float


class ScenarioResultModel(BaseModel):
    """Full revaluation under one scenario."""

    name: str
    description: str
    curves: list[str] | None
    points: list[ScenarioPointModel]
    present_value: float
    pnl: float
    shifts: list[QuoteShiftResult]


class ScenariosResult(BaseModel):
    """Result of ``POST /v1/risk/scenarios``."""

    instrument_type: str
    currency: str
    valuation_date: dt.date
    present_value: float
    scenarios: list[ScenarioResultModel]


class CarryResult(BaseModel):
    """Result of ``POST /v1/risk/carry`` (trade currency; total = carry + roll-down)."""

    instrument_type: str
    currency: str
    valuation_date: dt.date
    hypothesis: str
    horizon: str
    horizon_date: dt.date
    present_value: float
    horizon_present_value: float
    cashflows: float
    total: float
    carry: float
    rolldown: float
    assumed_fixings: dict[str, float]


class BatchItemResult(BaseModel):
    """Outcome of one trade of a batch: ``result`` when ``status == "ok"``, ``error`` otherwise."""

    trade_id: str
    status: Literal["ok", "error"]
    result: PriceResult | None = None
    error: ErrorBody | None = None
    trade_hash: str | None = None
    duration_ms: float = 0.0


class BatchSummary(BaseModel):
    """Counts of a batch; ``total_present_value`` sums the trades priced successfully."""

    trade_count: int
    succeeded: int
    failed: int
    total_present_value: float


class BatchPriceResult(BaseModel):
    """Result of ``POST /v1/batch/price`` (trades in request order)."""

    valuation_date: dt.date
    currency: str
    summary: BatchSummary
    results: list[BatchItemResult]


class CacheStatsResult(BaseModel):
    """Result of ``GET /v1/cache/stats`` (per process: each worker has its own cache)."""

    hits: int
    misses: int
    evictions: int
    size: int
    max_size: int
    ttl_seconds: float | None
    hit_ratio: float | None = Field(description="hits / (hits + misses); null before any lookup.")


class CacheClearResult(BaseModel):
    """Result of ``DELETE /v1/cache``."""

    entries_dropped: int


class LogEvent(BaseModel):
    """One log event of the in-memory buffer (same fields as the JSON logs, no stack trace)."""

    model_config = ConfigDict(extra="allow")

    timestamp: dt.datetime
    level: str
    logger: str
    event: str
    request_id: str


class LogBufferInfo(BaseModel):
    """State of the in-memory log buffer of the process that answered."""

    size: int
    capacity: int
    evicted: int


class LogsResult(BaseModel):
    """Result of ``GET /v1/logs/{request_id}``: the timeline of one request, oldest first."""

    request_id: str
    event_count: int
    duration_ms: float = Field(description="Time between the first and the last event.")
    events: list[LogEvent]
    buffer: LogBufferInfo
