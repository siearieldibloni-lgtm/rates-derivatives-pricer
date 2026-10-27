"""Interest rate index definitions (€STR, SOFR, Euribor...)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from rate_derivatives_pricer.core.conventions.business_day import BusinessDayConvention
from rate_derivatives_pricer.core.conventions.calendar import Calendar
from rate_derivatives_pricer.core.conventions.day_count import DayCountConvention
from rate_derivatives_pricer.core.conventions.tenor import Tenor


@dataclass(frozen=True, slots=True)
class OvernightIndex:
    """Overnight risk-free rate index such as €STR or SOFR.

    The fixing of business day ``d`` is the rate applying from ``d`` to the next business day of
    ``fixing_calendar``; it is published on the next business day.

    Attributes:
        name: Stable identifier (``"ESTR"``), also the key of the fixings in a snapshot.
        currency: ISO 4217 code of the index currency.
        fixing_calendar: Calendar defining the overnight periods.
        day_count: Accrual convention of the published rate (Act/360 for €STR).
    """

    name: str
    currency: str
    fixing_calendar: Calendar
    day_count: DayCountConvention


@dataclass(frozen=True, slots=True)
class IborIndex:
    """Term rate index such as Euribor 3M or 6M (fixed in advance, simple interest).

    The fixing of a coupon is observed ``fixing_days`` business days of ``fixing_calendar`` before
    the start of its accrual period and applies to that whole period. Projected fixings are the
    simply compounded forward rates of the index projection curve over the accrual period
    (QuantLib's default "par coupon" convention).

    Attributes:
        name: Stable identifier (``"EURIBOR6M"``), also the key of the fixings in a snapshot and
            of the projection curve in a market environment.
        currency: ISO 4217 code of the index currency.
        tenor: Tenor of the rate (``6M``); also the payment frequency of a swap floating leg.
        fixing_days: Business days between the fixing date and the start of the accrual period.
        fixing_calendar: Calendar counting the fixing days.
        day_count: Accrual convention of the rate (Act/360 for Euribor).
        business_day_convention: Adjustment of the dates of a period of this index.
        end_of_month: End-of-month rule of the index periods (used by index deposits).
    """

    name: str
    currency: str
    tenor: Tenor
    fixing_days: int
    fixing_calendar: Calendar
    day_count: DayCountConvention
    business_day_convention: BusinessDayConvention
    end_of_month: bool

    def fixing_date(self, accrual_start: date) -> date:
        """Fixing date of the period starting on ``accrual_start``."""
        return self.fixing_calendar.advance_business_days(accrual_start, -self.fixing_days)
