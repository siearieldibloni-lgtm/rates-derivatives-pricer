from __future__ import annotations

from datetime import date

import pytest

from rate_derivatives_pricer.core.conventions.business_day import BusinessDayConvention as Bdc
from rate_derivatives_pricer.core.conventions.calendar import TargetCalendar
from rate_derivatives_pricer.core.conventions.tenor import Tenor
from rate_derivatives_pricer.core.errors import ScheduleError
from rate_derivatives_pricer.core.schedule.generator import DateGenerationRule, ScheduleGenerator

TARGET = TargetCalendar()


def annual(**kwargs: object) -> ScheduleGenerator:
    return ScheduleGenerator(tenor=Tenor.parse("1Y"), calendar=TARGET, **kwargs)  # type: ignore[arg-type]


def test_backward_schedule_puts_the_stub_first() -> None:
    schedule = annual().generate(date(2026, 10, 21), date(2029, 4, 21))
    assert schedule.dates == (
        date(2026, 10, 21),
        date(2027, 4, 21),
        date(2028, 4, 21),
        date(2029, 4, 23),  # 21 April 2029 is a Saturday: modified following
    )
    assert [p.is_regular for p in schedule.periods] == [False, True, True]
    assert len(schedule) == 3
    assert schedule.start_date == date(2026, 10, 21)
    assert schedule.end_date == date(2029, 4, 23)


def test_forward_schedule_puts_the_stub_last() -> None:
    schedule = annual(rule=DateGenerationRule.FORWARD).generate(
        date(2026, 10, 21), date(2029, 4, 21)
    )
    assert schedule.dates[:3] == (date(2026, 10, 21), date(2027, 10, 21), date(2028, 10, 23))
    assert [p.is_regular for p in schedule.periods] == [True, True, False]


def test_short_schedule_has_a_single_period() -> None:
    schedule = annual().generate(date(2026, 10, 21), date(2026, 10, 28))
    assert len(schedule) == 1
    assert not schedule.periods[0].is_regular


def test_payment_lag_and_payment_convention() -> None:
    lagged = annual(payment_lag_days=1).generate(date(2026, 10, 21), date(2027, 10, 21))
    assert lagged.periods[0].payment_date == date(2027, 10, 22)
    # Accrual ends on Thursday 24 Dec 2026; the lag skips Christmas and Boxing Day.
    christmas = annual(payment_lag_days=1).generate(date(2025, 12, 24), date(2026, 12, 24))
    assert christmas.periods[0].payment_date == date(2026, 12, 28)
    unlagged = annual(payment_convention=Bdc.FOLLOWING).generate(
        date(2026, 10, 21), date(2027, 10, 21)
    )
    assert unlagged.periods[0].payment_date == unlagged.periods[0].accrual_end


def test_end_of_month_rule_rolls_on_month_ends() -> None:
    quarterly = ScheduleGenerator(tenor=Tenor.parse("3M"), calendar=TARGET, end_of_month=True)
    schedule = quarterly.generate(date(2026, 4, 30), date(2027, 4, 30))
    assert schedule.dates == (
        date(2026, 4, 30),
        date(2026, 7, 31),
        date(2026, 10, 30),  # 31 October 2026 is a Saturday: last business day
        date(2027, 1, 29),
        date(2027, 4, 30),
    )


def test_end_of_month_rule_with_unadjusted_dates() -> None:
    generator = ScheduleGenerator(
        tenor=Tenor.parse("3M"),
        calendar=TARGET,
        convention=Bdc.UNADJUSTED,
        termination_convention=Bdc.UNADJUSTED,
        end_of_month=True,
    )
    schedule = generator.generate(date(2026, 4, 30), date(2027, 4, 30))
    assert schedule.dates[2] == date(2026, 10, 31)  # calendar month end, not adjusted


def test_forward_end_of_month_rule() -> None:
    generator = ScheduleGenerator(
        tenor=Tenor.parse("3M"),
        calendar=TARGET,
        rule=DateGenerationRule.FORWARD,
        end_of_month=True,
    )
    schedule = generator.generate(date(2026, 4, 30), date(2027, 3, 15))
    assert schedule.dates == (
        date(2026, 4, 30),
        date(2026, 7, 31),
        date(2026, 10, 30),
        date(2027, 1, 29),
        date(2027, 3, 15),
    )


def test_invalid_schedules() -> None:
    with pytest.raises(ScheduleError):
        annual().generate(date(2026, 10, 21), date(2026, 10, 21))
    with pytest.raises(ScheduleError):
        ScheduleGenerator(tenor=Tenor(0, Tenor.parse("1Y").unit), calendar=TARGET).generate(
            date(2026, 10, 21), date(2027, 10, 21)
        )
    # Saturday to Sunday collapses to a single adjusted date.
    with pytest.raises(ScheduleError):
        annual().generate(date(2026, 10, 24), date(2026, 10, 25))
