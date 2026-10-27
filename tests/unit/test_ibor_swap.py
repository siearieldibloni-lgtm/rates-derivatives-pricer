"""Ibor index, Ibor quotes, Euribor conventions, Ibor coupons and the vanilla IRS."""

from __future__ import annotations

import math
from datetime import date

import pytest

from rate_derivatives_pricer.core.conventions.currency import (
    CurrencyConventions,
    conventions_from_dict,
)
from rate_derivatives_pricer.core.conventions.day_count import Act360, Thirty360
from rate_derivatives_pricer.core.conventions.tenor import Tenor
from rate_derivatives_pricer.core.engines.base import engine_for
from rate_derivatives_pricer.core.errors import (
    ConventionError,
    MarketDataError,
    UnsupportedInstrumentError,
)
from rate_derivatives_pricer.core.instruments.cashflows import FixedCoupon, IborCoupon
from rate_derivatives_pricer.core.instruments.factories import (
    build_spot_vanilla_irs,
    build_vanilla_irs,
    ibor_swap_maturity_date,
    vanilla_irs_factory,
)
from rate_derivatives_pricer.core.instruments.legs import PayReceive
from rate_derivatives_pricer.core.instruments.swaps import SwapDirection, VanillaIrs
from rate_derivatives_pricer.core.market.environment import MarketEnvironment
from rate_derivatives_pricer.core.market.indices import IborIndex
from rate_derivatives_pricer.core.market.providers import snapshot_from_dict, snapshot_to_dict
from rate_derivatives_pricer.core.market.quotes import (
    DepositQuote,
    IborDepositQuote,
    IborSwapQuote,
    OisSwapQuote,
    QuoteUnit,
)
from rate_derivatives_pricer.core.market.snapshot import MarketSnapshot
from tests.unit.helpers import VALUATION_DATE, flat_curve

EURIBOR_6M_FIXING = 0.0205


def two_curve_environment(
    discount_rate: float,
    projection_rate: float,
    fixings: dict[date, float] | None = None,
    index: str = "EURIBOR6M",
) -> MarketEnvironment:
    """OIS discounting at ``discount_rate``, Euribor projection at ``projection_rate`` (flat)."""
    discount = flat_curve(discount_rate)
    return MarketEnvironment(
        valuation_date=VALUATION_DATE,
        currency="EUR",
        discount_curve=discount,
        projection_curves={"ESTR": discount, index: flat_curve(projection_rate)},
        fixings={index: fixings or {}},
    )


@pytest.fixture(scope="module")
def euribor_6m(eur: CurrencyConventions) -> IborIndex:
    return eur.ibor_index("EURIBOR6M")


# --------------------------------------------------------------------------- index and conventions
def test_euribor_conventions_are_loaded_from_configuration(eur: CurrencyConventions) -> None:
    assert [index.name for index in eur.ibor_indices] == ["EURIBOR6M", "EURIBOR3M"]
    assert eur.default_ibor_index == "EURIBOR6M"
    assert eur.ibor_index() is eur.ibor_index("euribor6m")
    index = eur.ibor_index("EURIBOR3M")
    assert (index.tenor, index.fixing_days, index.day_count) == (Tenor.parse("3M"), 2, Act360())
    assert index.end_of_month
    swap = eur.require_ibor_swap()
    assert swap.fixed_frequency.tenor == Tenor.parse("1Y")
    assert swap.fixed_day_count == Thirty360()
    assert swap.payment_lag_days == 0
    assert eur.require_ibor_deposit().settlement_days == 2


def test_unknown_index_and_currency_without_ibor_conventions(eur: CurrencyConventions) -> None:
    with pytest.raises(ConventionError) as error:
        eur.ibor_index("EURIBOR12M")
    assert error.value.details["available"] == ["EURIBOR6M", "EURIBOR3M"]
    overnight_only = conventions_from_dict(
        {
            "currency": "XXX",
            "calendar": "TARGET",
            "spot_lag_days": 2,
            "curve_day_count": "ACT/365F",
            "overnight_index": {"name": "ON", "calendar": "TARGET", "day_count": "ACT/360"},
            "deposit": {
                "settlement_days": 0,
                "business_day_convention": "MODIFIED_FOLLOWING",
                "end_of_month": False,
                "day_count": "ACT/360",
            },
            "ois": {
                "fixed_frequency": "ANNUAL",
                "fixed_day_count": "ACT/360",
                "floating_frequency": "ANNUAL",
                "floating_day_count": "ACT/360",
                "business_day_convention": "MODIFIED_FOLLOWING",
                "end_of_month": False,
                "rule": "BACKWARD",
                "payment_lag_days": 0,
            },
        }
    )
    assert overnight_only.ibor_indices == ()
    with pytest.raises(ConventionError):
        overnight_only.require_ibor_swap()
    with pytest.raises(ConventionError):
        overnight_only.require_ibor_deposit()
    with pytest.raises(ConventionError):
        overnight_only.ibor_index()


def test_fixing_date_is_two_target_days_before_the_period(euribor_6m: IborIndex) -> None:
    assert euribor_6m.fixing_date(date(2026, 10, 21)) == date(2026, 10, 19)
    assert euribor_6m.fixing_date(date(2026, 10, 19)) == date(2026, 10, 15)  # over a weekend
    # Good Friday and Easter Monday are TARGET holidays.
    assert euribor_6m.fixing_date(date(2026, 4, 8)) == date(2026, 4, 2)


# --------------------------------------------------------------------------- quotes
def test_ibor_quote_identity_and_validation() -> None:
    six = IborSwapQuote("5Y", 2.5, QuoteUnit.PERCENT, index_name="euribor6m")
    three = IborSwapQuote("5Y", 2.4, QuoteUnit.PERCENT, index_name="EURIBOR3M")
    assert six.index_name == "EURIBOR6M"
    assert six.key != three.key
    assert six.key != OisSwapQuote("5Y", 2.5).key
    assert (six.curve_label, OisSwapQuote("5Y", 2.5).curve_label) == ("EURIBOR6M", "OIS")
    bumped = six.with_rate(0.026)
    assert isinstance(bumped, IborSwapQuote)
    assert (bumped.index_name, bumped.unit) == ("EURIBOR6M", QuoteUnit.PERCENT)
    assert bumped.rate == pytest.approx(0.026, abs=1e-15)
    with pytest.raises(MarketDataError):
        IborDepositQuote("6M", 2.0, index_name="  ")


def test_snapshot_accepts_the_same_tenor_on_two_curves_but_not_twice_on_one() -> None:
    def snapshot(*quotes: object) -> MarketSnapshot:
        return MarketSnapshot("s", "EUR", VALUATION_DATE, tuple(quotes))  # type: ignore[arg-type]

    six = IborSwapQuote("5Y", 2.5, index_name="EURIBOR6M")
    three = IborSwapQuote("5Y", 2.4, index_name="EURIBOR3M")
    assert len(snapshot(OisSwapQuote("5Y", 2.2), six, three).quotes) == 3
    with pytest.raises(MarketDataError, match="duplicate quote"):
        snapshot(six, IborSwapQuote("5Y", 2.6, index_name="EURIBOR6M"))


def test_snapshot_round_trip_keeps_the_index() -> None:
    data = {
        "name": "mini",
        "currency": "EUR",
        "valuation_date": "2026-10-19",
        "quotes": [
            {"type": "deposit", "tenor": "ON", "value": 1.9, "unit": "percent"},
            {"type": "ibor_deposit", "index": "EURIBOR6M", "tenor": "6M", "value": 2.0},
            {"type": "ibor_swap", "index": "EURIBOR3M", "tenor": "2Y", "value": 2.1},
        ],
    }
    snapshot = snapshot_from_dict(data)
    assert isinstance(snapshot.quotes[1], IborDepositQuote)
    assert isinstance(snapshot.quotes[0], DepositQuote)
    dumped = snapshot_to_dict(snapshot)
    assert dumped["quotes"][1]["index"] == "EURIBOR6M"
    assert "index" not in dumped["quotes"][0]
    assert snapshot_from_dict(dumped).quotes == snapshot.quotes


def test_ibor_quote_without_an_index_is_rejected() -> None:
    data = {
        "currency": "EUR",
        "valuation_date": "2026-10-19",
        "quotes": [{"type": "ibor_swap", "tenor": "2Y", "value": 2.1}],
    }
    with pytest.raises(MarketDataError, match=r"quotes\[0\]\.index"):
        snapshot_from_dict(data)


# --------------------------------------------------------------------------- Ibor coupon
def _coupon(index: IborIndex, start: date, end: date) -> IborCoupon:
    return IborCoupon(
        notional=1e6,
        accrual_start=start,
        accrual_end=end,
        payment_date=end,
        day_count=index.day_count,
        index=index,
        fixing_date=index.fixing_date(start),
    )


def test_projected_coupon_is_the_forward_of_the_projection_curve(euribor_6m: IborIndex) -> None:
    env = two_curve_environment(0.02, 0.03)
    start, end = date(2027, 4, 21), date(2027, 10, 21)
    coupon = _coupon(euribor_6m, start, end)
    tau = Act360().year_fraction(start, end)
    curve = env.projection_curve("EURIBOR6M")
    forward = (curve.discount_factor(start) / curve.discount_factor(end) - 1.0) / tau
    assert coupon.rate(env) == pytest.approx(forward, abs=1e-15)
    assert coupon.amount(env) == pytest.approx(1e6 * forward * tau, abs=1e-9)
    # Flat 3% continuously compounded (Act/365F): the Act/360 simple forward is
    # (exp(0.03 * days / 365) - 1) / (days / 360), slightly above 3%.
    days = (end - start).days
    assert forward == pytest.approx(math.expm1(0.03 * days / 365) / (days / 360), rel=1e-12)


def test_realised_fixing_is_used_and_required_in_the_past(euribor_6m: IborIndex) -> None:
    start, end = date(2026, 8, 20), date(2027, 2, 22)
    coupon = _coupon(euribor_6m, start, end)
    fixed = two_curve_environment(0.02, 0.03, {coupon.fixing_date: EURIBOR_6M_FIXING})
    assert coupon.rate(fixed) == EURIBOR_6M_FIXING
    with pytest.raises(MarketDataError, match="missing EURIBOR6M fixing"):
        coupon.rate(two_curve_environment(0.02, 0.03))
    with pytest.raises(MarketDataError):
        IborCoupon.amounts([coupon], two_curve_environment(0.02, 0.03))


def test_fixing_of_the_valuation_date_is_used_if_known_and_projected_otherwise(
    euribor_6m: IborIndex,
) -> None:
    coupon = _coupon(euribor_6m, date(2026, 10, 21), date(2027, 4, 21))
    assert coupon.fixing_date == VALUATION_DATE
    known = two_curve_environment(0.02, 0.03, {VALUATION_DATE: EURIBOR_6M_FIXING})
    unknown = two_curve_environment(0.02, 0.03)
    assert coupon.rate(known) == EURIBOR_6M_FIXING
    assert coupon.rate(unknown) != EURIBOR_6M_FIXING
    assert coupon.rate(unknown) == pytest.approx(0.0302, abs=5e-4)


def test_batched_amounts_equal_the_coupon_by_coupon_amounts(euribor_6m: IborIndex) -> None:
    # One realised, one with today's fixing, then projected coupons.
    starts = [date(2026, 4, 21), date(2026, 10, 21), date(2027, 4, 21), date(2027, 10, 21)]
    coupons = [
        _coupon(euribor_6m, s, e)
        for s, e in zip(starts, [*starts[1:], date(2028, 4, 21)], strict=True)
    ]
    env = two_curve_environment(
        0.02, 0.03, {coupons[0].fixing_date: 0.019, coupons[1].fixing_date: 0.0205}
    )
    assert IborCoupon.amounts(coupons, env) == pytest.approx(
        [coupon.amount(env) for coupon in coupons], abs=1e-9
    )
    assert IborCoupon.amounts([], env) == []


# --------------------------------------------------------------------------- vanilla IRS
def test_spot_vanilla_irs_structure(eur: CurrencyConventions, euribor_6m: IborIndex) -> None:
    swap = build_spot_vanilla_irs(
        eur, euribor_6m, VALUATION_DATE, Tenor.parse("2Y"), 1e7, 0.02, SwapDirection.PAY_FIXED
    )
    assert swap.instrument_type == "vanilla_irs"
    assert swap.currency == "EUR"
    assert (swap.start_date, swap.end_date) == (date(2026, 10, 21), date(2028, 10, 23))
    assert swap.fixed_leg.direction is PayReceive.PAY
    assert swap.floating_leg.direction is PayReceive.RECEIVE
    assert swap.legs == (swap.fixed_leg, swap.floating_leg)
    # Annual fixed leg, 30/360; semi-annual Euribor 6M leg, Act/360; no payment lag.
    assert len(swap.fixed_coupons) == 2
    assert len(swap.ibor_coupons) == 4
    assert all(
        isinstance(c, FixedCoupon) and c.day_count == Thirty360() for c in swap.fixed_coupons
    )
    assert all(c.day_count == Act360() for c in swap.ibor_coupons)
    assert all(c.payment_date == c.accrual_end for c in swap.fixed_coupons + swap.ibor_coupons)
    assert swap.fixed_coupons[-1].accrual_end == swap.ibor_coupons[-1].accrual_end == swap.end_date
    assert next(c.fixing_date for c in swap.ibor_coupons) == date(2026, 10, 19)
    assert swap.last_cashflow_date == swap.end_date


def test_three_month_swap_pays_quarterly(eur: CurrencyConventions) -> None:
    swap = build_spot_vanilla_irs(
        eur,
        eur.ibor_index("EURIBOR3M"),
        VALUATION_DATE,
        Tenor.parse("1Y"),
        1e7,
        0.02,
        SwapDirection.RECEIVE_FIXED,
    )
    assert len(swap.ibor_coupons) == 4
    assert swap.floating_leg.direction is PayReceive.PAY
    assert swap.index.name == "EURIBOR3M"


def test_maturity_without_end_of_month_rule(eur: CurrencyConventions) -> None:
    # The rule is off for EUR vanilla swaps (ADR-027): a month-end start is not rolled to month end.
    assert ibor_swap_maturity_date(eur, date(2026, 4, 30), Tenor.parse("1Y")) == date(2027, 4, 30)


def test_factory_defaults_and_validation(eur: CurrencyConventions) -> None:
    params = {"notional": 1e6, "fixed_rate": 0.02, "direction": "pay_fixed", "maturity": "3Y"}
    swap = vanilla_irs_factory(params, VALUATION_DATE)
    assert swap.index.name == "EURIBOR6M"  # the currency's default index
    assert vanilla_irs_factory({**params, "index": "EURIBOR3M"}, VALUATION_DATE).index.name == (
        "EURIBOR3M"
    )
    forward = vanilla_irs_factory(
        {**params, "maturity": None, "start_date": "2027-10-21", "end_date": "2030-10-21"},
        VALUATION_DATE,
    )
    assert forward.start_date == date(2027, 10, 21)
    with pytest.raises(ConventionError):
        vanilla_irs_factory({**params, "index": "EURIBOR12M"}, VALUATION_DATE)
    with pytest.raises(ConventionError):
        vanilla_irs_factory({**params, "end_date": "2030-10-21"}, VALUATION_DATE)
    with pytest.raises(ConventionError, match="missing trade field"):
        vanilla_irs_factory({"notional": 1e6}, VALUATION_DATE)
    with pytest.raises(ConventionError, match="notional"):
        build_vanilla_irs(
            eur,
            eur.ibor_index(),
            0.0,
            0.02,
            SwapDirection.PAY_FIXED,
            VALUATION_DATE,
            date(2028, 1, 1),
        )


# --------------------------------------------------------------------------- engine
def _irs(eur: CurrencyConventions, fixed_rate: float, direction: SwapDirection) -> VanillaIrs:
    return build_spot_vanilla_irs(
        eur, eur.ibor_index(), VALUATION_DATE, Tenor.parse("5Y"), 1e7, fixed_rate, direction
    )


def test_engine_prices_both_swap_types_and_rejects_others(eur: CurrencyConventions) -> None:
    assert engine_for("vanilla_irs").supports(_irs(eur, 0.02, SwapDirection.PAY_FIXED))
    with pytest.raises(UnsupportedInstrumentError):
        engine_for("swaption")


def test_swap_at_its_par_rate_has_a_zero_pv(eur: CurrencyConventions) -> None:
    env = two_curve_environment(0.02, 0.025)
    engine = engine_for("vanilla_irs")
    par = engine.price(_irs(eur, 0.02, SwapDirection.PAY_FIXED), env).par_rate
    for direction in SwapDirection:
        result = engine.price(_irs(eur, par, direction), env)
        assert result.present_value == pytest.approx(0.0, abs=1e-9 * 1e7)
    receiver = engine.price(_irs(eur, 0.03, SwapDirection.RECEIVE_FIXED), env)
    payer = engine.price(_irs(eur, 0.03, SwapDirection.PAY_FIXED), env)
    assert receiver.present_value == pytest.approx(-payer.present_value, rel=1e-12)


def test_irs_pv_matches_its_closed_form_on_flat_curves(eur: CurrencyConventions) -> None:
    env = two_curve_environment(0.02, 0.025)
    swap = _irs(eur, 0.03, SwapDirection.PAY_FIXED)
    result = engine_for("vanilla_irs").price(swap, env)
    discount = env.discount_curve
    # PV payer = floating leg - fixed leg, each coupon discounted on the OIS curve.
    fixed_leg = sum(
        c.amount(env) * discount.discount_factor(c.payment_date) for c in swap.fixed_coupons
    )
    floating_leg = sum(
        c.amount(env) * discount.discount_factor(c.payment_date) for c in swap.ibor_coupons
    )
    assert result.present_value == pytest.approx(floating_leg - fixed_leg, rel=1e-12)
    assert [leg.name for leg in result.legs] == ["fixed", "floating"]
    assert result.annuity == pytest.approx(
        sum(
            c.notional * c.year_fraction * discount.discount_factor(c.payment_date)
            for c in swap.fixed_coupons
        ),
        rel=1e-12,
    )


def test_projection_curve_matters_for_a_multi_curve_irs(eur: CurrencyConventions) -> None:
    swap = _irs(eur, 0.025, SwapDirection.PAY_FIXED)
    engine = engine_for("vanilla_irs")
    single = engine.price(swap, two_curve_environment(0.025, 0.025)).present_value
    multi = engine.price(swap, two_curve_environment(0.025, 0.030)).present_value
    # Higher projection rates raise the floating leg a payer receives.
    assert multi > single


def test_present_value_shortcut_agrees_with_price(eur: CurrencyConventions) -> None:
    env = two_curve_environment(0.02, 0.025, {VALUATION_DATE: EURIBOR_6M_FIXING})
    engine = engine_for("vanilla_irs")
    for direction in SwapDirection:
        swap = _irs(eur, 0.027, direction)
        assert engine.present_value(swap, env) == pytest.approx(
            engine.price(swap, env).present_value, rel=1e-13
        )


def test_seasoned_irs_needs_its_past_fixings(eur: CurrencyConventions) -> None:
    swap = vanilla_irs_factory(
        {
            "notional": 1e7,
            "fixed_rate": 0.02,
            "direction": "pay_fixed",
            "start_date": "2026-04-21",
            "maturity": "2Y",
        },
        VALUATION_DATE,
    )
    engine = engine_for("vanilla_irs")
    with pytest.raises(MarketDataError, match="missing EURIBOR6M fixing"):
        engine.price(swap, two_curve_environment(0.02, 0.025))
    first_fixing = swap.ibor_coupons[0].fixing_date
    env = two_curve_environment(0.02, 0.025, {first_fixing: 0.021})
    result = engine.price(swap, env)
    # The first coupon is paid on 2026-10-21, after the valuation date: still alive, at its fixing.
    assert any(row.rate == 0.021 for row in result.cashflows)
