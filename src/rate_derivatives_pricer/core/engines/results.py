"""Pricing results: never a bare float (PV, legs, cashflows, par rate, annuity, diagnostics)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True, slots=True)
class CashflowResult:
    """One row of the cashflow table.

    Attributes:
        leg: Leg name (``"fixed"``, ``"overnight"``).
        accrual_start: Accrual start.
        accrual_end: Accrual end.
        payment_date: Payment date.
        year_fraction: Accrual year fraction.
        notional: Notional (positive).
        rate: Coupon rate (decimal); compounded rate for overnight coupons.
        amount: Signed amount (positive = received), in the trade currency.
        discount_factor: Discount factor at the payment date.
        present_value: Signed present value ``amount * discount_factor``.
    """

    leg: str
    accrual_start: date
    accrual_end: date
    payment_date: date
    year_fraction: float
    notional: float
    rate: float
    amount: float
    discount_factor: float
    present_value: float


@dataclass(frozen=True, slots=True)
class LegResult:
    """Present value of one leg.

    Attributes:
        name: Leg name.
        direction: ``"pay"`` or ``"receive"``.
        present_value: Signed PV in the trade currency.
    """

    name: str
    direction: str
    present_value: float


@dataclass(frozen=True, slots=True)
class PricingDiagnostics:
    """Technical context of a pricing.

    Attributes:
        engine: Engine name.
        elapsed_ms: Wall-clock pricing time in milliseconds.
        core_version: Version of the quant core.
        warnings: Non-fatal issues met while pricing.
    """

    engine: str
    elapsed_ms: float
    core_version: str
    warnings: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class PricingResult:
    """Full pricing output of a swap.

    Attributes:
        instrument_type: Priced instrument type.
        currency: Trade currency.
        valuation_date: Valuation date.
        present_value: Signed PV of the trade (sum of the legs), in the trade currency.
        legs: PV per leg.
        cashflows: Future cashflows (payment after the valuation date).
        par_rate: Fixed rate giving a zero PV for the remaining cashflows (decimal).
        annuity: ``sum_i N * tau_i * P(pay_i)`` over the remaining fixed coupons (currency per
            unit of rate, unsigned).
        pvbp: PV change of the fixed leg for a 1bp change of the fixed rate: ``annuity * 1e-4``.
        diagnostics: Timing, versions and warnings.
    """

    instrument_type: str
    currency: str
    valuation_date: date
    present_value: float
    legs: tuple[LegResult, ...]
    cashflows: tuple[CashflowResult, ...]
    par_rate: float
    annuity: float
    pvbp: float
    diagnostics: PricingDiagnostics
