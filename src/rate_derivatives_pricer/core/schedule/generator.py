"""Coupon schedule generation (accrual periods and payment dates).

The algorithm reproduces the market (and QuantLib ``Schedule``) behaviour, against which it is
validated date by date:

1. *Unadjusted* dates are rolled from the termination date backwards (``BACKWARD``) or from the
   effective date forwards (``FORWARD``) by multiples of the coupon tenor; a remaining short
   period becomes a stub at the front (backward) or at the back (forward).
2. If the end-of-month rule is on and the roll date (termination date for backward generation,
   effective date for forward) is the last business day of its month, every intermediate date and
   the roll date are moved to the last business day of their month; the other end date is only
   adjusted with its business day convention.
3. Otherwise dates are adjusted with the business day convention; the termination date uses its
   own convention.
4. Payment dates are the adjusted accrual end dates moved by ``payment_lag_days`` business days.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from rate_derivatives_pricer.core.conventions.business_day import BusinessDayConvention
from rate_derivatives_pricer.core.conventions.calendar import Calendar, NullCalendar
from rate_derivatives_pricer.core.conventions.tenor import Tenor, end_of_month
from rate_derivatives_pricer.core.errors import ScheduleError

# Guard against runaway loops on absurd inputs (a 100Y monthly schedule has 1200 periods).
MAX_SCHEDULE_PERIODS = 5000
_NULL_CALENDAR = NullCalendar()


class DateGenerationRule(StrEnum):
    """Direction in which unadjusted schedule dates are rolled."""

    #: Roll back from the termination date; a stub, if any, is the first period (market standard).
    BACKWARD = "BACKWARD"
    #: Roll forward from the effective date; a stub, if any, is the last period.
    FORWARD = "FORWARD"


@dataclass(frozen=True, slots=True)
class SchedulePeriod:
    """One accrual period of a schedule.

    Attributes:
        accrual_start: Adjusted start date (accrual includes this date).
        accrual_end: Adjusted end date (accrual excludes this date).
        payment_date: Date on which the period's cashflow is paid.
        is_regular: ``False`` for a stub period.
    """

    accrual_start: date
    accrual_end: date
    payment_date: date
    is_regular: bool = True


@dataclass(frozen=True, slots=True)
class Schedule:
    """Ordered sequence of contiguous accrual periods (immutable).

    Attributes:
        dates: Adjusted schedule dates ``d_0 < d_1 < ... < d_n``; period ``i`` is
            ``[d_i, d_{i+1})``.
        periods: The ``n`` accrual periods with their payment dates.
    """

    dates: tuple[date, ...]
    periods: tuple[SchedulePeriod, ...]

    @property
    def start_date(self) -> date:
        """Adjusted effective date."""
        return self.dates[0]

    @property
    def end_date(self) -> date:
        """Adjusted termination date."""
        return self.dates[-1]

    def __len__(self) -> int:
        return len(self.periods)


@dataclass(frozen=True, slots=True)
class ScheduleGenerator:
    """Parametrised schedule generator (one instance per leg convention).

    Attributes:
        tenor: Coupon period length (e.g. ``1Y`` for an annual leg).
        calendar: Calendar used to adjust accrual dates.
        convention: Business day convention of the effective and intermediate dates.
        termination_convention: Business day convention of the termination date.
        rule: Direction of date generation.
        end_of_month: End-of-month rule (applies only if the roll date is a month end).
        payment_lag_days: Business days between accrual end and payment (0 = paid at period end).
        payment_calendar: Calendar for the payment lag; defaults to ``calendar``.
        payment_convention: Adjustment of the payment date when the lag is zero.
    """

    tenor: Tenor
    calendar: Calendar
    convention: BusinessDayConvention = BusinessDayConvention.MODIFIED_FOLLOWING
    termination_convention: BusinessDayConvention = BusinessDayConvention.MODIFIED_FOLLOWING
    rule: DateGenerationRule = DateGenerationRule.BACKWARD
    end_of_month: bool = False
    payment_lag_days: int = 0
    payment_calendar: Calendar | None = None
    payment_convention: BusinessDayConvention = BusinessDayConvention.FOLLOWING

    def generate(self, effective_date: date, termination_date: date) -> Schedule:
        """Build the schedule between two (possibly unadjusted) dates.

        Args:
            effective_date: Start of the first accrual period.
            termination_date: End of the last accrual period (exclusive).

        Raises:
            ScheduleError: If ``termination_date`` is not after ``effective_date`` or the tenor
                is not positive.
        """
        if termination_date <= effective_date:
            raise ScheduleError(
                "termination date must be after effective date",
                {
                    "effective": effective_date.isoformat(),
                    "termination": termination_date.isoformat(),
                },
            )
        if self.tenor.length <= 0:
            raise ScheduleError("schedule tenor must be positive", {"tenor": str(self.tenor)})

        unadjusted, regular = self._unadjusted_dates(effective_date, termination_date)
        dates = self._adjust(unadjusted, effective_date, termination_date)
        dates, regular = _drop_degenerate_dates(dates, regular)
        if len(dates) < 2:
            raise ScheduleError(
                "schedule collapses to a single date after adjustment",
                {
                    "effective": effective_date.isoformat(),
                    "termination": termination_date.isoformat(),
                },
            )
        payment_calendar = self.payment_calendar or self.calendar
        periods = tuple(
            SchedulePeriod(
                accrual_start=start,
                accrual_end=end,
                payment_date=self._payment_date(end, payment_calendar),
                is_regular=is_regular,
            )
            for start, end, is_regular in zip(dates[:-1], dates[1:], regular, strict=True)
        )
        return Schedule(dates=tuple(dates), periods=periods)

    def _payment_date(self, accrual_end: date, payment_calendar: Calendar) -> date:
        """Accrual end moved by the payment lag (business days of the payment calendar)."""
        if self.payment_lag_days == 0:
            return payment_calendar.adjust(accrual_end, self.payment_convention)
        return payment_calendar.advance_business_days(accrual_end, self.payment_lag_days)

    def _unadjusted_dates(
        self, effective: date, termination: date
    ) -> tuple[list[date], list[bool]]:
        """Step 1: roll unadjusted dates; returns ``(dates, is_regular per period)``.

        Dates are generated with plain calendar arithmetic (null calendar), keeping the
        end-of-month property of the roll date when the rule is on. A candidate that would
        coincide with the previous date after adjustment is skipped.
        """
        backward = self.rule is DateGenerationRule.BACKWARD
        seed, exit_date = (termination, effective) if backward else (effective, termination)
        step = -self.tenor if backward else self.tenor
        dates, regular = [seed], []
        for periods in range(1, MAX_SCHEDULE_PERIODS + 1):
            candidate = _NULL_CALENDAR.advance(
                seed, step * periods, self.convention, self.end_of_month
            )
            past_exit = candidate < exit_date if backward else candidate > exit_date
            if past_exit:
                break
            if self._adjusted(dates[-1]) != self._adjusted(candidate):
                dates.append(candidate)
                regular.append(True)
        else:  # pragma: no cover - defensive, unreachable for sane tenors
            raise ScheduleError("too many schedule periods", {"tenor": str(self.tenor)})
        if self._adjusted(dates[-1]) != self._adjusted(exit_date):
            dates.append(exit_date)
            regular.append(False)  # stub
        if backward:
            dates.reverse()
            regular.reverse()
        return dates, regular

    def _adjusted(self, d: date) -> date:
        return self.calendar.adjust(d, self.convention)

    def _month_end(self, d: date) -> date:
        """Roll date end under the EOM rule: last (business) day of its month."""
        if self.termination_convention is BusinessDayConvention.UNADJUSTED:
            return end_of_month(d)
        return self.calendar.end_of_month(d)

    def _adjust(self, dates: list[date], effective: date, termination: date) -> list[date]:
        """Steps 2 and 3: end-of-month rule or business day adjustment."""
        roll_date = termination if self.rule is DateGenerationRule.BACKWARD else effective
        adjusted = list(dates)
        if self.end_of_month and self.calendar.is_end_of_month(roll_date):
            for i in range(1, len(adjusted) - 1):
                adjusted[i] = (
                    end_of_month(adjusted[i])
                    if self.convention is BusinessDayConvention.UNADJUSTED
                    else self.calendar.end_of_month(adjusted[i])
                )
            # Only the roll date end is moved to its month end; the other end is a plain date.
            if self.rule is DateGenerationRule.BACKWARD:
                adjusted[0] = self.calendar.adjust(adjusted[0], self.convention)
                adjusted[-1] = self._month_end(adjusted[-1])
            else:
                adjusted[0] = self._month_end(adjusted[0])
                adjusted[-1] = self.calendar.adjust(adjusted[-1], self.termination_convention)
            return adjusted
        adjusted[0] = self.calendar.adjust(adjusted[0], self.convention)
        for i in range(1, len(adjusted) - 1):
            adjusted[i] = self.calendar.adjust(adjusted[i], self.convention)
        adjusted[-1] = self.calendar.adjust(adjusted[-1], self.termination_convention)
        return adjusted


def _drop_degenerate_dates(dates: list[date], regular: list[bool]) -> tuple[list[date], list[bool]]:
    """Remove a next-to-last (or second) date that adjustment pushed onto or past an end date."""
    dates, regular = list(dates), list(regular)
    if len(dates) >= 3 and dates[-2] >= dates[-1]:
        # Merge the last two periods: the merged period keeps the stub flag of either.
        del dates[-2]
        regular[-2] = regular[-2] and regular[-1]
        del regular[-1]
    if len(dates) >= 3 and dates[1] <= dates[0]:
        del dates[1]
        regular[0] = regular[0] and regular[1]
        del regular[1]
    return dates, regular
