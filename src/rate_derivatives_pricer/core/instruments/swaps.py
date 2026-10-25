"""Swap instruments: pure contract descriptions (engines compute, ADR-007)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import ClassVar

from rate_derivatives_pricer.core.instruments.cashflows import (
    FixedCoupon,
    OvernightCompoundedCoupon,
)
from rate_derivatives_pricer.core.instruments.legs import Leg, PayReceive
from rate_derivatives_pricer.core.market.indices import OvernightIndex


class Instrument(ABC):
    """A financial contract; it does not carry its pricing engine (unlike QuantLib)."""

    #: Stable instrument type identifier (registry key, API discriminator).
    instrument_type: ClassVar[str]

    @property
    @abstractmethod
    def currency(self) -> str:
        """ISO 4217 code of the instrument's cashflows."""


class SwapDirection(StrEnum):
    """Direction of a fixed-vs-floating swap, named after the fixed leg."""

    #: Pay the fixed rate, receive the floating leg (payer swap).
    PAY_FIXED = "pay_fixed"
    #: Receive the fixed rate, pay the floating leg (receiver swap).
    RECEIVE_FIXED = "receive_fixed"

    @property
    def fixed_leg_direction(self) -> PayReceive:
        """Direction of the fixed leg."""
        return PayReceive.PAY if self is SwapDirection.PAY_FIXED else PayReceive.RECEIVE

    @property
    def floating_leg_direction(self) -> PayReceive:
        """Direction of the floating leg (opposite of the fixed leg)."""
        return PayReceive.RECEIVE if self is SwapDirection.PAY_FIXED else PayReceive.PAY


class Swap(Instrument):
    """Exchange of two legs."""

    @property
    @abstractmethod
    def legs(self) -> tuple[Leg, Leg]:
        """The two legs."""


@dataclass(frozen=True, slots=True)
class OisSwap(Swap):
    """Overnight indexed swap: fixed leg vs overnight rate compounded in arrears.

    Attributes:
        swap_currency: ISO 4217 code.
        notional: Notional of both legs (positive; the direction gives the sign).
        fixed_rate: Fixed rate (decimal).
        direction: Pay or receive the fixed rate.
        start_date: Adjusted effective date.
        end_date: Adjusted maturity date (end of the last accrual period).
        index: Overnight index of the floating leg.
        fixed_leg: Fixed coupons.
        overnight_leg: Compounded overnight coupons.
    """

    instrument_type: ClassVar[str] = "ois_swap"

    swap_currency: str
    notional: float
    fixed_rate: float
    direction: SwapDirection
    start_date: date
    end_date: date
    index: OvernightIndex
    fixed_leg: Leg
    overnight_leg: Leg

    @property
    def currency(self) -> str:
        """ISO 4217 code."""
        return self.swap_currency

    @property
    def legs(self) -> tuple[Leg, Leg]:
        """``(fixed_leg, overnight_leg)``."""
        return (self.fixed_leg, self.overnight_leg)

    @property
    def fixed_coupons(self) -> tuple[FixedCoupon, ...]:
        """Fixed leg coupons (typed view)."""
        return tuple(cf for cf in self.fixed_leg.cashflows if isinstance(cf, FixedCoupon))

    @property
    def overnight_coupons(self) -> tuple[OvernightCompoundedCoupon, ...]:
        """Overnight leg coupons (typed view)."""
        return tuple(
            cf for cf in self.overnight_leg.cashflows if isinstance(cf, OvernightCompoundedCoupon)
        )
