"""Holiday calendars and business day arithmetic.

Semantics follow the market (ISDA) and QuantLib, against which they are validated date by date:

* ``advance`` by ``nD`` moves by business days; by weeks, months or years it does calendar
  arithmetic first and then adjusts with the business day convention;
* the end-of-month rule applies when the start date is the *last business day* of its month: the
  result is then the last business day of the target month.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from typing import ClassVar

from rate_derivatives_pricer.core.conventions.business_day import BusinessDayConvention
from rate_derivatives_pricer.core.conventions.tenor import Tenor, TimeUnit, end_of_month
from rate_derivatives_pricer.core.errors import ConventionError
from rate_derivatives_pricer.core.registry import CALENDARS

ONE_DAY = timedelta(days=1)
SATURDAY = 5  # date.weekday(): Monday = 0 ... Sunday = 6

# TARGET2 holiday rules (Good Friday, Easter Monday, 1 May, 26 December) apply from 2000 on.
TARGET_FULL_RULES_START_YEAR = 2000
# Additional TARGET closing days on 31 December, before the current set of rules was fixed.
TARGET_DECEMBER_31_CLOSING_YEARS = frozenset({1998, 1999, 2001})
# Cache sizes: calendars are queried day by day over decades of schedules.
EASTER_CACHE_SIZE = 512
HOLIDAY_CACHE_SIZE = 65536


@lru_cache(maxsize=EASTER_CACHE_SIZE)
def easter_sunday(year: int) -> date:
    """Gregorian Easter Sunday (anonymous Gregorian / Meeus-Jones-Butcher algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month, day = divmod(h + ell - 7 * m + 114, 31)
    return date(year, month, day + 1)


class Calendar(ABC):
    """Business day calendar (stateless; subclasses are immutable value objects)."""

    #: Stable identifier used in configuration and in the API (registry key).
    name: ClassVar[str]

    @abstractmethod
    def is_business_day(self, d: date) -> bool:
        """Whether ``d`` is a business day (weekends and holidays are not)."""

    def is_holiday(self, d: date) -> bool:
        """Whether ``d`` is not a business day."""
        return not self.is_business_day(d)

    def is_end_of_month(self, d: date) -> bool:
        """Whether ``d`` is on or after the last business day of its month."""
        return d.month != self.adjust(d + ONE_DAY, BusinessDayConvention.FOLLOWING).month

    def end_of_month(self, d: date) -> date:
        """Last business day of the month of ``d``."""
        return self.adjust(end_of_month(d), BusinessDayConvention.PRECEDING)

    def adjust(
        self, d: date, convention: BusinessDayConvention = BusinessDayConvention.FOLLOWING
    ) -> date:
        """Move ``d`` to a business day according to ``convention``."""
        if convention is BusinessDayConvention.UNADJUSTED:
            return d
        step = -ONE_DAY if convention is BusinessDayConvention.PRECEDING else ONE_DAY
        adjusted = d
        while not self.is_business_day(adjusted):
            adjusted += step
        if convention is BusinessDayConvention.MODIFIED_FOLLOWING and adjusted.month != d.month:
            # Rolling forward crossed a month boundary: roll backward instead.
            return self.adjust(d, BusinessDayConvention.PRECEDING)
        return adjusted

    def advance_business_days(self, d: date, days: int) -> date:
        """Move by ``days`` business days (``0`` adjusts ``d`` to the following business day)."""
        if days == 0:
            return self.adjust(d, BusinessDayConvention.FOLLOWING)
        step = ONE_DAY if days > 0 else -ONE_DAY
        result, remaining = d, abs(days)
        while remaining > 0:
            result += step
            if self.is_business_day(result):
                remaining -= 1
        return result

    def advance(
        self,
        d: date,
        tenor: Tenor,
        convention: BusinessDayConvention = BusinessDayConvention.FOLLOWING,
        end_of_month: bool = False,
    ) -> date:
        """Move ``d`` by ``tenor``.

        Args:
            d: Start date.
            tenor: ``nD`` = business days; ``nW``/``nM``/``nY`` = calendar arithmetic, then
                adjusted.
            convention: Adjustment of the resulting date (ignored for business-day moves).
            end_of_month: Apply the end-of-month rule for month and year tenors: if ``d`` is the
                last business day of its month, return the last business day of the target month.
        """
        if tenor.unit is TimeUnit.DAYS:
            return self.advance_business_days(d, tenor.length)
        target = tenor.add_to(d)
        if tenor.unit is not TimeUnit.WEEKS and end_of_month and self.is_end_of_month(d):
            return self.end_of_month(target)
        return self.adjust(target, convention)

    def business_days_between(self, start: date, end: date) -> int:
        """Number of business days in ``[start, end)`` (start included, end excluded).

        Raises:
            ConventionError: If ``end`` is before ``start``.
        """
        if end < start:
            raise ConventionError(
                "end date before start date", {"start": start.isoformat(), "end": end.isoformat()}
            )
        return len(self.business_days(start, end - ONE_DAY))

    def business_days(self, start: date, end: date) -> list[date]:
        """All business days in ``[start, end]`` (both included), in increasing order."""
        days, current = [], start
        while current <= end:
            if self.is_business_day(current):
                days.append(current)
            current += ONE_DAY
        return days


@CALENDARS.register("NULL")
@dataclass(frozen=True, slots=True)
class NullCalendar(Calendar):
    """Every day is a business day (used for unadjusted schedule arithmetic)."""

    name: ClassVar[str] = "NULL"

    def is_business_day(self, d: date) -> bool:
        """Always ``True``."""
        return True


@CALENDARS.register("WEEKENDS_ONLY")
@dataclass(frozen=True, slots=True)
class WeekendsOnlyCalendar(Calendar):
    """Saturdays and Sundays are the only holidays."""

    name: ClassVar[str] = "WEEKENDS_ONLY"

    def is_business_day(self, d: date) -> bool:
        """``False`` on Saturdays and Sundays."""
        return d.weekday() < SATURDAY


@CALENDARS.register("TARGET")
@dataclass(frozen=True, slots=True)
class TargetCalendar(Calendar):
    """TARGET (Trans-European Automated Real-time Gross settlement Express Transfer) calendar.

    Holidays: weekends, 1 January, Good Friday, Easter Monday, 1 May, 25 and 26 December, plus
    31 December in 1998, 1999 and 2001. Good Friday, Easter Monday, 1 May and 26 December are
    holidays from 2000 on.
    """

    name: ClassVar[str] = "TARGET"

    def is_business_day(self, d: date) -> bool:
        """``False`` on weekends and TARGET holidays."""
        return d.weekday() < SATURDAY and not _is_target_holiday(d)


@lru_cache(maxsize=HOLIDAY_CACHE_SIZE)
def _is_target_holiday(d: date) -> bool:
    """TARGET holiday rules for a weekday (cached: calendars are queried day by day)."""
    year, month, day = d.year, d.month, d.day
    if (month, day) in {(1, 1), (12, 25)}:
        return True
    if year >= TARGET_FULL_RULES_START_YEAR:
        easter = easter_sunday(year)
        if d in {easter - 2 * ONE_DAY, easter + ONE_DAY}:  # Good Friday, Easter Monday
            return True
        if (month, day) in {(5, 1), (12, 26)}:  # Labour Day, Boxing Day
            return True
    return (month, day) == (12, 31) and year in TARGET_DECEMBER_31_CLOSING_YEARS


@dataclass(frozen=True, slots=True)
class JointCalendar(Calendar):
    """Union of holidays: a day is a business day only if it is one in every calendar."""

    calendars: tuple[Calendar, ...]
    name: ClassVar[str] = "JOINT"

    def __post_init__(self) -> None:
        if not self.calendars:
            raise ConventionError("a joint calendar needs at least one calendar")

    @property
    def label(self) -> str:
        """Readable identifier, e.g. ``"TARGET+WEEKENDS_ONLY"``."""
        return "+".join(c.name for c in self.calendars)

    def is_business_day(self, d: date) -> bool:
        """``True`` if ``d`` is a business day in all underlying calendars."""
        return all(c.is_business_day(d) for c in self.calendars)


def calendar_from_name(name: str) -> Calendar:
    """Instantiate a registered calendar; ``"A+B"`` builds a :class:`JointCalendar`.

    Raises:
        ConventionError: If a calendar name is unknown.
    """
    parts = [part.strip().upper() for part in name.split("+")]
    calendars = tuple(CALENDARS.get(part)() for part in parts)
    return calendars[0] if len(calendars) == 1 else JointCalendar(calendars)
