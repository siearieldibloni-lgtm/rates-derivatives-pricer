from __future__ import annotations

import logging
import math
from datetime import date

import pytest

from rate_derivatives_pricer.core.conventions.currency import CurrencyConventions
from rate_derivatives_pricer.core.conventions.day_count import Act360
from rate_derivatives_pricer.core.conventions.tenor import Tenor
from rate_derivatives_pricer.core.engines.base import engine_for
from rate_derivatives_pricer.core.engines.discounting import DiscountingSwapEngine
from rate_derivatives_pricer.core.errors import (
    ConventionError,
    MarketDataError,
    PricingError,
    ScheduleError,
    UnsupportedInstrumentError,
)
from rate_derivatives_pricer.core.instruments.cashflows import (
    FixedCoupon,
    OvernightCompoundedCoupon,
)
from rate_derivatives_pricer.core.instruments.factories import (
    build_ois_swap,
    build_spot_ois_swap,
    ois_maturity_date,
    ois_swap_factory,
    spot_date,
)
from rate_derivatives_pricer.core.instruments.legs import PayReceive
from rate_derivatives_pricer.core.instruments.swaps import Instrument, SwapDirection
from rate_derivatives_pricer.core.market.environment import MarketEnvironment
from tests.unit.helpers import VALUATION_DATE, flat_environment

ESTR_FIXING = 0.0192


def test_spot_date_and_maturity(eur: CurrencyConventions) -> None:
    assert spot_date(eur, VALUATION_DATE) == date(2026, 10, 21)
    # Thursday before Easter: Good Friday and Easter Monday are skipped.
    assert spot_date(eur, date(2026, 4, 2)) == date(2026, 4, 8)
    # Start on the last business day of the month: end-of-month rule.
    assert ois_maturity_date(eur, date(2026, 4, 30), Tenor.parse("1M")) == date(2026, 5, 29)
    assert ois_maturity_date(eur, date(2026, 10, 21), Tenor.parse("1M")) == date(2026, 11, 21)


def test_swap_structure(eur: CurrencyConventions) -> None:
    swap = build_spot_ois_swap(
        eur, VALUATION_DATE, Tenor.parse("18M"), 1e7, 0.02, SwapDirection.PAY_FIXED
    )
    assert swap.instrument_type == "ois_swap"
    assert swap.currency == "EUR"
    assert (swap.start_date, swap.end_date) == (date(2026, 10, 21), date(2028, 4, 21))
    assert swap.fixed_leg.direction is PayReceive.PAY
    assert swap.overnight_leg.direction is PayReceive.RECEIVE
    # Annual backward schedule: a 6M front stub, then a full year.
    fixed = swap.fixed_coupons
    assert [(c.accrual_start, c.accrual_end, c.payment_date) for c in fixed] == [
        (date(2026, 10, 21), date(2027, 4, 21), date(2027, 4, 22)),
        (date(2027, 4, 21), date(2028, 4, 21), date(2028, 4, 24)),
    ]
    assert len(swap.overnight_coupons) == 2
    assert swap.legs == (swap.fixed_leg, swap.overnight_leg)


def test_factory_from_parameters() -> None:
    params = {"notional": 5e6, "fixed_rate": 0.021, "direction": "RECEIVE_FIXED", "maturity": "2Y"}
    swap = ois_swap_factory(params, VALUATION_DATE)
    assert swap.direction is SwapDirection.RECEIVE_FIXED
    assert swap.end_date == date(2028, 10, 23)  # 21 Oct 2028 is a Saturday
    explicit = ois_swap_factory(
        {**params, "maturity": None, "start_date": "2026-11-02", "end_date": date(2027, 11, 2)},
        VALUATION_DATE,
    )
    assert (explicit.start_date, explicit.end_date) == (date(2026, 11, 2), date(2027, 11, 2))


@pytest.mark.parametrize(
    ("params", "error"),
    [
        ({"fixed_rate": 0.02, "direction": "pay_fixed", "maturity": "1Y"}, "notional"),
        ({"notional": 1e6, "fixed_rate": 0.02, "direction": "long", "maturity": "1Y"}, "invalid"),
        ({"notional": 1e6, "fixed_rate": 0.02, "direction": "pay_fixed"}, "exactly one"),
        (
            {
                "notional": 1e6,
                "fixed_rate": 0.02,
                "direction": "pay_fixed",
                "maturity": "1Y",
                "end_date": "2027-10-21",
            },
            "exactly one",
        ),
        (
            {"notional": 1e6, "fixed_rate": 0.02, "direction": "pay_fixed", "end_date": "21/10/27"},
            "invalid date",
        ),
        (
            {"notional": -1.0, "fixed_rate": 0.02, "direction": "pay_fixed", "maturity": "1Y"},
            "positive",
        ),
        (
            {
                "notional": 1e6,
                "fixed_rate": 0.02,
                "direction": "pay_fixed",
                "maturity": "1Y",
                "currency": "XXX",
            },
            "currency",
        ),
    ],
)
def test_factory_rejects_invalid_trades(params: dict[str, object], error: str) -> None:
    with pytest.raises(ConventionError, match=error):
        ois_swap_factory(params, VALUATION_DATE)


def test_factory_rejects_inverted_dates() -> None:
    params = {
        "notional": 1e6,
        "fixed_rate": 0.02,
        "direction": "pay_fixed",
        "start_date": "2027-01-04",
        "end_date": "2026-12-01",
    }
    with pytest.raises(ScheduleError):
        ois_swap_factory(params, VALUATION_DATE)


# --------------------------------------------------------------------------- cashflows
def _coupon(start: date, end: date, pay: date) -> OvernightCompoundedCoupon:
    from rate_derivatives_pricer.core.conventions.currency import get_conventions

    return OvernightCompoundedCoupon(
        notional=1e6,
        accrual_start=start,
        accrual_end=end,
        payment_date=pay,
        day_count=Act360(),
        index=get_conventions("EUR").overnight_index,
    )


def test_fixed_coupon_amount() -> None:
    coupon = FixedCoupon(
        1e6, date(2026, 10, 21), date(2027, 10, 21), date(2027, 10, 22), Act360(), 0.02
    )
    env = flat_environment(0.02)
    assert coupon.year_fraction == 365 / 360
    assert coupon.rate(env) == 0.02
    assert coupon.amount(env) == pytest.approx(1e6 * 0.02 * 365 / 360, abs=1e-9)


def test_future_overnight_coupon_telescopes() -> None:
    env = flat_environment(0.02)
    coupon = _coupon(date(2026, 10, 21), date(2027, 10, 21), date(2027, 10, 22))
    # Flat continuous 2% curve: compound factor exp(0.02 * days / 365).
    expected = math.expm1(0.02 * 365 / 365)
    assert coupon.amount(env) == pytest.approx(1e6 * expected, rel=1e-12)
    assert coupon.rate(env) == pytest.approx(expected / (365 / 360), rel=1e-12)


def test_seasoned_overnight_coupon_compounds_fixings() -> None:
    # Coupon from Monday 12 Oct to Monday 26 Oct 2026, valued on Monday 19 Oct.
    fixing_dates = [date(2026, 10, d) for d in (12, 13, 14, 15, 16)]
    fixings = dict.fromkeys(fixing_dates, ESTR_FIXING)
    env = flat_environment(0.02, fixings)
    coupon = _coupon(date(2026, 10, 12), date(2026, 10, 26), date(2026, 10, 27))
    realised = (1 + ESTR_FIXING / 360) ** 4 * (1 + ESTR_FIXING * 3 / 360)  # Friday covers 3 days
    projected = math.exp(0.02 * 7 / 365)  # 19 -> 26 Oct, telescoped on the curve
    assert coupon.compound_factor(env) == pytest.approx(realised * projected, rel=1e-14)

    # Today's fixing, when already known, replaces the projection of the first day.
    with_today = flat_environment(0.02, {**fixings, VALUATION_DATE: 0.03})
    expected_today = realised * (1 + 0.03 / 360) * math.exp(0.02 * 6 / 365)
    assert coupon.compound_factor(with_today) == pytest.approx(expected_today, rel=1e-14)


def test_missing_past_fixing_is_an_error() -> None:
    env = flat_environment(0.02, {date(2026, 10, 12): ESTR_FIXING})
    coupon = _coupon(date(2026, 10, 12), date(2026, 10, 26), date(2026, 10, 27))
    with pytest.raises(MarketDataError) as error:
        coupon.amount(env)
    assert error.value.details == {"index": "ESTR", "date": "2026-10-13"}


def test_coupon_fully_in_the_past() -> None:
    fixings = {d: ESTR_FIXING for d in (date(2026, 10, 12), date(2026, 10, 13))}
    coupon = _coupon(date(2026, 10, 12), date(2026, 10, 14), date(2026, 10, 15))
    assert coupon.compound_factor(flat_environment(0.02, fixings)) == pytest.approx(
        (1 + ESTR_FIXING / 360) ** 2, rel=1e-15
    )


# --------------------------------------------------------------------------- engine
def test_engine_lookup() -> None:
    assert isinstance(engine_for("ois_swap"), DiscountingSwapEngine)
    with pytest.raises(UnsupportedInstrumentError):
        engine_for("ois_swap", "monte_carlo")
    with pytest.raises(UnsupportedInstrumentError):
        engine_for("cap")


class _Unknown(Instrument):
    instrument_type = "unknown"

    @property
    def currency(self) -> str:
        return "EUR"


def test_engine_rejects_unsupported_instruments(env: MarketEnvironment) -> None:
    engine = DiscountingSwapEngine()
    assert not engine.supports(_Unknown())
    with pytest.raises(UnsupportedInstrumentError):
        engine.price(_Unknown(), env)


def test_pricing_result(
    eur: CurrencyConventions, env: MarketEnvironment, caplog: pytest.LogCaptureFixture
) -> None:
    swap = build_spot_ois_swap(
        eur, VALUATION_DATE, Tenor.parse("5Y"), 1e7, 0.025, SwapDirection.PAY_FIXED
    )
    with caplog.at_level(logging.INFO, logger="rate_derivatives_pricer"):
        result = DiscountingSwapEngine().price(swap, env)
    assert result.instrument_type == "ois_swap"
    assert result.currency == "EUR"
    assert result.valuation_date == VALUATION_DATE
    assert result.par_rate == pytest.approx(0.02254, abs=1e-13)  # calibration quote
    fixed, overnight = result.legs
    assert (fixed.name, fixed.direction, overnight.direction) == ("fixed", "pay", "receive")
    assert result.present_value == pytest.approx(fixed.present_value + overnight.present_value)
    # Paying 2.50% vs a 2.254% par rate: PV = -(K - par) * annuity.
    assert result.present_value == pytest.approx(
        -(0.025 - result.par_rate) * result.annuity, rel=1e-12
    )
    assert result.pvbp == pytest.approx(result.annuity * 1e-4)
    assert len(result.cashflows) == 10
    row = result.cashflows[0]
    assert row.leg == "fixed"
    assert row.amount < 0
    assert row.present_value == pytest.approx(row.amount * row.discount_factor)
    assert row.rate == pytest.approx(0.025)
    assert result.diagnostics.engine == "DiscountingSwapEngine"
    assert result.diagnostics.warnings == ()
    assert any(r.getMessage() == "pricing_completed" for r in caplog.records)


def test_matured_swap_has_no_cashflows(eur: CurrencyConventions, env: MarketEnvironment) -> None:
    swap = build_ois_swap(
        eur, 1e6, 0.02, SwapDirection.PAY_FIXED, date(2025, 1, 15), date(2026, 1, 15)
    )
    result = DiscountingSwapEngine().price(swap, env)
    assert result.present_value == 0.0
    assert result.cashflows == ()
    assert math.isnan(result.par_rate)
    assert result.diagnostics.warnings
    with pytest.raises(PricingError):
        DiscountingSwapEngine().par_rate(swap, env)
