from __future__ import annotations

from datetime import date

import pytest

from rate_derivatives_pricer.core.conventions.business_day import BusinessDayConvention as Bdc
from rate_derivatives_pricer.core.conventions.calendar import (
    JointCalendar,
    NullCalendar,
    TargetCalendar,
    WeekendsOnlyCalendar,
    calendar_from_name,
    easter_sunday,
)
from rate_derivatives_pricer.core.conventions.currency import (
    CurrencyConventions,
    conventions_from_dict,
    get_conventions,
)
from rate_derivatives_pricer.core.conventions.day_count import (
    Act360,
    Act365Fixed,
    Thirty360,
    Thirty360European,
    day_count_from_name,
)
from rate_derivatives_pricer.core.conventions.tenor import (
    Frequency,
    Tenor,
    TimeUnit,
    add_months,
    is_end_of_month,
)
from rate_derivatives_pricer.core.errors import ConventionError

TARGET = TargetCalendar()


# --------------------------------------------------------------------------- tenor
@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("1W", Tenor(1, TimeUnit.WEEKS)),
        ("18m", Tenor(18, TimeUnit.MONTHS)),
        (" 30Y ", Tenor(30, TimeUnit.YEARS)),
        ("ON", Tenor(1, TimeUnit.DAYS)),
        ("2D", Tenor(2, TimeUnit.DAYS)),
    ],
)
def test_tenor_parse(label: str, expected: Tenor) -> None:
    assert Tenor.parse(label) == expected


@pytest.mark.parametrize("label", ["", "Y", "1X", "-1Y", "1.5Y"])
def test_tenor_parse_rejects_invalid_labels(label: str) -> None:
    with pytest.raises(ConventionError):
        Tenor.parse(label)


def test_tenor_arithmetic_and_str() -> None:
    assert str(Tenor.parse("6M")) == "6M"
    assert -Tenor.parse("1Y") == Tenor(-1, TimeUnit.YEARS)
    assert 3 * Tenor.parse("1M") == Tenor(3, TimeUnit.MONTHS) == Tenor.parse("1M") * 3


@pytest.mark.parametrize(
    ("start", "label", "expected"),
    [
        (date(2026, 1, 31), "1M", date(2026, 2, 28)),  # day clamped to month length
        (date(2028, 1, 31), "1M", date(2028, 2, 29)),  # leap year
        (date(2028, 2, 29), "1Y", date(2029, 2, 28)),
        (date(2026, 3, 31), "-1M", date(2026, 2, 28)),
        (date(2026, 10, 19), "1W", date(2026, 10, 26)),
        (date(2026, 10, 19), "3D", date(2026, 10, 22)),
        (date(2026, 12, 15), "1M", date(2027, 1, 15)),
    ],
)
def test_tenor_add_to(start: date, label: str, expected: date) -> None:
    tenor = Tenor(-1, TimeUnit.MONTHS) if label == "-1M" else Tenor.parse(label)
    assert tenor.add_to(start) == expected


def test_add_months_and_end_of_month_helpers() -> None:
    assert add_months(date(2026, 11, 30), 3) == date(2027, 2, 28)
    assert is_end_of_month(date(2026, 2, 28))
    assert not is_end_of_month(date(2028, 2, 28))


def test_frequency() -> None:
    assert Frequency.parse("annual").tenor == Tenor.parse("1Y")
    assert Frequency.QUARTERLY.tenor == Tenor.parse("3M")
    assert Frequency.SEMIANNUAL.tenor == Tenor.parse("6M")
    with pytest.raises(ConventionError):
        Frequency.parse("fortnightly")


# --------------------------------------------------------------------------- calendars
@pytest.mark.parametrize(
    ("year", "expected"),
    [(2024, date(2024, 3, 31)), (2025, date(2025, 4, 20)), (2026, date(2026, 4, 5))],
)
def test_easter_sunday(year: int, expected: date) -> None:
    assert easter_sunday(year) == expected


@pytest.mark.parametrize(
    "holiday",
    [
        date(2026, 1, 1),
        date(2026, 4, 3),  # Good Friday
        date(2026, 4, 6),  # Easter Monday
        date(2026, 5, 1),
        date(2026, 12, 25),
        date(2026, 12, 26),
        date(2001, 12, 31),  # historical closing day
    ],
)
def test_target_holidays(holiday: date) -> None:
    assert TARGET.is_holiday(holiday)


def test_target_pre_2000_rules() -> None:
    assert TARGET.is_business_day(date(1999, 5, 3))
    assert TARGET.is_business_day(date(1998, 4, 10))  # Good Friday 1998 was open
    assert TARGET.is_holiday(date(1999, 12, 31))


def test_target_weekends() -> None:
    assert TARGET.is_holiday(date(2026, 10, 24))  # Saturday
    assert TARGET.is_business_day(date(2026, 10, 19))


@pytest.mark.parametrize(
    ("convention", "expected"),
    [
        (Bdc.FOLLOWING, date(2026, 6, 1)),
        (Bdc.MODIFIED_FOLLOWING, date(2026, 5, 29)),
        (Bdc.PRECEDING, date(2026, 5, 29)),
        (Bdc.UNADJUSTED, date(2026, 5, 30)),
    ],
)
def test_adjust(convention: Bdc, expected: date) -> None:
    assert TARGET.adjust(date(2026, 5, 30), convention) == expected  # Saturday


def test_advance_business_days_and_tenors() -> None:
    # Thursday before Easter: +2 business days skips Good Friday and Easter Monday.
    assert TARGET.advance_business_days(date(2026, 4, 2), 2) == date(2026, 4, 8)
    assert TARGET.advance_business_days(date(2026, 4, 8), -2) == date(2026, 4, 2)
    assert TARGET.advance_business_days(date(2026, 10, 24), 0) == date(2026, 10, 26)
    assert TARGET.advance(date(2026, 10, 21), Tenor.parse("1M")) == date(2026, 11, 23)
    # End-of-month rule: last business day of April -> last business day of target months.
    assert TARGET.advance(date(2026, 4, 30), Tenor.parse("1M"), Bdc.FOLLOWING, True) == date(
        2026, 5, 29
    )
    assert TARGET.advance(date(2026, 4, 30), Tenor.parse("1W"), Bdc.FOLLOWING, True) == date(
        2026, 5, 7
    )


def test_end_of_month_and_business_day_counts() -> None:
    assert TARGET.is_end_of_month(date(2026, 5, 29))
    assert not TARGET.is_end_of_month(date(2026, 5, 28))
    assert TARGET.end_of_month(date(2026, 5, 3)) == date(2026, 5, 29)
    assert TARGET.business_days_between(date(2026, 10, 19), date(2026, 10, 26)) == 5
    assert TARGET.business_days(date(2026, 10, 23), date(2026, 10, 26)) == [
        date(2026, 10, 23),
        date(2026, 10, 26),
    ]
    with pytest.raises(ConventionError):
        TARGET.business_days_between(date(2026, 10, 26), date(2026, 10, 19))


def test_other_calendars() -> None:
    assert NullCalendar().is_business_day(date(2026, 10, 24))
    assert WeekendsOnlyCalendar().is_holiday(date(2026, 10, 25))
    assert WeekendsOnlyCalendar().is_business_day(date(2026, 12, 25))
    joint = calendar_from_name("TARGET+WEEKENDS_ONLY")
    assert isinstance(joint, JointCalendar)
    assert joint.label == "TARGET+WEEKENDS_ONLY"
    assert joint.is_holiday(date(2026, 12, 25))
    assert calendar_from_name("target") == TARGET
    with pytest.raises(ConventionError):
        JointCalendar(())
    with pytest.raises(ConventionError):
        calendar_from_name("MARS")


# --------------------------------------------------------------------------- day counts
def test_day_counts() -> None:
    start, end = date(2026, 1, 31), date(2026, 3, 31)
    assert Act360().year_fraction(start, end) == 59 / 360
    assert Act365Fixed().year_fraction(start, end) == 59 / 365
    # 30/360 Bond Basis: D1 = 31 -> 30, then D2 = 31 -> 30 because D1 is 30.
    assert Thirty360().day_count(start, end) == 60
    assert Thirty360().day_count(date(2026, 1, 15), date(2026, 3, 31)) == 76
    assert Thirty360European().day_count(date(2026, 1, 15), date(2026, 3, 31)) == 75
    assert Thirty360European().year_fraction(start, end) == 60 / 360
    assert Thirty360().year_fraction(end, start) == -60 / 360
    assert day_count_from_name("act/360") == Act360()
    with pytest.raises(ConventionError):
        day_count_from_name("ACT/ACT")


# --------------------------------------------------------------------------- currency
def test_eur_conventions_are_loaded_from_configuration(eur: CurrencyConventions) -> None:
    assert eur.currency == "EUR"
    assert eur.calendar == TARGET
    assert eur.spot_lag_days == 2
    assert eur.overnight_index.name == "ESTR"
    assert eur.overnight_index.day_count == Act360()
    assert eur.ois.payment_lag_days == 1
    assert eur.ois.fixed_frequency is Frequency.ANNUAL
    assert eur.ois.business_day_convention is Bdc.MODIFIED_FOLLOWING
    assert eur.deposit.settlement_days == 0
    assert get_conventions(" eur ") is eur


def test_unknown_currency() -> None:
    with pytest.raises(ConventionError) as error:
        get_conventions("XXX")
    assert "EUR" in error.value.details["available"]


def test_conventions_from_dict_reports_missing_and_invalid_fields() -> None:
    with pytest.raises(ConventionError, match=r"root\.currency"):
        conventions_from_dict({})
    config = {
        "currency": "TST",
        "calendar": "TARGET",
        "spot_lag_days": 2,
        "curve_day_count": "ACT/365F",
        "overnight_index": {"name": "TST", "calendar": "TARGET", "day_count": "ACT/360"},
        "deposit": {
            "settlement_days": 0,
            "business_day_convention": "SIDEWAYS",
            "end_of_month": False,
            "day_count": "ACT/360",
        },
    }
    with pytest.raises(ConventionError, match=r"deposit\.business_day_convention"):
        conventions_from_dict(config)
