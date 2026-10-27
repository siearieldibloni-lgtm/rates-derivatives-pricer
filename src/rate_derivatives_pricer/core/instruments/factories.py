"""Instrument factories: build legs from a schedule and a contract description.

Date logic of a spot-starting OIS (same as QuantLib ``MakeOIS``, against which it is validated):

* spot date = valuation date (adjusted to a business day) + ``spot_lag_days`` business days;
* the end-of-month rule is used if enabled and the start date is the last business day of its
  month; the maturity is then the last business day of the target month, otherwise it is
  ``start + tenor`` (unadjusted, adjusted by the schedule with the business day convention).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from rate_derivatives_pricer.core.conventions.business_day import BusinessDayConvention
from rate_derivatives_pricer.core.conventions.currency import CurrencyConventions, get_conventions
from rate_derivatives_pricer.core.conventions.tenor import Tenor
from rate_derivatives_pricer.core.errors import ConventionError
from rate_derivatives_pricer.core.instruments.cashflows import (
    FixedCoupon,
    IborCoupon,
    OvernightCompoundedCoupon,
)
from rate_derivatives_pricer.core.instruments.legs import Leg
from rate_derivatives_pricer.core.instruments.swaps import OisSwap, SwapDirection, VanillaIrs
from rate_derivatives_pricer.core.market.indices import IborIndex
from rate_derivatives_pricer.core.registry import INSTRUMENTS
from rate_derivatives_pricer.core.schedule.generator import Schedule, ScheduleGenerator

FIXED_LEG_NAME = "fixed"
OVERNIGHT_LEG_NAME = "overnight"
FLOATING_LEG_NAME = "floating"


def spot_date(conventions: CurrencyConventions, valuation_date: date) -> date:
    """Spot date: valuation date adjusted (following), plus the spot lag in business days."""
    calendar = conventions.calendar
    trade_date = calendar.adjust(valuation_date, BusinessDayConvention.FOLLOWING)
    return calendar.advance_business_days(trade_date, conventions.spot_lag_days)


def uses_end_of_month(conventions: CurrencyConventions, start_date: date) -> bool:
    """Whether the OIS end-of-month rule applies to a swap starting on ``start_date``."""
    return conventions.ois.end_of_month and conventions.calendar.is_end_of_month(start_date)


def ois_maturity_date(conventions: CurrencyConventions, start_date: date, tenor: Tenor) -> date:
    """Maturity of an OIS of ``tenor`` starting on ``start_date`` (before schedule adjustment)."""
    if uses_end_of_month(conventions, start_date):
        return conventions.calendar.advance(
            start_date, tenor, conventions.ois.business_day_convention, end_of_month=True
        )
    return tenor.add_to(start_date)


def _ois_schedule(
    conventions: CurrencyConventions, tenor: Tenor, start: date, end: date, end_of_month: bool
) -> Schedule:
    ois = conventions.ois
    return ScheduleGenerator(
        tenor=tenor,
        calendar=conventions.calendar,
        convention=ois.business_day_convention,
        termination_convention=ois.business_day_convention,
        rule=ois.rule,
        end_of_month=end_of_month,
        payment_lag_days=ois.payment_lag_days,
        payment_calendar=conventions.calendar,
    ).generate(start, end)


def build_ois_swap(
    conventions: CurrencyConventions,
    notional: float,
    fixed_rate: float,
    direction: SwapDirection,
    start_date: date,
    end_date: date,
) -> OisSwap:
    """Build an OIS between two dates with the currency's OIS conventions.

    Args:
        conventions: Market conventions of the swap currency.
        notional: Notional (positive), in the swap currency.
        fixed_rate: Fixed rate (decimal).
        direction: Pay or receive the fixed rate.
        start_date: Effective date (adjusted by the schedule if not a business day).
        end_date: Maturity date (adjusted by the schedule).

    Raises:
        ConventionError: If the notional is not positive.
        ScheduleError: If the dates do not define a schedule.
    """
    if not notional > 0.0:
        raise ConventionError("notional must be positive", {"notional": notional})
    ois = conventions.ois
    end_of_month = uses_end_of_month(conventions, start_date)
    fixed_schedule = _ois_schedule(
        conventions, ois.fixed_frequency.tenor, start_date, end_date, end_of_month
    )
    floating_schedule = _ois_schedule(
        conventions, ois.floating_frequency.tenor, start_date, end_date, end_of_month
    )
    index = conventions.overnight_index

    fixed_leg = Leg(
        name=FIXED_LEG_NAME,
        cashflows=tuple(
            FixedCoupon(
                notional=notional,
                accrual_start=period.accrual_start,
                accrual_end=period.accrual_end,
                payment_date=period.payment_date,
                day_count=ois.fixed_day_count,
                fixed_rate=fixed_rate,
            )
            for period in fixed_schedule.periods
        ),
        notional=notional,
        direction=direction.fixed_leg_direction,
    )
    overnight_leg = Leg(
        name=OVERNIGHT_LEG_NAME,
        cashflows=tuple(
            OvernightCompoundedCoupon(
                notional=notional,
                accrual_start=period.accrual_start,
                accrual_end=period.accrual_end,
                payment_date=period.payment_date,
                day_count=ois.floating_day_count,
                index=index,
            )
            for period in floating_schedule.periods
        ),
        notional=notional,
        direction=direction.floating_leg_direction,
    )
    return OisSwap(
        swap_currency=conventions.currency,
        notional=notional,
        fixed_rate=fixed_rate,
        direction=direction,
        start_date=fixed_schedule.start_date,
        end_date=fixed_schedule.end_date,
        index=index,
        fixed_leg=fixed_leg,
        overnight_leg=overnight_leg,
    )


def build_spot_ois_swap(
    conventions: CurrencyConventions,
    valuation_date: date,
    tenor: Tenor,
    notional: float,
    fixed_rate: float,
    direction: SwapDirection,
) -> OisSwap:
    """Build a spot-starting OIS of ``tenor`` (the instrument behind an OIS quote)."""
    start = spot_date(conventions, valuation_date)
    end = ois_maturity_date(conventions, start, tenor)
    return build_ois_swap(conventions, notional, fixed_rate, direction, start, end)


def _parse_date(value: Any, field: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ConventionError(f"invalid date for {field}: {value!r}", {"field": field}) from None


def _required_swap_fields(params: Mapping[str, Any]) -> tuple[float, float, SwapDirection]:
    """``notional``, ``fixed_rate`` and ``direction`` of a fixed-vs-floating swap description."""
    try:
        notional = float(params["notional"])
        fixed_rate = float(params["fixed_rate"])
        direction = SwapDirection(str(params["direction"]).lower())
    except KeyError as missing:
        raise ConventionError(
            f"missing trade field {missing.args[0]}", {"field": missing.args[0]}
        ) from None
    except ValueError as invalid:
        raise ConventionError(f"invalid trade field: {invalid}") from None
    return notional, fixed_rate, direction


def _optional_dates(params: Mapping[str, Any]) -> tuple[date | None, str | None, date | None]:
    """``start_date``, ``maturity`` and ``end_date`` (exactly one of the last two is given)."""
    maturity, end_value = params.get("maturity"), params.get("end_date")
    if (maturity is None) == (end_value is None):
        raise ConventionError(
            "exactly one of maturity or end_date is required", {"fields": ["maturity", "end_date"]}
        )
    start_value = params.get("start_date")
    return (
        _parse_date(start_value, "start_date") if start_value is not None else None,
        None if maturity is None else str(maturity),
        _parse_date(end_value, "end_date") if end_value is not None else None,
    )


@INSTRUMENTS.register(OisSwap.instrument_type)
def ois_swap_factory(params: Mapping[str, Any], valuation_date: date) -> OisSwap:
    """Build an :class:`OisSwap` from a trade description.

    Args:
        params: ``currency`` (default ``"EUR"``), ``notional`` (> 0), ``fixed_rate`` (decimal),
            ``direction`` (``"pay_fixed"`` | ``"receive_fixed"``), ``start_date`` (optional,
            ISO date; spot by default) and exactly one of ``maturity`` (tenor, e.g. ``"5Y"``)
            or ``end_date`` (ISO date).
        valuation_date: Valuation date, used to compute the spot start date.

    Raises:
        ConventionError: If a field is missing or invalid.
        ScheduleError: If the dates do not define a schedule.
    """
    conventions = get_conventions(str(params.get("currency", "EUR")))
    notional, fixed_rate, direction = _required_swap_fields(params)
    start_date, maturity, end_date = _optional_dates(params)
    start = start_date if start_date is not None else spot_date(conventions, valuation_date)
    end = (
        ois_maturity_date(conventions, start, Tenor.parse(maturity))
        if maturity is not None
        else end_date
    )
    assert end is not None  # exactly one of maturity / end_date was checked
    return build_ois_swap(conventions, notional, fixed_rate, direction, start, end)


# --------------------------------------------------------------------------- vanilla IRS
def ibor_swap_maturity_date(
    conventions: CurrencyConventions, start_date: date, tenor: Tenor
) -> date:
    """Maturity of a vanilla swap of ``tenor`` starting on ``start_date`` (before adjustment).

    Same rule as :func:`ois_maturity_date` with the vanilla swap conventions; the end-of-month
    rule is off for EUR (ADR-027), so the maturity is ``start + tenor`` adjusted by the schedule.
    """
    swap = conventions.require_ibor_swap()
    if swap.end_of_month and conventions.calendar.is_end_of_month(start_date):
        return conventions.calendar.advance(
            start_date, tenor, swap.business_day_convention, end_of_month=True
        )
    return tenor.add_to(start_date)


def _ibor_schedule(
    conventions: CurrencyConventions, tenor: Tenor, start: date, end: date
) -> Schedule:
    swap = conventions.require_ibor_swap()
    end_of_month = swap.end_of_month and conventions.calendar.is_end_of_month(start)
    return ScheduleGenerator(
        tenor=tenor,
        calendar=conventions.calendar,
        convention=swap.business_day_convention,
        termination_convention=swap.business_day_convention,
        rule=swap.rule,
        end_of_month=end_of_month,
        payment_lag_days=swap.payment_lag_days,
        payment_calendar=conventions.calendar,
    ).generate(start, end)


def build_vanilla_irs(
    conventions: CurrencyConventions,
    index: IborIndex,
    notional: float,
    fixed_rate: float,
    direction: SwapDirection,
    start_date: date,
    end_date: date,
) -> VanillaIrs:
    """Build a vanilla IRS between two dates with the currency's swap conventions.

    The fixed leg uses the swap conventions; the floating leg pays the Ibor ``index`` at the
    index tenor, with the index day count and a fixing ``index.fixing_days`` before each period.

    Args:
        conventions: Market conventions of the swap currency.
        index: Ibor index of the floating leg.
        notional: Notional (positive), in the swap currency.
        fixed_rate: Fixed rate (decimal).
        direction: Pay or receive the fixed rate.
        start_date: Effective date (adjusted by the schedule if not a business day).
        end_date: Maturity date (adjusted by the schedule).

    Raises:
        ConventionError: If the notional is not positive or the conventions are missing.
        ScheduleError: If the dates do not define a schedule.
    """
    if not notional > 0.0:
        raise ConventionError("notional must be positive", {"notional": notional})
    swap = conventions.require_ibor_swap()
    fixed_schedule = _ibor_schedule(conventions, swap.fixed_frequency.tenor, start_date, end_date)
    floating_schedule = _ibor_schedule(conventions, index.tenor, start_date, end_date)

    fixed_leg = Leg(
        name=FIXED_LEG_NAME,
        cashflows=tuple(
            FixedCoupon(
                notional=notional,
                accrual_start=period.accrual_start,
                accrual_end=period.accrual_end,
                payment_date=period.payment_date,
                day_count=swap.fixed_day_count,
                fixed_rate=fixed_rate,
            )
            for period in fixed_schedule.periods
        ),
        notional=notional,
        direction=direction.fixed_leg_direction,
    )
    floating_leg = Leg(
        name=FLOATING_LEG_NAME,
        cashflows=tuple(
            IborCoupon(
                notional=notional,
                accrual_start=period.accrual_start,
                accrual_end=period.accrual_end,
                payment_date=period.payment_date,
                day_count=index.day_count,
                index=index,
                fixing_date=index.fixing_date(period.accrual_start),
            )
            for period in floating_schedule.periods
        ),
        notional=notional,
        direction=direction.floating_leg_direction,
    )
    return VanillaIrs(
        swap_currency=conventions.currency,
        notional=notional,
        fixed_rate=fixed_rate,
        direction=direction,
        start_date=fixed_schedule.start_date,
        end_date=fixed_schedule.end_date,
        index=index,
        fixed_leg=fixed_leg,
        floating_leg=floating_leg,
    )


def build_spot_vanilla_irs(
    conventions: CurrencyConventions,
    index: IborIndex,
    valuation_date: date,
    tenor: Tenor,
    notional: float,
    fixed_rate: float,
    direction: SwapDirection,
) -> VanillaIrs:
    """Build a spot-starting vanilla IRS of ``tenor`` (the instrument behind an IRS quote)."""
    start = spot_date(conventions, valuation_date)
    end = ibor_swap_maturity_date(conventions, start, tenor)
    return build_vanilla_irs(conventions, index, notional, fixed_rate, direction, start, end)


@INSTRUMENTS.register(VanillaIrs.instrument_type)
def vanilla_irs_factory(params: Mapping[str, Any], valuation_date: date) -> VanillaIrs:
    """Build a :class:`VanillaIrs` from a trade description.

    Args:
        params: ``currency`` (default ``"EUR"``), ``index`` (default: the currency's default
            Ibor index, e.g. ``"EURIBOR6M"``), ``notional`` (> 0), ``fixed_rate`` (decimal),
            ``direction`` (``"pay_fixed"`` | ``"receive_fixed"``), ``start_date`` (optional,
            ISO date; spot by default) and exactly one of ``maturity`` (tenor, e.g. ``"5Y"``)
            or ``end_date`` (ISO date).
        valuation_date: Valuation date, used to compute the spot start date.

    Raises:
        ConventionError: If a field is missing or invalid, or the index is unknown.
        ScheduleError: If the dates do not define a schedule.
    """
    conventions = get_conventions(str(params.get("currency", "EUR")))
    index = conventions.ibor_index(params.get("index"))
    notional, fixed_rate, direction = _required_swap_fields(params)
    start_date, maturity, end_date = _optional_dates(params)
    start = start_date if start_date is not None else spot_date(conventions, valuation_date)
    end = (
        ibor_swap_maturity_date(conventions, start, Tenor.parse(maturity))
        if maturity is not None
        else end_date
    )
    assert end is not None  # exactly one of maturity / end_date was checked
    return build_vanilla_irs(conventions, index, notional, fixed_rate, direction, start, end)
