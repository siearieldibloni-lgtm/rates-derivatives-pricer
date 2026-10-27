"""USD added by configuration (phase 3): US calendars, SOFR curve and SOFR OIS against QuantLib.

Same grids and the same tolerances as the EUR validation (ROADMAP phase 3): the only USD-specific
inputs are the snapshot ``USD_2026-10-19`` and ``core/conventions/data/USD.json`` (ADR-032).
"""

from __future__ import annotations

import itertools
from datetime import date, timedelta

import pytest
import QuantLib as ql

from rate_derivatives_pricer.core.conventions.business_day import BusinessDayConvention
from rate_derivatives_pricer.core.conventions.calendar import (
    Calendar,
    UsGovernmentBondCalendar,
    UsSofrCalendar,
)
from rate_derivatives_pricer.core.conventions.currency import get_conventions
from rate_derivatives_pricer.core.conventions.tenor import Tenor
from rate_derivatives_pricer.core.engines.base import engine_for
from rate_derivatives_pricer.core.instruments.cashflows import OvernightCompoundedCoupon
from rate_derivatives_pricer.core.instruments.swaps import OisSwap
from rate_derivatives_pricer.core.market.bootstrap import CurveBuilder, CurveBuildOptions
from rate_derivatives_pricer.core.market.environment import MarketEnvironment
from rate_derivatives_pricer.core.market.snapshot import MarketSnapshot
from rate_derivatives_pricer.core.registry import INSTRUMENTS
from tests.validation.market_grid import (
    USD_REFERENCE_SNAPSHOT,
    market_grid,
    reference_snapshot,
)
from tests.validation.quantlib_oracle import (
    BDC,
    US_GOVERNMENT_BOND_CALENDAR,
    US_SOFR_CALENDAR,
    build_estr_curve,
    evaluation_date,
    from_ql,
    make_ois,
    to_ql,
)
from tests.validation.tolerances import CASHFLOW_REL, DISCOUNT_FACTOR_ABS, SWAP_REL

pytestmark = pytest.mark.validation

BASIS_POINT = 1e-4
INTERPOLATIONS = ["log_linear_df", "linear_zero"]
GRID = market_grid(USD_REFERENCE_SNAPSHOT)
CURVE_SAMPLE_STEP_DAYS = 11
CURVE_SAMPLE_HORIZON_DAYS = 40 * 365


def _daily(start: date, end: date, step: int = 1) -> list[date]:
    return [start + timedelta(days=k) for k in range(0, (end - start).days + 1, step)]


def _ids(snapshot: MarketSnapshot) -> str:
    return snapshot.name


# --------------------------------------------------------------------------- calendars
CALENDAR_PAIRS = [
    (UsGovernmentBondCalendar(), US_GOVERNMENT_BOND_CALENDAR),
    (UsSofrCalendar(), US_SOFR_CALENDAR),
]


@pytest.mark.parametrize(("ours", "reference"), CALENDAR_PAIRS, ids=lambda x: str(x))
def test_us_business_days_match_quantlib_over_60_years(
    ours: Calendar, reference: ql.Calendar
) -> None:
    mismatches = [
        d
        for d in _daily(date(1995, 1, 1), date(2060, 12, 31))
        if ours.is_business_day(d) != reference.isBusinessDay(to_ql(d))
    ]
    assert mismatches == []


@pytest.mark.parametrize("label", ["1D", "2D", "1W", "1M", "3M", "1Y", "5Y", "30Y"])
def test_sofr_calendar_advance_matches_quantlib(label: str) -> None:
    ours, tenor = UsSofrCalendar(), Tenor.parse(label)
    for d in _daily(date(2025, 1, 1), date(2028, 12, 31), step=3):
        for end_of_month in (False, True):
            expected = US_SOFR_CALENDAR.advance(
                to_ql(d), ql.Period(label), BDC["MODIFIED_FOLLOWING"], end_of_month
            )
            assert ours.advance(
                d, tenor, BusinessDayConvention.MODIFIED_FOLLOWING, end_of_month
            ) == from_ql(expected), d


def test_usd_conventions_are_configuration_only() -> None:
    usd = get_conventions("USD")
    assert usd.overnight_index.name == "SOFR"
    assert usd.calendar.name == "US_SOFR"
    assert usd.ois.payment_lag_days == 2
    assert usd.ibor_indices == ()  # no term index: vanilla IRS are refused in USD


# --------------------------------------------------------------------------- SOFR curve
@pytest.mark.parametrize("interpolation", INTERPOLATIONS)
@pytest.mark.parametrize("snapshot", GRID, ids=_ids)
def test_sofr_curve_discount_factors_match_quantlib(
    snapshot: MarketSnapshot, interpolation: str
) -> None:
    env = CurveBuilder(CurveBuildOptions(interpolation=interpolation)).build(snapshot)
    pillars = env.calibration[0].pillars
    with evaluation_date(snapshot.valuation_date):
        reference = build_estr_curve(snapshot, interpolation)
        assert [from_ql(d) for d in reference.dates()][1:] == [p.pillar_date for p in pillars]
        for pillar in pillars:
            assert pillar.discount_factor == pytest.approx(
                reference.discount(to_ql(pillar.pillar_date)), abs=DISCOUNT_FACTOR_ABS
            ), pillar.label
        samples = [
            snapshot.valuation_date + timedelta(days=k)
            for k in range(0, CURVE_SAMPLE_HORIZON_DAYS, CURVE_SAMPLE_STEP_DAYS)
        ]
        ours = env.discount_curve.discount_factors(samples)
        for d, discount_factor in zip(samples, ours, strict=True):
            assert discount_factor == pytest.approx(
                reference.discount(to_ql(d)), abs=DISCOUNT_FACTOR_ABS
            ), d


# --------------------------------------------------------------------------- SOFR OIS
MATURITIES = ["1W", "1M", "3M", "6M", "1Y", "18M", "2Y", "5Y", "7Y", "10Y", "15Y", "20Y", "30Y"]
DIRECTIONS = ["pay_fixed", "receive_fixed"]
NOTIONALS = [1e6, 2.5e8]
FIXED_RATES = [-0.005, 0.035, 0.05]
# Seasoned and forward swaps; 2026-03-30 accrues over Good Friday (2026-04-03, no SOFR fixing).
SEASONED = [
    (date(2026, 1, 15), "1Y"),
    (date(2026, 3, 30), "2Y"),
    (date(2026, 8, 31), "18M"),
    (date(2026, 10, 19), "6M"),
    (date(2026, 10, 1), "10Y"),
    (date(2027, 1, 29), "5Y"),
]


@pytest.fixture(scope="module")
def environments() -> dict[str, MarketEnvironment]:
    return {s.name: CurveBuilder().build(s) for s in GRID}


def _compare_swap(
    snapshot: MarketSnapshot,
    env: MarketEnvironment,
    curve: ql.YieldTermStructure,
    params: dict[str, object],
    start: date | None,
) -> None:
    """Compare one SOFR OIS; must run inside ``evaluation_date(snapshot.valuation_date)``."""
    notional, fixed_rate = float(params["notional"]), float(params["fixed_rate"])  # type: ignore[arg-type]
    swap = INSTRUMENTS.get("ois_swap")({**params, "currency": "USD"}, snapshot.valuation_date)
    assert isinstance(swap, OisSwap)
    result = engine_for("ois_swap").price(swap, env)
    reference = make_ois(
        curve,
        snapshot.fixings_by_index().get("SOFR", {}),
        notional,
        fixed_rate,
        params["direction"] == "pay_fixed",
        str(params["maturity"]),
        start,
        currency="USD",
    )
    ql_dates = [
        (from_ql(c.accrualStartDate()), from_ql(c.accrualEndDate()), from_ql(c.date()))
        for c in map(ql.as_coupon, reference.overnightLeg())
    ]
    assert [
        (c.accrual_start, c.accrual_end, c.payment_date) for c in swap.overnight_coupons
    ] == ql_dates
    assert result.present_value / notional == pytest.approx(
        reference.NPV() / notional, abs=SWAP_REL
    )
    assert result.par_rate == pytest.approx(reference.fairRate(), rel=SWAP_REL)
    assert result.annuity == pytest.approx(abs(reference.fixedLegBPS()) / BASIS_POINT, rel=SWAP_REL)
    ql_amounts = {
        from_ql(cf.date()): cf.amount()
        for cf in reference.overnightLeg()
        if from_ql(cf.date()) > snapshot.valuation_date
    }
    for coupon in swap.overnight_coupons:
        if coupon.payment_date > snapshot.valuation_date:
            assert isinstance(coupon, OvernightCompoundedCoupon)
            assert coupon.amount(env) == pytest.approx(
                ql_amounts[coupon.payment_date], rel=CASHFLOW_REL, abs=CASHFLOW_REL * notional
            )


@pytest.mark.parametrize("snapshot", GRID, ids=_ids)
def test_spot_sofr_swaps_match_quantlib(
    snapshot: MarketSnapshot, environments: dict[str, MarketEnvironment]
) -> None:
    env = environments[snapshot.name]
    with evaluation_date(snapshot.valuation_date):
        curve = build_estr_curve(snapshot)
        for maturity, direction, notional, fixed_rate in itertools.product(
            MATURITIES, DIRECTIONS, NOTIONALS, FIXED_RATES
        ):
            params: dict[str, object] = {
                "notional": notional,
                "fixed_rate": fixed_rate,
                "direction": direction,
                "maturity": maturity,
            }
            _compare_swap(snapshot, env, curve, params, None)


@pytest.mark.parametrize("direction", DIRECTIONS)
def test_seasoned_and_forward_sofr_swaps_match_quantlib(direction: str) -> None:
    snapshot = reference_snapshot(USD_REFERENCE_SNAPSHOT)
    env = CurveBuilder().build(snapshot)
    with evaluation_date(snapshot.valuation_date):
        curve = build_estr_curve(snapshot)
        for start, maturity in SEASONED:
            params: dict[str, object] = {
                "notional": 1e7,
                "fixed_rate": 0.036,
                "direction": direction,
                "maturity": maturity,
                "start_date": start.isoformat(),
            }
            _compare_swap(snapshot, env, curve, params, start)
