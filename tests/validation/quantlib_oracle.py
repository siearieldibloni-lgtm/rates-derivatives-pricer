"""QuantLib reference implementations (test oracle only, never imported by core/).

QuantLib keeps a *global* evaluation date; we pass dates explicitly (ADR-009). Every helper here
takes the valuation date and sets ``Settings.evaluationDate`` itself, so the two sides are
always aligned.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import date

import QuantLib as ql

from rate_derivatives_pricer.core.market.quotes import DepositQuote, OisSwapQuote
from rate_derivatives_pricer.core.market.snapshot import MarketSnapshot

# Same conventions as core/conventions/data/EUR.json, spelled in QuantLib terms.
EUR_SPOT_LAG = 2
EUR_PAYMENT_LAG = 1
CALENDAR = ql.TARGET()

BDC = {
    "FOLLOWING": ql.Following,
    "MODIFIED_FOLLOWING": ql.ModifiedFollowing,
    "PRECEDING": ql.Preceding,
    "UNADJUSTED": ql.Unadjusted,
}


def to_ql(d: date) -> ql.Date:
    return ql.Date(d.day, d.month, d.year)


def from_ql(d: ql.Date) -> date:
    return date(d.year(), d.month(), d.dayOfMonth())


@contextmanager
def evaluation_date(d: date) -> Iterator[None]:
    """Temporarily set QuantLib's global evaluation date."""
    settings = ql.Settings.instance()
    previous = settings.evaluationDate
    settings.evaluationDate = to_ql(d)
    try:
        yield
    finally:
        settings.evaluationDate = previous


def estr_index(
    curve: ql.YieldTermStructureHandle | None = None, fixings: Mapping[date, float] | None = None
) -> ql.OvernightIndex:
    index = ql.Estr(curve) if curve is not None else ql.Estr()
    index.clearFixings()
    if fixings:
        index.addFixings([to_ql(d) for d in fixings], list(fixings.values()), True)
    return index


def build_estr_curve(
    snapshot: MarketSnapshot, interpolation: str = "log_linear_df"
) -> ql.YieldTermStructure:
    """Bootstrap the €STR curve of ``snapshot`` with QuantLib (call within ``evaluation_date``)."""
    index = estr_index()
    helpers = []
    for quote in snapshot.quotes:
        handle = ql.QuoteHandle(ql.SimpleQuote(quote.rate))
        if isinstance(quote, DepositQuote):
            helpers.append(
                ql.DepositRateHelper(
                    handle,
                    ql.Period(quote.tenor.length, ql.Days),
                    0,
                    CALENDAR,
                    ql.ModifiedFollowing,
                    False,
                    ql.Actual360(),
                )
            )
        elif isinstance(quote, OisSwapQuote):
            helpers.append(
                ql.OISRateHelper(
                    EUR_SPOT_LAG,
                    ql.Period(str(quote.tenor)),
                    handle,
                    index,
                    paymentLag=EUR_PAYMENT_LAG,
                    paymentCalendar=CALENDAR,
                    paymentFrequency=ql.Annual,
                    convention=ql.ModifiedFollowing,
                )
            )
        else:  # pragma: no cover - test data guard
            raise TypeError(quote)
    reference = to_ql(snapshot.valuation_date)
    if interpolation == "log_linear_df":
        curve = ql.PiecewiseLogLinearDiscount(reference, helpers, ql.Actual365Fixed())
    elif interpolation == "linear_zero":
        curve = ql.PiecewiseLinearZero(reference, helpers, ql.Actual365Fixed())
    else:  # pragma: no cover - test data guard
        raise ValueError(interpolation)
    curve.enableExtrapolation()
    return curve


def make_ois(
    curve: ql.YieldTermStructure,
    fixings: Mapping[date, float],
    notional: float,
    fixed_rate: float,
    pay_fixed: bool,
    tenor: str,
    effective_date: date | None = None,
) -> ql.OvernightIndexedSwap:
    """QuantLib OIS with the EUR conventions, priced on ``curve`` (discounting and projection)."""
    handle = ql.YieldTermStructureHandle(curve)
    index = estr_index(handle, fixings)
    # QuantLib refuses both an explicit effective date and settlement days.
    kwargs: dict[str, object] = (
        {"effectiveDate": to_ql(effective_date)}
        if effective_date is not None
        else {"settlementDays": EUR_SPOT_LAG}
    )
    swap = ql.MakeOIS(
        ql.Period(tenor),
        index,
        fixed_rate,
        paymentLag=EUR_PAYMENT_LAG,
        paymentCalendar=CALENDAR,
        convention=ql.ModifiedFollowing,
        nominal=notional,
        swapType=ql.Swap.Payer if pay_fixed else ql.Swap.Receiver,
        discountingTermStructure=handle,
        **kwargs,
    )
    return swap
