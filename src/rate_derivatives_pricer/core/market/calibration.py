"""Calibration instruments: the quote implied by a candidate curve (QuantLib's ``RateHelper``).

The bootstrap solves, pillar by pillar, ``implied_quote(curve) = market quote``. Each instrument
names the curve node it determines (its *pillar*): the latest date its value depends on.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date

from rate_derivatives_pricer.core.conventions.business_day import BusinessDayConvention
from rate_derivatives_pricer.core.conventions.currency import CurrencyConventions
from rate_derivatives_pricer.core.conventions.day_count import DayCountConvention
from rate_derivatives_pricer.core.engines.discounting import DiscountingSwapEngine
from rate_derivatives_pricer.core.errors import MarketDataError
from rate_derivatives_pricer.core.instruments.factories import build_spot_ois_swap
from rate_derivatives_pricer.core.instruments.swaps import OisSwap, SwapDirection
from rate_derivatives_pricer.core.market.environment import MarketEnvironment
from rate_derivatives_pricer.core.market.quotes import DepositQuote, OisSwapQuote, Quote

# Unit notional of calibration swaps: the par rate does not depend on the notional.
CALIBRATION_NOTIONAL = 1.0


class CalibrationInstrument(ABC):
    """Instrument whose market quote pins one curve node."""

    @property
    @abstractmethod
    def quote(self) -> Quote:
        """Market quote being matched."""

    @property
    @abstractmethod
    def pillar_date(self) -> date:
        """Curve node determined by this instrument (latest date its value depends on)."""

    @abstractmethod
    def implied_quote(self, env: MarketEnvironment) -> float:
        """Quote (decimal rate) implied by the curves of ``env``."""

    @property
    def label(self) -> str:
        """Pillar label (the quote's tenor label)."""
        return self.quote.tenor_label


@dataclass(frozen=True, slots=True)
class DepositCalibration(CalibrationInstrument):
    """Cash deposit: simple rate ``(P(start) / P(end) - 1) / tau(start, end)``.

    Attributes:
        deposit_quote: Quoted deposit rate.
        start_date: Value date of the deposit.
        end_date: Maturity of the deposit (the pillar).
        day_count: Accrual convention of the deposit rate.
    """

    deposit_quote: Quote
    start_date: date
    end_date: date
    day_count: DayCountConvention

    @property
    def quote(self) -> Quote:
        """Market quote."""
        return self.deposit_quote

    @property
    def pillar_date(self) -> date:
        """Deposit maturity."""
        return self.end_date

    def implied_quote(self, env: MarketEnvironment) -> float:
        """Simple forward rate between start and end on the discount curve."""
        return env.discount_curve.forward_rate(self.start_date, self.end_date, self.day_count)


@dataclass(frozen=True, slots=True)
class OisCalibration(CalibrationInstrument):
    """Spot-starting OIS: implied quote = par fixed rate.

    The pillar is the last payment date (accrual end plus payment lag), the latest discount
    factor the par rate depends on (QuantLib ``Pillar::LastRelevantDate``).

    Attributes:
        swap_quote: Quoted par rate.
        swap: Unit-notional swap with the quote's tenor and the currency's OIS conventions.
    """

    swap_quote: Quote
    swap: OisSwap

    @property
    def quote(self) -> Quote:
        """Market quote."""
        return self.swap_quote

    @property
    def pillar_date(self) -> date:
        """Latest payment date of either leg (or maturity if later)."""
        last_payments = (leg.cashflows[-1].payment_date for leg in self.swap.legs)
        return max(self.swap.end_date, *last_payments)

    def implied_quote(self, env: MarketEnvironment) -> float:
        """Par rate of the swap."""
        return DiscountingSwapEngine().par_rate(self.swap, env)


def calibration_instrument_for(
    quote: Quote, conventions: CurrencyConventions, valuation_date: date
) -> CalibrationInstrument:
    """Build the calibration instrument behind ``quote`` with the currency's conventions.

    Raises:
        MarketDataError: If the quote type has no calibration instrument.
    """
    calendar = conventions.calendar
    if isinstance(quote, DepositQuote):
        deposit = conventions.deposit
        trade_date = calendar.adjust(valuation_date, BusinessDayConvention.FOLLOWING)
        start = calendar.advance_business_days(trade_date, deposit.settlement_days)
        end = calendar.advance(
            start, quote.tenor, deposit.business_day_convention, deposit.end_of_month
        )
        return DepositCalibration(quote, start, end, deposit.day_count)
    if isinstance(quote, OisSwapQuote):
        swap = build_spot_ois_swap(
            conventions,
            valuation_date,
            quote.tenor,
            notional=CALIBRATION_NOTIONAL,
            fixed_rate=quote.rate,
            direction=SwapDirection.RECEIVE_FIXED,
        )
        return OisCalibration(quote, swap)
    raise MarketDataError(
        "no calibration instrument for quote type",
        {"instrument_type": quote.instrument_type, "tenor": quote.tenor_label},
    )
