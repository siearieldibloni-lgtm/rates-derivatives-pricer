"""Shared builders for unit tests (flat curves and environments)."""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path

from rate_derivatives_pricer.core.conventions.day_count import Act365Fixed
from rate_derivatives_pricer.core.market.curves import DiscountCurve
from rate_derivatives_pricer.core.market.environment import MarketEnvironment
from rate_derivatives_pricer.core.market.interpolation import LogLinearOnDiscountFactor

MARKET_DIR = Path(__file__).resolve().parents[2] / "data" / "market"
REFERENCE_SNAPSHOT = "EUR_2026-10-19"
VALUATION_DATE = date(2026, 10, 19)


def flat_curve(rate: float, reference_date: date = VALUATION_DATE) -> DiscountCurve:
    """Continuously compounded flat curve with a 50-year pillar (exact under log-linear DF)."""
    day_count = Act365Fixed()
    pillar = date(reference_date.year + 50, reference_date.month, 1)
    return DiscountCurve(
        curve_reference_date=reference_date,
        pillar_dates=(pillar,),
        pillar_discount_factors=(
            math.exp(-rate * day_count.year_fraction(reference_date, pillar)),
        ),
        interpolator=LogLinearOnDiscountFactor(),
        day_count=day_count,
        name="FLAT",
    )


def flat_environment(
    rate: float, fixings: dict[date, float] | None = None, valuation_date: date = VALUATION_DATE
) -> MarketEnvironment:
    curve = flat_curve(rate, valuation_date)
    return MarketEnvironment(
        valuation_date=valuation_date,
        currency="EUR",
        discount_curve=curve,
        projection_curves={"ESTR": curve},
        fixings={"ESTR": fixings or {}},
    )
