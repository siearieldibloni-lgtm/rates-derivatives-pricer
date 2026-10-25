"""Tenors ("3M", "1Y") and coupon frequencies, with calendar-free date arithmetic."""

from __future__ import annotations

import calendar as _stdlib_calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum, StrEnum

from rate_derivatives_pricer.core.errors import ConventionError

DAYS_PER_WEEK = 7
MONTHS_PER_YEAR = 12

# Market labels that are not of the "<n><unit>" form. "ON" (overnight) spans one business day.
_TENOR_ALIASES = {"ON": "1D"}
_TENOR_PATTERN = re.compile(r"^\s*(\d+)\s*([DWMY])\s*$", re.IGNORECASE)


class TimeUnit(StrEnum):
    """Unit of a :class:`Tenor`."""

    DAYS = "D"
    WEEKS = "W"
    MONTHS = "M"
    YEARS = "Y"


def last_day_of_month(year: int, month: int) -> int:
    """Number of days in ``month`` of ``year`` (handles leap years)."""
    return _stdlib_calendar.monthrange(year, month)[1]


def end_of_month(d: date) -> date:
    """Last calendar day of the month of ``d``."""
    return d.replace(day=last_day_of_month(d.year, d.month))


def is_end_of_month(d: date) -> bool:
    """Whether ``d`` is the last calendar day of its month."""
    return d.day == last_day_of_month(d.year, d.month)


def add_months(d: date, months: int) -> date:
    """Add calendar months, clamping the day to the target month length (31 Jan + 1M = 28/29 Feb).

    Args:
        d: Start date.
        months: Number of months, may be negative.
    """
    month_index = d.year * MONTHS_PER_YEAR + (d.month - 1) + months
    year, month_zero_based = divmod(month_index, MONTHS_PER_YEAR)
    month = month_zero_based + 1
    return date(year, month, min(d.day, last_day_of_month(year, month)))


@dataclass(frozen=True, slots=True, order=True)
class Tenor:
    """A length of time such as ``3M`` or ``10Y`` (value object).

    Attributes:
        length: Number of units; may be negative for backward arithmetic.
        unit: Time unit. ``DAYS`` means calendar days for :meth:`add_to` and business days for
            ``Calendar.advance``, as in the market (and in QuantLib).
    """

    length: int
    unit: TimeUnit

    @classmethod
    def parse(cls, label: str) -> Tenor:
        """Parse ``"1W"``, ``"18M"``, ``"30Y"`` (case-insensitive) or the alias ``"ON"``.

        Raises:
            ConventionError: If the label is not a valid tenor.
        """
        text = _TENOR_ALIASES.get(label.strip().upper(), label)
        match = _TENOR_PATTERN.match(text)
        if match is None:
            raise ConventionError(f"invalid tenor: {label!r}", {"tenor": label})
        return cls(int(match.group(1)), TimeUnit(match.group(2).upper()))

    def __neg__(self) -> Tenor:
        return Tenor(-self.length, self.unit)

    def __mul__(self, factor: int) -> Tenor:
        return Tenor(self.length * factor, self.unit)

    __rmul__ = __mul__

    def __str__(self) -> str:
        return f"{self.length}{self.unit.value}"

    def add_to(self, d: date) -> date:
        """Calendar-free date arithmetic: ``d + tenor`` without business day adjustment.

        Months and years clamp the day of month to the target month length.
        """
        match self.unit:
            case TimeUnit.DAYS:
                return d + timedelta(days=self.length)
            case TimeUnit.WEEKS:
                return d + timedelta(days=DAYS_PER_WEEK * self.length)
            case TimeUnit.MONTHS:
                return add_months(d, self.length)
            case TimeUnit.YEARS:
                return add_months(d, MONTHS_PER_YEAR * self.length)


class Frequency(Enum):
    """Coupon frequency; the value is the number of periods per year."""

    ANNUAL = 1
    SEMIANNUAL = 2
    QUARTERLY = 4
    MONTHLY = 12

    @classmethod
    def parse(cls, name: str) -> Frequency:
        """Parse a frequency name such as ``"ANNUAL"`` (case-insensitive).

        Raises:
            ConventionError: If the name is unknown.
        """
        try:
            return cls[name.strip().upper()]
        except KeyError:
            raise ConventionError(f"invalid frequency: {name!r}", {"frequency": name}) from None

    @property
    def tenor(self) -> Tenor:
        """Length of one period (``ANNUAL`` -> ``1Y``, ``QUARTERLY`` -> ``3M``)."""
        if self is Frequency.ANNUAL:
            return Tenor(1, TimeUnit.YEARS)
        return Tenor(MONTHS_PER_YEAR // self.value, TimeUnit.MONTHS)
