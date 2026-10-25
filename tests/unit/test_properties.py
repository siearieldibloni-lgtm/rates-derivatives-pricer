"""Property-based tests (hypothesis) of curves and OIS pricing (docs/CONVENTIONS.md section 4)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rate_derivatives_pricer.core.conventions.currency import CurrencyConventions, get_conventions
from rate_derivatives_pricer.core.conventions.day_count import Act360
from rate_derivatives_pricer.core.conventions.tenor import Tenor, TimeUnit
from rate_derivatives_pricer.core.engines.discounting import DiscountingSwapEngine
from rate_derivatives_pricer.core.instruments.factories import build_spot_ois_swap
from rate_derivatives_pricer.core.instruments.swaps import SwapDirection
from rate_derivatives_pricer.core.market.bootstrap import CurveBuilder
from rate_derivatives_pricer.core.market.environment import MarketEnvironment
from rate_derivatives_pricer.core.market.providers import FileMarketDataProvider
from rate_derivatives_pricer.core.market.snapshot import MarketSnapshot
from tests.unit.helpers import MARKET_DIR, REFERENCE_SNAPSHOT, VALUATION_DATE
from tests.validation.tolerances import PAR_SWAP_PV_PER_NOTIONAL

BASIS_POINT = 1e-4
EUR: CurrencyConventions = get_conventions("EUR")
SNAPSHOT: MarketSnapshot = FileMarketDataProvider(MARKET_DIR).load_snapshot(REFERENCE_SNAPSHOT)
ENV: MarketEnvironment = CurveBuilder().build(SNAPSHOT)
ENGINE = DiscountingSwapEngine()

days_ahead = st.integers(min_value=0, max_value=45 * 365)
maturities = st.builds(Tenor, st.integers(min_value=1, max_value=360), st.just(TimeUnit.MONTHS))
notionals = st.floats(min_value=1e3, max_value=1e10)
directions = st.sampled_from(list(SwapDirection))


@given(days=days_ahead)
def test_discount_factors_are_positive_and_one_at_the_reference_date(days: int) -> None:
    curve = ENV.discount_curve
    assert curve.discount_factor(VALUATION_DATE) == 1.0
    assert 0.0 < curve.discount_factor(VALUATION_DATE + timedelta(days=days)) <= 1.0


@given(days=days_ahead, step=st.integers(min_value=1, max_value=3650))
def test_discount_factors_decrease_with_positive_forwards(days: int, step: int) -> None:
    curve = ENV.discount_curve
    start = VALUATION_DATE + timedelta(days=days)
    assert curve.discount_factor(start + timedelta(days=step)) < curve.discount_factor(start)


@given(days=days_ahead, step=st.integers(min_value=1, max_value=3650))
def test_forward_rate_is_consistent_with_discount_factors(days: int, step: int) -> None:
    curve = ENV.discount_curve
    start = VALUATION_DATE + timedelta(days=days)
    end = start + timedelta(days=step)
    forward = curve.forward_rate(start, end, Act360())
    tau = Act360().year_fraction(start, end)
    assert 1.0 + forward * tau == pytest.approx(
        curve.discount_factor(start) / curve.discount_factor(end), rel=1e-13
    )


@given(maturity=maturities, notional=notionals, direction=directions)
def test_swap_struck_at_par_has_zero_pv(
    maturity: Tenor, notional: float, direction: SwapDirection
) -> None:
    probe = build_spot_ois_swap(EUR, VALUATION_DATE, maturity, notional, 0.0, direction)
    par_rate = ENGINE.par_rate(probe, ENV)
    at_par = build_spot_ois_swap(EUR, VALUATION_DATE, maturity, notional, par_rate, direction)
    assert abs(ENGINE.price(at_par, ENV).present_value) <= PAR_SWAP_PV_PER_NOTIONAL * notional


@given(
    maturity=maturities,
    notional=notionals,
    fixed_rate=st.floats(min_value=-0.01, max_value=0.08),
)
def test_pay_and_receive_fixed_are_opposite(
    maturity: Tenor, notional: float, fixed_rate: float
) -> None:
    payer = build_spot_ois_swap(
        EUR, VALUATION_DATE, maturity, notional, fixed_rate, SwapDirection.PAY_FIXED
    )
    receiver = build_spot_ois_swap(
        EUR, VALUATION_DATE, maturity, notional, fixed_rate, SwapDirection.RECEIVE_FIXED
    )
    payer_result, receiver_result = ENGINE.price(payer, ENV), ENGINE.price(receiver, ENV)
    assert payer_result.present_value == pytest.approx(-receiver_result.present_value, abs=1e-9)
    assert payer_result.par_rate == receiver_result.par_rate
    assert payer_result.annuity == receiver_result.annuity


@settings(max_examples=20, deadline=None)
@given(
    shift_bp=st.floats(min_value=-300.0, max_value=300.0),
    weekday_offset=st.integers(min_value=0, max_value=60),
)
def test_bootstrap_reprices_shifted_markets(shift_bp: float, weekday_offset: int) -> None:
    valuation_date = date(2026, 8, 17) + timedelta(days=weekday_offset)
    snapshot = MarketSnapshot(
        name="shifted",
        currency="EUR",
        valuation_date=valuation_date,
        quotes=tuple(q.with_rate(q.rate + shift_bp * BASIS_POINT) for q in SNAPSHOT.quotes),
    )
    report = CurveBuilder().build(snapshot).calibration[0]
    assert report.max_abs_residual <= 1e-12
