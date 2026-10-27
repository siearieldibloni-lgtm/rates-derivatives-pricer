"""Market environment: the curves and fixings consumed by instruments and engines."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from types import MappingProxyType

from rate_derivatives_pricer.core.errors import MarketDataError
from rate_derivatives_pricer.core.market.curves import Curve


@dataclass(frozen=True, slots=True)
class PillarReport:
    """Calibration diagnostics of one pillar.

    Attributes:
        label: Quote tenor label (``"5Y"``).
        instrument_type: Calibration instrument type (``"ois_swap"``).
        pillar_date: Curve node solved for by this instrument.
        market_quote: Quoted rate (decimal).
        implied_quote: Rate implied by the calibrated curve (decimal).
        residual: ``implied_quote - market_quote``.
        iterations: Solver iterations.
        discount_factor: Discount factor found at the pillar.
    """

    label: str
    instrument_type: str
    pillar_date: date
    market_quote: float
    implied_quote: float
    residual: float
    iterations: int
    discount_factor: float


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    """Calibration diagnostics of one curve.

    Attributes:
        curve_name: Curve identifier (``"EUR-ESTR"``, ``"EUR-EURIBOR6M"``).
        interpolation: Interpolator name.
        solver: Solver name.
        pillars: Per-pillar diagnostics, by increasing pillar date.
        index_name: Key of the curve in ``MarketEnvironment.projection_curves`` (``"ESTR"``).
        is_discount_curve: Whether the curve discounts cashflows (the OIS curve).
    """

    curve_name: str
    interpolation: str
    solver: str
    pillars: tuple[PillarReport, ...]
    index_name: str = ""
    is_discount_curve: bool = False

    @property
    def max_abs_residual(self) -> float:
        """Largest absolute residual over the pillars (decimal rate)."""
        return max((abs(p.residual) for p in self.pillars), default=0.0)

    @property
    def max_iterations(self) -> int:
        """Largest number of solver iterations over the pillars."""
        return max((p.iterations for p in self.pillars), default=0)


@dataclass(frozen=True, slots=True)
class MarketEnvironment:
    """Discount curve, projection curves by index and fixings, as of a valuation date.

    Attributes:
        valuation_date: Valuation date (equal to the curves' reference date).
        currency: ISO 4217 code.
        discount_curve: Curve used to discount cashflows.
        projection_curves: Forward projection curve per index name.
        fixings: Historical fixings per index name: ``{"ESTR": {date: rate}}``.
        calibration: Calibration reports of the bootstrapped curves.
    """

    valuation_date: date
    currency: str
    discount_curve: Curve
    projection_curves: Mapping[str, Curve] = field(default_factory=dict)
    fixings: Mapping[str, Mapping[date, float]] = field(default_factory=dict)
    calibration: tuple[CalibrationReport, ...] = ()

    def __post_init__(self) -> None:
        # Read-only views: an environment is shared by the cache across requests.
        object.__setattr__(
            self, "projection_curves", MappingProxyType(dict(self.projection_curves))
        )
        object.__setattr__(
            self,
            "fixings",
            MappingProxyType({k: MappingProxyType(dict(v)) for k, v in self.fixings.items()}),
        )

    def projection_curve(self, index_name: str) -> Curve:
        """Projection curve of ``index_name``.

        Raises:
            MarketDataError: If no curve projects this index.
        """
        try:
            return self.projection_curves[index_name]
        except KeyError:
            raise MarketDataError(
                f"no projection curve for index {index_name}",
                {"index": index_name, "available": sorted(self.projection_curves)},
            ) from None

    def fixing(self, index_name: str, fixing_date: date) -> float | None:
        """Historical fixing of ``index_name`` on ``fixing_date``, or ``None`` if unknown."""
        return self.fixings.get(index_name, {}).get(fixing_date)
