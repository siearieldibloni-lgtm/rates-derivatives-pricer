"""Service-level inputs: plain frozen dataclasses (Pydantic stays in api/ and sdk/, ADR-018).

The API validates the HTTP payload with its Pydantic schemas, then builds these objects; the
mappers turn them into core domain objects.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

DEFAULT_CURRENCY = "EUR"
DEFAULT_INTERPOLATION = "log_linear_df"
DEFAULT_SOLVER = "brent"
INLINE_SNAPSHOT_NAME = "inline"
DEFAULT_BUMP_SIZE_BP = 1.0
DEFAULT_BUMP_MODE = "central"
DEFAULT_CARRY_HORIZON = "3M"


@dataclass(frozen=True, slots=True)
class SnapshotReference:
    """Market data by reference: the name of a stored snapshot (``"EUR_2026-10-19"``)."""

    snapshot: str


@dataclass(frozen=True, slots=True)
class InlineMarket:
    """Market data by value, for reproducibility and debugging.

    Attributes:
        valuation_date: ISO date.
        quotes: ``[{"type", "tenor", "value", "unit"}]``.
        fixings: ``{"ESTR": [{"date", "value", "unit"}]}``.
    """

    valuation_date: str
    quotes: tuple[Mapping[str, Any], ...]
    fixings: Mapping[str, tuple[Mapping[str, Any], ...]] = field(default_factory=dict)


type MarketInput = SnapshotReference | InlineMarket


@dataclass(frozen=True, slots=True)
class TradeInput:
    """Trade description: registered instrument type and its parameters."""

    type: str
    params: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class CalculationOptions:
    """Curve construction and pricing options.

    Attributes:
        currency: Currency of inline market data (a referenced snapshot carries its own).
        interpolation: Interpolator registry key.
        solver: Bootstrap root finder.
    """

    currency: str = DEFAULT_CURRENCY
    interpolation: str = DEFAULT_INTERPOLATION
    solver: str = DEFAULT_SOLVER


@dataclass(frozen=True, slots=True)
class RiskOptions:
    """Risk measures to compute and how to bump the market.

    Attributes:
        measures: Names registered in the risk measure registry (``"key_rate_dv01"``...).
        bump_size_bp: Bump size in basis points (> 0).
        mode: Finite difference scheme: ``"up"``, ``"down"`` or ``"central"``.
    """

    measures: tuple[str, ...]
    bump_size_bp: float = DEFAULT_BUMP_SIZE_BP
    mode: str = DEFAULT_BUMP_MODE


@dataclass(frozen=True, slots=True)
class ScenarioOptions:
    """Scenarios to run (``/v1/risk/scenarios``).

    Attributes:
        names: Preset scenario names (``"steepener"``...), in order.
        custom: Custom scenarios ``{"name", "description", "curves", "points": [{"tenor",
            "shift_bp"}]}``, run after the presets. Every preset runs when both are empty.
    """

    names: tuple[str, ...] = ()
    custom: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class CarryOptions:
    """Carry/roll-down horizon (``/v1/risk/carry``).

    Attributes:
        horizon: Tenor of the horizon (``"3M"``).
    """

    horizon: str = DEFAULT_CARRY_HORIZON
