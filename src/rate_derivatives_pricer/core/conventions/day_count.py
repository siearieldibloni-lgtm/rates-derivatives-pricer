"""Day count conventions: year fraction between two dates (ISDA 2006 definitions, section 4.16)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from typing import ClassVar

from rate_derivatives_pricer.core.registry import DAY_COUNTS

DAYS_PER_YEAR_360 = 360.0
DAYS_PER_YEAR_365 = 365.0
DAYS_PER_30_360_MONTH = 30
MONTHS_PER_YEAR = 12
LAST_DAY_31 = 31


class DayCountConvention(ABC):
    """Computes the accrual year fraction between two dates (stateless value object)."""

    #: Stable identifier used in configuration and in the API (registry key).
    name: ClassVar[str]

    @abstractmethod
    def day_count(self, start: date, end: date) -> int:
        """Number of days between ``start`` and ``end`` under this convention (signed)."""

    @abstractmethod
    def year_fraction(self, start: date, end: date) -> float:
        """Year fraction from ``start`` (included) to ``end`` (excluded); negative if reversed."""


@DAY_COUNTS.register("ACT/360")
@dataclass(frozen=True, slots=True)
class Act360(DayCountConvention):
    """Actual/360: actual calendar days divided by 360 (money market, €STR, Euribor)."""

    name: ClassVar[str] = "ACT/360"

    def day_count(self, start: date, end: date) -> int:
        """Actual number of calendar days."""
        return (end - start).days

    def year_fraction(self, start: date, end: date) -> float:
        """``(end - start) / 360``."""
        return self.day_count(start, end) / DAYS_PER_YEAR_360


@DAY_COUNTS.register("ACT/365F")
@dataclass(frozen=True, slots=True)
class Act365Fixed(DayCountConvention):
    """Actual/365 (Fixed): actual calendar days divided by 365; also the curves' time measure."""

    name: ClassVar[str] = "ACT/365F"

    def day_count(self, start: date, end: date) -> int:
        """Actual number of calendar days."""
        return (end - start).days

    def year_fraction(self, start: date, end: date) -> float:
        """``(end - start) / 365``."""
        return self.day_count(start, end) / DAYS_PER_YEAR_365


def _thirty_360_days(start: date, d1: int, end: date, d2: int) -> int:
    """``360 (Y2 - Y1) + 30 (M2 - M1) + (D2 - D1)`` with already adjusted days of month."""
    return (
        DAYS_PER_30_360_MONTH * MONTHS_PER_YEAR * (end.year - start.year)
        + DAYS_PER_30_360_MONTH * (end.month - start.month)
        + (d2 - d1)
    )


@DAY_COUNTS.register("30/360")
@dataclass(frozen=True, slots=True)
class Thirty360(DayCountConvention):
    """30/360 Bond Basis (ISDA 2006 section 4.16(f)).

    ``D1 = 31`` becomes 30; ``D2 = 31`` becomes 30 only if ``D1`` is (then) 30.
    """

    name: ClassVar[str] = "30/360"

    def day_count(self, start: date, end: date) -> int:
        """30/360 Bond Basis day count."""
        d1 = min(start.day, DAYS_PER_30_360_MONTH)
        d2 = end.day
        if d2 == LAST_DAY_31 and d1 == DAYS_PER_30_360_MONTH:
            d2 = DAYS_PER_30_360_MONTH
        return _thirty_360_days(start, d1, end, d2)

    def year_fraction(self, start: date, end: date) -> float:
        """Day count divided by 360."""
        return self.day_count(start, end) / DAYS_PER_YEAR_360


@DAY_COUNTS.register("30E/360")
@dataclass(frozen=True, slots=True)
class Thirty360European(DayCountConvention):
    """30E/360 Eurobond Basis (ISDA 2006 section 4.16(g)): any 31st becomes the 30th."""

    name: ClassVar[str] = "30E/360"

    def day_count(self, start: date, end: date) -> int:
        """30E/360 day count."""
        d1 = min(start.day, DAYS_PER_30_360_MONTH)
        d2 = min(end.day, DAYS_PER_30_360_MONTH)
        return _thirty_360_days(start, d1, end, d2)

    def year_fraction(self, start: date, end: date) -> float:
        """Day count divided by 360."""
        return self.day_count(start, end) / DAYS_PER_YEAR_360


def day_count_from_name(name: str) -> DayCountConvention:
    """Instantiate a registered day count convention (``"ACT/360"``, ``"ACT/365F"``, ...).

    Raises:
        ConventionError: If the name is unknown.
    """
    return DAY_COUNTS.get(name.strip().upper())()
