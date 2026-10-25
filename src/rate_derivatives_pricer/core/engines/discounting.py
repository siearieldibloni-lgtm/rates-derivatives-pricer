"""Discounting engine for swaps: sum of discounted cashflows per leg.

Conventions:

* a cashflow paid on or before the valuation date has occurred and is excluded (as QuantLib's
  default ``includeReferenceDateEvents = False``);
* ``PV = sum_legs sign(leg) * sum_i amount_i * P(pay_i)`` on the discount curve;
* ``annuity = sum_i N * tau_i * P(pay_i)`` over the remaining fixed coupons;
* ``par rate = PV(floating leg, unsigned) / annuity``.
"""

from __future__ import annotations

import logging
import time
from typing import ClassVar

import rate_derivatives_pricer
from rate_derivatives_pricer.core.engines.base import PricingEngine
from rate_derivatives_pricer.core.engines.results import (
    CashflowResult,
    LegResult,
    PricingDiagnostics,
    PricingResult,
)
from rate_derivatives_pricer.core.errors import PricingError, UnsupportedInstrumentError
from rate_derivatives_pricer.core.instruments.cashflows import Coupon
from rate_derivatives_pricer.core.instruments.legs import Leg
from rate_derivatives_pricer.core.instruments.swaps import Instrument, OisSwap
from rate_derivatives_pricer.core.market.environment import MarketEnvironment
from rate_derivatives_pricer.core.registry import ENGINES

logger = logging.getLogger(__name__)

BASIS_POINT = 1e-4
MILLISECONDS_PER_SECOND = 1000.0


def _alive_coupons(leg: Leg, env: MarketEnvironment) -> list[Coupon]:
    """Coupons of ``leg`` paid strictly after the valuation date."""
    return [
        cf
        for cf in leg.cashflows
        if isinstance(cf, Coupon) and cf.payment_date > env.valuation_date
    ]


def _unsigned_leg_value(leg: Leg, env: MarketEnvironment) -> float:
    """``sum_i amount_i * P(pay_i)`` over the remaining coupons, without the leg sign."""
    coupons = _alive_coupons(leg, env)
    if not coupons:
        return 0.0
    discount_factors = env.discount_curve.discount_factors([cf.payment_date for cf in coupons])
    return float(sum(cf.amount(env) * df for cf, df in zip(coupons, discount_factors, strict=True)))


def _annuity(leg: Leg, env: MarketEnvironment) -> float:
    """``sum_i N * tau_i * P(pay_i)`` over the remaining coupons of ``leg``."""
    coupons = _alive_coupons(leg, env)
    if not coupons:
        return 0.0
    discount_factors = env.discount_curve.discount_factors([cf.payment_date for cf in coupons])
    return float(
        sum(
            cf.notional * cf.year_fraction * df
            for cf, df in zip(coupons, discount_factors, strict=True)
        )
    )


@ENGINES.register((OisSwap.instrument_type, "discounting"))
class DiscountingSwapEngine(PricingEngine):
    """Prices fixed-vs-floating swaps by discounting projected cashflows on one curve."""

    method: ClassVar[str] = "discounting"
    supported_instruments: ClassVar[tuple[str, ...]] = (OisSwap.instrument_type,)

    def _check(self, instrument: Instrument) -> OisSwap:
        if not isinstance(instrument, OisSwap):
            raise UnsupportedInstrumentError(
                "DiscountingSwapEngine cannot price this instrument",
                {"instrument_type": instrument.instrument_type},
            )
        return instrument

    def par_rate(self, instrument: Instrument, env: MarketEnvironment) -> float:
        """Fixed rate that zeroes the PV of the remaining cashflows (decimal).

        Lightweight path used by the bootstrap (no cashflow table).

        Raises:
            PricingError: If no fixed coupon remains (annuity is zero).
        """
        swap = self._check(instrument)
        annuity = _annuity(swap.fixed_leg, env)
        if annuity == 0.0:
            raise PricingError("par rate undefined: no remaining fixed coupon")
        return _unsigned_leg_value(swap.overnight_leg, env) / annuity

    def price(self, instrument: Instrument, env: MarketEnvironment) -> PricingResult:
        """Full pricing: PV per leg, cashflow table, par rate, annuity and PVBP."""
        started = time.perf_counter()
        swap = self._check(instrument)

        rows: list[CashflowResult] = []
        leg_results: list[LegResult] = []
        floating_unsigned_pv = 0.0
        for leg in swap.legs:
            coupons = _alive_coupons(leg, env)
            discount_factors = env.discount_curve.discount_factors(
                [cf.payment_date for cf in coupons]
            )
            leg_pv = 0.0
            for coupon, discount_factor in zip(coupons, discount_factors, strict=True):
                amount = coupon.amount(env)
                signed_amount = leg.direction.sign * amount
                present_value = signed_amount * float(discount_factor)
                leg_pv += present_value
                rows.append(
                    CashflowResult(
                        leg=leg.name,
                        accrual_start=coupon.accrual_start,
                        accrual_end=coupon.accrual_end,
                        payment_date=coupon.payment_date,
                        year_fraction=coupon.year_fraction,
                        notional=coupon.notional,
                        # Fixed or compounded rate; equals amount / (N * tau) in both cases.
                        rate=amount / (coupon.notional * coupon.year_fraction),
                        amount=signed_amount,
                        discount_factor=float(discount_factor),
                        present_value=present_value,
                    )
                )
            if leg is swap.overnight_leg:
                floating_unsigned_pv = leg.direction.sign * leg_pv
            leg_results.append(LegResult(leg.name, leg.direction.value, leg_pv))

        annuity = _annuity(swap.fixed_leg, env)
        warnings: list[str] = []
        if annuity > 0.0:
            par_rate = floating_unsigned_pv / annuity
        else:
            par_rate = float("nan")
            warnings.append("par rate undefined: no remaining fixed coupon")
        present_value = sum(leg.present_value for leg in leg_results)
        elapsed_ms = (time.perf_counter() - started) * MILLISECONDS_PER_SECOND

        logger.info(
            "pricing_completed",
            extra={
                "trade_type": swap.instrument_type,
                "currency": swap.currency,
                "present_value": present_value,
                "leg_present_values": {leg.name: leg.present_value for leg in leg_results},
                "cashflow_count": len(rows),
                "par_rate": par_rate,
                "annuity": annuity,
                "duration_ms": elapsed_ms,
            },
        )
        return PricingResult(
            instrument_type=swap.instrument_type,
            currency=swap.currency,
            valuation_date=env.valuation_date,
            present_value=present_value,
            legs=tuple(leg_results),
            cashflows=tuple(rows),
            par_rate=par_rate,
            annuity=annuity,
            pvbp=annuity * BASIS_POINT,
            diagnostics=PricingDiagnostics(
                engine=type(self).__name__,
                elapsed_ms=elapsed_ms,
                core_version=rate_derivatives_pricer.__version__,
                warnings=tuple(warnings),
            ),
        )
