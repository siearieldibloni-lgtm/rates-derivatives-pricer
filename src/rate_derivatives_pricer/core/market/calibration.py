"""Calibration instruments: the quote implied by a candidate curve (QuantLib's ``RateHelper``).

The bootstrap solves, pillar by pillar, ``implied_quote(curve) = market quote``. Each instrument
names the curve node it determines (its *pillar*): the latest date its value depends on.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from datetime import date

from rate_derivatives_pricer.core.conventions.business_day import BusinessDayConvention
from rate_derivatives_pricer.core.conventions.currency import CurrencyConventions
from rate_derivatives_pricer.core.conventions.day_count import DayCountConvention
from rate_derivatives_pricer.core.engines.discounting import DiscountingSwapEngine
from rate_derivatives_pricer.core.errors import MarketDataError
from rate_derivatives_pricer.core.instruments.factories import (
    build_spot_ois_swap,
    build_spot_vanilla_irs,
)
from rate_derivatives_pricer.core.instruments.swaps import OisSwap, SwapDirection, VanillaIrs
from rate_derivatives_pricer.core.market.environment import MarketEnvironment
from rate_derivatives_pricer.core.market.indices import IborIndex
from rate_derivatives_pricer.core.market.quotes import (
    DepositQuote,
    IborDepositQuote,
    IborSwapQuote,
    OisSwapQuote,
    Quote,
)

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
    def with_quote(self, quote: Quote) -> CalibrationInstrument:
        """This instrument matching another quote of the same instrument (a bumped rate).

        The dates and schedules depend on the instrument's identity, never on the quoted rate, so
        they are shared with the original; only the matched quote changes.
        """

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

    def with_quote(self, quote: Quote) -> DepositCalibration:
        """Same instrument matching another quote of the same instrument (a bumped rate)."""
        return replace(self, deposit_quote=quote)

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
    _pillar: date = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # Computed once: the pillar is read many times per bootstrap (sorting, memo keys).
        last_payments = (leg.cashflows[-1].payment_date for leg in self.swap.legs)
        object.__setattr__(self, "_pillar", max(self.swap.end_date, *last_payments))

    @property
    def quote(self) -> Quote:
        """Market quote."""
        return self.swap_quote

    def with_quote(self, quote: Quote) -> OisCalibration:
        """Same instrument matching another quote of the same instrument (a bumped rate)."""
        return replace(self, swap_quote=quote)

    @property
    def pillar_date(self) -> date:
        """Latest payment date of either leg (or maturity if later)."""
        return self._pillar

    def implied_quote(self, env: MarketEnvironment) -> float:
        """Par rate of the swap."""
        return DiscountingSwapEngine().par_rate(self.swap, env)


@dataclass(frozen=True, slots=True)
class IborDepositCalibration(CalibrationInstrument):
    """Deposit at the Ibor index rate: the forward over ``[spot, spot + tenor]`` of the index.

    Implied quote = simple forward rate of the *projection* curve of the index. It pins the
    first node of that curve (Euribor 6M deposit for the 6M curve).

    Attributes:
        deposit_quote: Quoted deposit rate.
        index: Ibor index of the deposit.
        start_date: Value date of the deposit (spot).
        end_date: Maturity of the deposit (the pillar).
    """

    deposit_quote: Quote
    index: IborIndex
    start_date: date
    end_date: date

    @property
    def quote(self) -> Quote:
        """Market quote."""
        return self.deposit_quote

    def with_quote(self, quote: Quote) -> IborDepositCalibration:
        """Same instrument matching another quote of the same instrument (a bumped rate)."""
        return replace(self, deposit_quote=quote)

    @property
    def pillar_date(self) -> date:
        """Deposit maturity."""
        return self.end_date

    def implied_quote(self, env: MarketEnvironment) -> float:
        """Simple forward rate between start and end on the index projection curve."""
        return env.projection_curve(self.index.name).forward_rate(
            self.start_date, self.end_date, self.index.day_count
        )


@dataclass(frozen=True, slots=True)
class IborSwapCalibration(CalibrationInstrument):
    """Spot-starting vanilla IRS: implied quote = par fixed rate.

    The fixed leg is discounted on the (already calibrated) OIS curve and the floating leg is
    projected on the candidate Ibor curve. The pillar is the last payment date of the swap, the
    latest date its par rate depends on (as for OIS).

    Attributes:
        swap_quote: Quoted par rate.
        swap: Unit-notional swap with the quote's tenor and the currency's swap conventions.
    """

    swap_quote: Quote
    swap: VanillaIrs
    _pillar: date = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # Computed once: the pillar is read many times per bootstrap (sorting, memo keys).
        object.__setattr__(
            self,
            "_pillar",
            max(cf.payment_date for leg in self.swap.legs for cf in leg.cashflows),
        )

    @property
    def quote(self) -> Quote:
        """Market quote."""
        return self.swap_quote

    def with_quote(self, quote: Quote) -> IborSwapCalibration:
        """Same instrument matching another quote of the same instrument (a bumped rate)."""
        return replace(self, swap_quote=quote)

    @property
    def pillar_date(self) -> date:
        """Latest payment date of either leg (the latest date the par rate depends on)."""
        return self._pillar

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
    if isinstance(quote, IborDepositQuote):
        index = conventions.ibor_index(quote.index_name)
        if quote.tenor != index.tenor:
            raise MarketDataError(
                "Ibor deposit tenor must equal the tenor of its index",
                {"index": index.name, "tenor": quote.tenor_label, "index_tenor": str(index.tenor)},
            )
        deposit = conventions.require_ibor_deposit()
        trade_date = calendar.adjust(valuation_date, BusinessDayConvention.FOLLOWING)
        start = calendar.advance_business_days(trade_date, deposit.settlement_days)
        end = calendar.advance(
            start, quote.tenor, deposit.business_day_convention, deposit.end_of_month
        )
        return IborDepositCalibration(quote, index, start, end)
    if isinstance(quote, IborSwapQuote):
        irs = build_spot_vanilla_irs(
            conventions,
            conventions.ibor_index(quote.index_name),
            valuation_date,
            quote.tenor,
            notional=CALIBRATION_NOTIONAL,
            fixed_rate=quote.rate,
            direction=SwapDirection.RECEIVE_FIXED,
        )
        return IborSwapCalibration(quote, irs)
    raise MarketDataError(
        "no calibration instrument for quote type",
        {"instrument_type": quote.instrument_type, "tenor": quote.tenor_label},
    )
