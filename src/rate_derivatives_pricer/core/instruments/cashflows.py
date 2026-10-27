"""Polymorphic cashflows: each cashflow computes its own amount from a :class:`MarketEnvironment`.

Amounts are unsigned (receive convention); the :class:`Leg` applies the pay/receive sign.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Self

from rate_derivatives_pricer.core.conventions.day_count import DayCountConvention
from rate_derivatives_pricer.core.errors import MarketDataError
from rate_derivatives_pricer.core.market.environment import MarketEnvironment
from rate_derivatives_pricer.core.market.indices import IborIndex, OvernightIndex


class Cashflow(ABC):
    """A single payment; concrete classes are frozen dataclasses with a ``payment_date`` field."""

    #: Date on which the amount is paid.
    payment_date: date

    @abstractmethod
    def amount(self, env: MarketEnvironment) -> float:
        """Unsigned amount paid on ``payment_date``, in the leg currency."""

    @classmethod
    def amounts(cls, cashflows: Sequence[Self], env: MarketEnvironment) -> list[float]:
        """Amounts of several cashflows of this type (hook for vectorised implementations).

        The engines call this for whole legs; the default simply loops over :meth:`amount`.
        """
        return [cashflow.amount(env) for cashflow in cashflows]


@dataclass(frozen=True, slots=True)
class Coupon(Cashflow):
    """Interest accrued on a notional over an accrual period: ``N * rate * tau``.

    Attributes:
        notional: Notional on which interest accrues (positive).
        accrual_start: Accrual start (included).
        accrual_end: Accrual end (excluded).
        payment_date: Payment date.
        day_count: Accrual day count convention.
    """

    notional: float
    accrual_start: date
    accrual_end: date
    payment_date: date
    day_count: DayCountConvention
    _year_fraction: float = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # Computed once: the coupon is immutable and bootstraps re-evaluate it thousands of times.
        object.__setattr__(
            self,
            "_year_fraction",
            self.day_count.year_fraction(self.accrual_start, self.accrual_end),
        )

    @property
    def year_fraction(self) -> float:
        """Accrual year fraction ``tau(accrual_start, accrual_end)``."""
        return self._year_fraction

    @abstractmethod
    def rate(self, env: MarketEnvironment) -> float:
        """Coupon rate (decimal) over the accrual period."""

    def amount(self, env: MarketEnvironment) -> float:
        """``N * rate * tau``."""
        return self.notional * self.rate(env) * self.year_fraction


@dataclass(frozen=True, slots=True)
class FixedCoupon(Coupon):
    """Fixed-rate coupon: ``N * K * tau``.

    Attributes:
        fixed_rate: Fixed rate ``K`` (decimal).
    """

    fixed_rate: float

    def rate(self, env: MarketEnvironment) -> float:
        """The fixed rate (independent of the market)."""
        return self.fixed_rate


@dataclass(frozen=True, slots=True)
class OvernightCompoundedCoupon(Coupon):
    """Overnight rate compounded daily in arrears (no lookback, lockout or observation shift).

    With business days ``d_0 = start < d_1 < ... < d_n = end`` of the index calendar and
    overnight fixings ``r_i`` (fixing of ``d_i``, applying over ``[d_i, d_{i+1})``)::

        compound factor  C = prod_i (1 + r_i * delta_i),   delta_i = tau_index(d_i, d_{i+1})
        coupon rate      R = (C - 1) / tau(start, end)
        amount           N * R * tau = N * (C - 1)

    Fixings strictly before the valuation date must be in the environment. The fixing of the
    valuation date is used if available, otherwise projected (as QuantLib). Projected overnight
    forwards telescope: their product from ``d_k`` to ``end`` equals ``P(d_k) / P(end)`` on the
    projection curve, which is exact and avoids a daily loop.

    Attributes:
        index: Overnight index (fixing calendar, day count, fixings key).
    """

    index: OvernightIndex

    def compound_factor(self, env: MarketEnvironment) -> float:
        """Compound factor ``C`` over the accrual period (realised fixings, then projection).

        Raises:
            MarketDataError: If a fixing before the valuation date is missing.
        """
        valuation_date = env.valuation_date
        curve = env.projection_curve(self.index.name)
        if self.accrual_start > valuation_date:
            # Entirely in the future: telescoped projection.
            start_df, end_df = curve.discount_factors([self.accrual_start, self.accrual_end])
            return float(start_df / end_df)

        # Observation dates d_0 .. d_{n-1}; the end date closes the last overnight period.
        observation_dates = [
            d
            for d in self.index.fixing_calendar.business_days(self.accrual_start, self.accrual_end)
            if d < self.accrual_end
        ]
        if not observation_dates or observation_dates[0] != self.accrual_start:
            observation_dates.insert(0, self.accrual_start)
        period_ends = [*observation_dates[1:], self.accrual_end]

        factor, i, n = 1.0, 0, len(observation_dates)
        # Realised part: fixings strictly before the valuation date are mandatory.
        while i < n and observation_dates[i] < valuation_date:
            factor *= self._realised_growth(env, observation_dates[i], period_ends[i])
            i += 1
        # Fixing of the valuation date itself: used if already known, projected otherwise.
        if i < n and observation_dates[i] == valuation_date:
            todays_fixing = env.fixing(self.index.name, valuation_date)
            if todays_fixing is not None:
                factor *= 1.0 + todays_fixing * self.index.day_count.year_fraction(
                    observation_dates[i], period_ends[i]
                )
                i += 1
        # Projected part, telescoped on the projection curve.
        if i < n:
            start_df, end_df = curve.discount_factors([observation_dates[i], self.accrual_end])
            factor *= float(start_df / end_df)
        return factor

    def _realised_growth(
        self, env: MarketEnvironment, fixing_date: date, period_end: date
    ) -> float:
        """``1 + r_i * delta_i`` for a mandatory past fixing."""
        fixing = env.fixing(self.index.name, fixing_date)
        if fixing is None:
            raise MarketDataError(
                f"missing {self.index.name} fixing",
                {"index": self.index.name, "date": fixing_date.isoformat()},
            )
        return 1.0 + fixing * self.index.day_count.year_fraction(fixing_date, period_end)

    def rate(self, env: MarketEnvironment) -> float:
        """Compounded rate ``(C - 1) / tau``."""
        return (self.compound_factor(env) - 1.0) / self.year_fraction

    def amount(self, env: MarketEnvironment) -> float:
        """``N * (C - 1)``."""
        return self.notional * (self.compound_factor(env) - 1.0)


@dataclass(frozen=True, slots=True)
class IborCoupon(Coupon):
    """Coupon fixed in advance on an Ibor index (Euribor 3M / 6M).

    The rate is observed on ``fixing_date``, ``index.fixing_days`` business days before the start
    of the accrual period, and applies to the whole period::

        realised   R = published fixing
        projected  R = (P(start) / P(end) - 1) / tau_index(start, end)
        amount       = N * R * tau

    ``P`` is the discount factor of the index *projection* curve and ``tau_index`` the index day
    count (QuantLib's "par coupon" convention: the forward is read over the accrual period itself,
    not over the index's own spot-based period).

    Fixings strictly before the valuation date are mandatory. The fixing of the valuation date is
    used if available and projected otherwise, as for overnight coupons (ADR-020).

    Attributes:
        index: Ibor index (projection curve key, fixing calendar and day count).
        fixing_date: Date on which the rate is fixed.
    """

    index: IborIndex
    fixing_date: date

    def rate(self, env: MarketEnvironment) -> float:
        """Realised fixing if known, else the forward rate of the projection curve.

        Raises:
            MarketDataError: If the fixing date is before the valuation date and no fixing is
                available.
        """
        fixing = env.fixing(self.index.name, self.fixing_date)
        if self.fixing_date < env.valuation_date:
            if fixing is None:
                raise MarketDataError(
                    f"missing {self.index.name} fixing",
                    {"index": self.index.name, "date": self.fixing_date.isoformat()},
                )
            return fixing
        if self.fixing_date == env.valuation_date and fixing is not None:
            return fixing
        return env.projection_curve(self.index.name).forward_rate(
            self.accrual_start, self.accrual_end, self.index.day_count
        )

    @classmethod
    def amounts(cls, cashflows: Sequence[Self], env: MarketEnvironment) -> list[float]:
        """Amounts of several Ibor coupons, projecting all forwards with one curve evaluation.

        Same result as looping over :meth:`amount`; the discount factors of every projected
        coupon of an index are read in a single vectorised call, which dominates the cost of a
        bootstrap and of every bump-and-reprice.
        """
        rates: dict[int, float] = {}
        projected: dict[str, list[int]] = {}
        for position, coupon in enumerate(cashflows):
            fixing = env.fixing(coupon.index.name, coupon.fixing_date)
            if coupon.fixing_date < env.valuation_date:
                # Realised: a missing fixing raises through the scalar path.
                rates[position] = coupon.rate(env)
            elif coupon.fixing_date == env.valuation_date and fixing is not None:
                rates[position] = fixing
            else:
                projected.setdefault(coupon.index.name, []).append(position)
        for index_name, positions in projected.items():
            curve = env.projection_curve(index_name)
            coupons = [cashflows[position] for position in positions]
            factors = curve.discount_factors(
                [c.accrual_start for c in coupons] + [c.accrual_end for c in coupons]
            )
            count = len(coupons)
            for k, (position, coupon) in enumerate(zip(positions, coupons, strict=True)):
                # Simply compounded forward (P(start) / P(end) - 1) / tau_index.
                tau_index = coupon.index.day_count.year_fraction(
                    coupon.accrual_start, coupon.accrual_end
                )
                rates[position] = float(factors[k] / factors[count + k] - 1.0) / tau_index
        return [
            coupon.notional * rates[position] * coupon.year_fraction
            for position, coupon in enumerate(cashflows)
        ]
