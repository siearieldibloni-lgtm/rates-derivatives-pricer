"""Yield curves defined by discount factor nodes and an interpolation scheme."""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np

from rate_derivatives_pricer.core.conventions.day_count import DayCountConvention
from rate_derivatives_pricer.core.errors import MarketDataError, PricingError
from rate_derivatives_pricer.core.market.interpolation import FloatArray, Interpolator

# Horizon used for the zero rate at the reference date, where -ln P(t) / t is 0/0.
ZERO_RATE_MIN_HORIZON = timedelta(days=1)


class Curve(ABC):
    """Interest rate curve: discount factors, zero rates and forward rates by date."""

    @property
    @abstractmethod
    def reference_date(self) -> date:
        """Date at which discount factors equal 1 (the valuation date)."""

    @abstractmethod
    def discount_factors(self, dates: Sequence[date]) -> FloatArray:
        """Discount factors ``P(t_ref, d)`` for dates on or after the reference date."""

    @abstractmethod
    def time(self, d: date) -> float:
        """Curve time of ``d`` in years from the reference date (curve day count)."""

    def discount_factor(self, d: date) -> float:
        """Discount factor ``P(t_ref, d)``."""
        return float(self.discount_factors([d])[0])

    def zero_rate(self, d: date) -> float:
        """Continuously compounded zero rate ``-ln P(d) / t(d)`` (curve day count).

        At the reference date the one-day zero rate is returned.
        """
        target = max(d, self.reference_date + ZERO_RATE_MIN_HORIZON)
        return -math.log(self.discount_factor(target)) / self.time(target)

    def forward_rate(self, start: date, end: date, day_count: DayCountConvention) -> float:
        """Simply compounded forward rate ``(P(start) / P(end) - 1) / tau(start, end)``.

        Args:
            start: Start of the forward period.
            end: End of the forward period (after ``start``).
            day_count: Accrual convention of the forward rate (e.g. Act/360).
        """
        start_df, end_df = self.discount_factors([start, end])
        return float((start_df / end_df - 1.0) / day_count.year_fraction(start, end))


@dataclass(frozen=True, slots=True)
class DiscountCurve(Curve):
    """Curve defined by discount factors at pillar dates and an :class:`Interpolator`.

    A node ``(reference_date, 1.0)`` is implied. Time is measured with an internal day count
    (Act/365F by default, ADR-008).

    Attributes:
        curve_reference_date: Reference (valuation) date.
        pillar_dates: Strictly increasing pillar dates, all after the reference date.
        pillar_discount_factors: Positive discount factors at the pillars.
        interpolator: Interpolation scheme between nodes.
        day_count: Time measure of the interpolation.
        name: Identifier, e.g. ``"EUR-ESTR"``.
    """

    curve_reference_date: date
    pillar_dates: tuple[date, ...]
    pillar_discount_factors: tuple[float, ...]
    interpolator: Interpolator
    day_count: DayCountConvention
    name: str = ""
    _node_times: FloatArray = field(init=False, repr=False, compare=False)
    _node_discount_factors: FloatArray = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if len(self.pillar_dates) != len(self.pillar_discount_factors):
            raise MarketDataError("pillar dates and discount factors differ in length")
        previous = self.curve_reference_date
        for pillar, discount_factor in zip(
            self.pillar_dates, self.pillar_discount_factors, strict=True
        ):
            if pillar <= previous:
                raise MarketDataError(
                    "curve pillars must be strictly increasing and after the reference date",
                    {"curve": self.name, "pillar": pillar.isoformat()},
                )
            if not (discount_factor > 0.0 and math.isfinite(discount_factor)):
                raise MarketDataError(
                    "discount factors must be positive",
                    {"curve": self.name, "pillar": pillar.isoformat()},
                )
            previous = pillar
        times = [0.0] + [self.time(pillar) for pillar in self.pillar_dates]
        # Frozen dataclass: cache the numeric node arrays once at construction.
        object.__setattr__(self, "_node_times", np.asarray(times, dtype=np.float64))
        object.__setattr__(
            self,
            "_node_discount_factors",
            np.asarray((1.0, *self.pillar_discount_factors), dtype=np.float64),
        )

    @property
    def reference_date(self) -> date:
        """Reference (valuation) date."""
        return self.curve_reference_date

    def time(self, d: date) -> float:
        """Curve time in years (internal day count)."""
        return self.day_count.year_fraction(self.curve_reference_date, d)

    def discount_factors(self, dates: Sequence[date]) -> FloatArray:
        """Interpolated discount factors.

        Raises:
            PricingError: If a date is before the reference date.
        """
        times = np.asarray([self.time(d) for d in dates], dtype=np.float64)
        if times.size and times.min() < 0.0:
            raise PricingError(
                "discount factor requested before the curve reference date",
                {"curve": self.name, "reference_date": self.curve_reference_date.isoformat()},
            )
        return self.interpolator.discount_factors(
            times, self._node_times, self._node_discount_factors
        )

    def with_pillars(
        self, pillar_dates: tuple[date, ...], pillar_discount_factors: tuple[float, ...]
    ) -> DiscountCurve:
        """Copy of this curve with other nodes (used by the bootstrap)."""
        return DiscountCurve(
            curve_reference_date=self.curve_reference_date,
            pillar_dates=pillar_dates,
            pillar_discount_factors=pillar_discount_factors,
            interpolator=self.interpolator,
            day_count=self.day_count,
            name=self.name,
        )
