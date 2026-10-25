"""Calendars, day counts and schedules: exact equality with QuantLib on generated grids."""

from __future__ import annotations

import itertools
from datetime import date, timedelta

import pytest
import QuantLib as ql

from rate_derivatives_pricer.core.conventions.business_day import BusinessDayConvention
from rate_derivatives_pricer.core.conventions.calendar import TargetCalendar
from rate_derivatives_pricer.core.conventions.day_count import (
    Act360,
    Act365Fixed,
    Thirty360,
    Thirty360European,
)
from rate_derivatives_pricer.core.conventions.tenor import Tenor
from rate_derivatives_pricer.core.schedule.generator import DateGenerationRule, ScheduleGenerator
from tests.validation.quantlib_oracle import BDC, CALENDAR, from_ql, to_ql
from tests.validation.tolerances import YEAR_FRACTION_ABS

pytestmark = pytest.mark.validation

TARGET = TargetCalendar()


def daily(start: date, end: date, step: int = 1) -> list[date]:
    return [start + timedelta(days=k) for k in range(0, (end - start).days + 1, step)]


def test_target_business_days_match_quantlib_over_60_years() -> None:
    mismatches = [
        d
        for d in daily(date(1995, 1, 1), date(2060, 12, 31))
        if TARGET.is_business_day(d) != CALENDAR.isBusinessDay(to_ql(d))
    ]
    assert mismatches == []


@pytest.mark.parametrize("convention", list(BusinessDayConvention))
def test_target_adjust_matches_quantlib(convention: BusinessDayConvention) -> None:
    for d in daily(date(2024, 1, 1), date(2032, 12, 31)):
        expected = from_ql(CALENDAR.adjust(to_ql(d), BDC[convention.value]))
        assert TARGET.adjust(d, convention) == expected, d


def test_target_end_of_month_matches_quantlib() -> None:
    for d in daily(date(2024, 1, 1), date(2032, 12, 31)):
        assert TARGET.is_end_of_month(d) == CALENDAR.isEndOfMonth(to_ql(d)), d
        assert TARGET.end_of_month(d) == from_ql(CALENDAR.endOfMonth(to_ql(d))), d


ADVANCE_TENORS = ["1D", "2D", "5D", "1W", "2W", "1M", "3M", "6M", "1Y", "18M", "5Y", "30Y"]


@pytest.mark.parametrize(
    ("label", "end_of_month"), list(itertools.product(ADVANCE_TENORS, [False, True]))
)
def test_target_advance_matches_quantlib(label: str, end_of_month: bool) -> None:
    tenor = Tenor.parse(label)
    for d in daily(date(2025, 1, 1), date(2029, 12, 31), step=2):
        for convention in (
            BusinessDayConvention.MODIFIED_FOLLOWING,
            BusinessDayConvention.FOLLOWING,
        ):
            expected = CALENDAR.advance(
                to_ql(d), ql.Period(label), BDC[convention.value], end_of_month
            )
            assert TARGET.advance(d, tenor, convention, end_of_month) == from_ql(expected), d


DAY_COUNT_PAIRS = [
    (Act360(), ql.Actual360()),
    (Act365Fixed(), ql.Actual365Fixed()),
    (Thirty360(), ql.Thirty360(ql.Thirty360.BondBasis)),
    (Thirty360European(), ql.Thirty360(ql.Thirty360.European)),
]


@pytest.mark.parametrize(("ours", "reference"), DAY_COUNT_PAIRS, ids=lambda x: str(x))
def test_day_counts_match_quantlib(ours: object, reference: ql.DayCounter) -> None:
    # Start dates around month ends (where 30/360 rules bite) and leap years.
    starts = [
        d for d in daily(date(2023, 1, 25), date(2025, 3, 5)) if d.day in {1, 15, 28, 29, 30, 31}
    ]
    offsets = [1, 2, 27, 28, 29, 30, 31, 59, 90, 181, 182, 365, 366, 730, 3653]
    for start in starts:
        for offset in offsets:
            end = start + timedelta(days=offset)
            assert ours.day_count(start, end) == reference.dayCount(to_ql(start), to_ql(end))  # type: ignore[attr-defined]
            assert ours.year_fraction(start, end) == pytest.approx(  # type: ignore[attr-defined]
                reference.yearFraction(to_ql(start), to_ql(end)), abs=YEAR_FRACTION_ABS
            )


SCHEDULE_TERMINATION_TENORS = ["1W", "1M", "11M", "1Y", "18M", "2Y", "5Y", "10Y"]
SCHEDULE_FREQUENCIES = ["1Y", "6M", "3M"]


def _effective_dates() -> list[date]:
    # Every 5th day plus every month end of two years: month ends exercise the EOM rule.
    regular = daily(date(2026, 1, 1), date(2027, 12, 31), step=5)
    month_ends = [
        d for d in daily(date(2026, 1, 1), date(2027, 12, 31)) if (d + timedelta(1)).day == 1
    ]
    business_month_ends = [TARGET.end_of_month(d) for d in month_ends]
    return sorted(set(regular + month_ends + business_month_ends))


@pytest.mark.parametrize(
    ("frequency", "rule", "end_of_month"),
    list(itertools.product(SCHEDULE_FREQUENCIES, list(DateGenerationRule), [False, True])),
)
def test_schedule_dates_match_quantlib(
    frequency: str, rule: DateGenerationRule, end_of_month: bool
) -> None:
    generator = ScheduleGenerator(
        tenor=Tenor.parse(frequency),
        calendar=TARGET,
        convention=BusinessDayConvention.MODIFIED_FOLLOWING,
        termination_convention=BusinessDayConvention.MODIFIED_FOLLOWING,
        rule=rule,
        end_of_month=end_of_month,
    )
    ql_rule = (
        ql.DateGeneration.Backward
        if rule is DateGenerationRule.BACKWARD
        else ql.DateGeneration.Forward
    )
    for effective in _effective_dates():
        for label in SCHEDULE_TERMINATION_TENORS:
            termination = Tenor.parse(label).add_to(effective)
            reference = ql.Schedule(
                to_ql(effective),
                to_ql(termination),
                ql.Period(frequency),
                CALENDAR,
                ql.ModifiedFollowing,
                ql.ModifiedFollowing,
                ql_rule,
                end_of_month,
            )
            expected = [from_ql(d) for d in reference]
            assert list(generator.generate(effective, termination).dates) == expected, (
                effective,
                label,
            )
