"""Typed exception hierarchy of the quant core: one exception per failure mode.

The API layer translates each class into a stable HTTP error code (docs/API.md section 5), so the
classes below are part of the public contract of ``core``.
"""

from __future__ import annotations

from typing import Any


class RatesPricerError(Exception):
    """Base class of every error raised by the quant core.

    Args:
        message: Human-readable description, in English.
        details: Machine-readable context (pillar, residual, field name...), JSON-serializable.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = dict(details or {})


class ConventionError(RatesPricerError):
    """Unknown or inconsistent convention (calendar, day count, tenor, currency)."""


class ScheduleError(RatesPricerError):
    """A schedule cannot be generated from the given dates and rules."""


class MarketDataError(RatesPricerError):
    """Market data is missing, malformed or inconsistent (quotes, fixings, snapshot)."""


class MarketDataNotFoundError(MarketDataError):
    """A referenced market snapshot does not exist (mapped to HTTP 404 by the API)."""


class CalibrationError(RatesPricerError):
    """Curve bootstrap failed on a pillar.

    Args:
        message: Human-readable description.
        pillar: Label of the failing calibration instrument (e.g. ``"5Y"``).
        residual: Last residual of the solver, in quote units (decimal rate).
    """

    def __init__(self, message: str, pillar: str, residual: float | None = None) -> None:
        super().__init__(message, {"pillar": pillar, "residual": residual})
        self.pillar = pillar
        self.residual = residual


class UnsupportedInstrumentError(RatesPricerError):
    """No factory or pricing engine is registered for the requested instrument type."""


class PricingError(RatesPricerError):
    """Pricing failed for a reason other than invalid inputs."""


class RiskComputationError(RatesPricerError):
    """A risk measure could not be computed."""
