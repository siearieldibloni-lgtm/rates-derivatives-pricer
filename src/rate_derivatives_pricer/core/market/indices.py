"""Interest rate index definitions (€STR, SOFR, Euribor...)."""

from __future__ import annotations

from dataclasses import dataclass

from rate_derivatives_pricer.core.conventions.calendar import Calendar
from rate_derivatives_pricer.core.conventions.day_count import DayCountConvention


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
