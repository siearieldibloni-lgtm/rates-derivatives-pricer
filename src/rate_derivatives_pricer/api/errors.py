"""Translation of errors into the structured HTTP error format (docs/API.md section 5).

``{"request_id": "...", "error": {"code": "CALIBRATION_FAILED", "message": "...", "details": {}}}``

Stack traces are logged with the ``request_id``, never returned to the client.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from rate_derivatives_pricer.api.logging_config import get_request_id
from rate_derivatives_pricer.api.schemas import ErrorBody, ErrorResponse
from rate_derivatives_pricer.core.errors import (
    CalibrationError,
    ConventionError,
    MarketDataError,
    MarketDataNotFoundError,
    PricingError,
    RatesPricerError,
    RiskComputationError,
    ScheduleError,
    UnsupportedInstrumentError,
    UnsupportedRiskMeasureError,
    UnsupportedScenarioError,
)

logger = logging.getLogger(__name__)

VALIDATION_ERROR = "VALIDATION_ERROR"
INTERNAL_ERROR = "INTERNAL_ERROR"
INTERNAL_ERROR_MESSAGE = "internal error; quote the request_id to support"

# Most specific classes first: the first matching entry wins.
_CORE_ERROR_MAPPING: tuple[tuple[type[RatesPricerError], int, str], ...] = (
    (MarketDataNotFoundError, 404, "NOT_FOUND"),
    (MarketDataError, 422, "MARKET_DATA_INVALID"),
    (ConventionError, 422, "INVALID_TRADE_DEFINITION"),
    (ScheduleError, 422, "INVALID_TRADE_DEFINITION"),
    (UnsupportedScenarioError, 400, "UNSUPPORTED_SCENARIO"),
    (UnsupportedRiskMeasureError, 400, "UNSUPPORTED_RISK_MEASURE"),
    (UnsupportedInstrumentError, 400, "UNSUPPORTED_INSTRUMENT"),
    (CalibrationError, 422, "CALIBRATION_FAILED"),
    (PricingError, 500, "COMPUTATION_FAILED"),
    (RiskComputationError, 500, "COMPUTATION_FAILED"),
)
_HTTP_STATUS_CODES = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}


def error_response(
    status_code: int, code: str, message: str, details: dict[str, Any] | None = None
) -> JSONResponse:
    """Build the structured error response for the current request."""
    body = ErrorResponse(
        request_id=get_request_id(),
        error=ErrorBody(code=code, message=message, details=jsonable_encoder(details or {})),
    )
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


def core_error_status(error: RatesPricerError) -> tuple[int, str]:
    """HTTP status and stable code of a core error."""
    for error_type, status_code, code in _CORE_ERROR_MAPPING:
        if isinstance(error, error_type):
            return status_code, code
    return 500, INTERNAL_ERROR  # pragma: no cover - every core error has an entry


def _count_error(request: Request, code: str) -> None:
    """Feed the ``pricer_errors_total`` metric when the application has metrics."""
    metrics = getattr(request.app.state, "metrics", None)
    if metrics is not None:
        metrics.count_error(code)


async def _core_error_handler(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, RatesPricerError)
    status_code, code = core_error_status(error)
    _count_error(request, code)
    level = logging.ERROR if status_code >= 500 else logging.WARNING
    logger.log(
        level,
        "request_rejected",
        extra={"error_code": code, "error_type": type(error).__name__, "error": error.message},
        exc_info=status_code >= 500,
    )
    return error_response(status_code, code, error.message, error.details)


async def _validation_error_handler(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, RequestValidationError)
    errors = [
        {"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
        for e in error.errors()
    ]
    logger.warning("request_rejected", extra={"error_code": VALIDATION_ERROR, "errors": errors})
    _count_error(request, VALIDATION_ERROR)
    return error_response(422, VALIDATION_ERROR, "request validation failed", {"errors": errors})


async def _http_error_handler(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, StarletteHTTPException)
    code = _HTTP_STATUS_CODES.get(error.status_code, "HTTP_ERROR")
    _count_error(request, code)
    return error_response(error.status_code, code, str(error.detail))


def register_error_handlers(app: FastAPI) -> None:
    """Install the handlers translating errors into structured responses.

    Unexpected exceptions are handled by :class:`RequestContextMiddleware`, which still knows
    the ``request_id``.
    """
    app.add_exception_handler(RatesPricerError, _core_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)
