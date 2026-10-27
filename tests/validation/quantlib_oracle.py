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

from rate_derivatives_pricer.core.market.quotes import (
    DepositQuote,
    IborDepositQuote,
    IborQuote,
    OisSwapQuote,
)
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
        elif isinstance(quote, IborQuote):
            continue  # Ibor quotes calibrate the Euribor curves (build_euribor_curve)
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


# --------------------------------------------------------------------------- Euribor / IRS
# Same conventions as the "ibor" section of core/conventions/data/EUR.json (ADR-027).
EURIBOR_INDICES = {"EURIBOR6M": ql.Euribor6M, "EURIBOR3M": ql.Euribor3M}
FIXED_DAY_COUNT = ql.Thirty360(ql.Thirty360.BondBasis)


def euribor_index(
    name: str,
    curve: ql.YieldTermStructureHandle | None = None,
    fixings: Mapping[date, float] | None = None,
) -> ql.IborIndex:
    """Euribor index forwarding on ``curve`` with the given fixings (none by default).

    QuantLib stores fixings globally by index name, so they are cleared first: the curve
    bootstrap must not see the published fixing of the valuation date (ADR-027).
    """
    index = EURIBOR_INDICES[name](curve) if curve is not None else EURIBOR_INDICES[name]()
    index.clearFixings()
    if fixings:
        index.addFixings([to_ql(d) for d in fixings], list(fixings.values()), True)
    return index


def build_euribor_curve(
    snapshot: MarketSnapshot,
    index_name: str,
    discount_curve: ql.YieldTermStructure,
    interpolation: str = "log_linear_df",
) -> ql.YieldTermStructure:
    """Bootstrap one Euribor projection curve discounted on ``discount_curve`` (QuantLib)."""
    discount_handle = ql.YieldTermStructureHandle(discount_curve)
    index = euribor_index(index_name)
    helpers = []
    for quote in snapshot.quotes:
        if not isinstance(quote, IborQuote) or quote.index_name != index_name:
            continue
        handle = ql.QuoteHandle(ql.SimpleQuote(quote.rate))
        if isinstance(quote, IborDepositQuote):
            helpers.append(
                ql.DepositRateHelper(
                    handle,
                    ql.Period(str(quote.tenor)),
                    index.fixingDays(),
                    CALENDAR,
                    ql.ModifiedFollowing,
                    True,
                    ql.Actual360(),
                )
            )
        else:
            helpers.append(
                ql.SwapRateHelper(
                    handle,
                    ql.Period(str(quote.tenor)),
                    CALENDAR,
                    ql.Annual,
                    ql.ModifiedFollowing,
                    FIXED_DAY_COUNT,
                    index,
                    ql.QuoteHandle(),
                    ql.Period(0, ql.Days),
                    discount_handle,
                )
            )
    reference = to_ql(snapshot.valuation_date)
    if interpolation == "log_linear_df":
        curve = ql.PiecewiseLogLinearDiscount(reference, helpers, ql.Actual365Fixed())
    elif interpolation == "linear_zero":
        curve = ql.PiecewiseLinearZero(reference, helpers, ql.Actual365Fixed())
    else:  # pragma: no cover - test data guard
        raise ValueError(interpolation)
    curve.enableExtrapolation()
    return curve


def make_irs(
    projection_curve: ql.YieldTermStructure,
    discount_curve: ql.YieldTermStructure,
    fixings: Mapping[date, float],
    index_name: str,
    notional: float,
    fixed_rate: float,
    pay_fixed: bool,
    tenor: str,
    effective_date: date | None = None,
) -> ql.VanillaSwap:
    """QuantLib vanilla IRS with the EUR conventions: projected on one curve, discounted on OIS."""
    index = euribor_index(index_name, ql.YieldTermStructureHandle(projection_curve), fixings)
    kwargs: dict[str, object] = (
        {"effectiveDate": to_ql(effective_date)} if effective_date is not None else {}
    )
    return ql.MakeVanillaSwap(
        ql.Period(tenor),
        index,
        fixed_rate,
        ql.Period(0, ql.Days),
        nominal=notional,
        swapType=ql.Swap.Payer if pay_fixed else ql.Swap.Receiver,
        fixedLegTenor=ql.Period(1, ql.Years),
        fixedLegDayCount=FIXED_DAY_COUNT,
        fixedLegConvention=ql.ModifiedFollowing,
        fixedLegTerminationDateConvention=ql.ModifiedFollowing,
        floatingLegConvention=ql.ModifiedFollowing,
        floatingLegTerminationDateConvention=ql.ModifiedFollowing,
        discountingTermStructure=ql.YieldTermStructureHandle(discount_curve),
        **kwargs,
    )


def freeze_log_linear(curve: ql.YieldTermStructure) -> ql.YieldTermStructure:
    """Non-reactive copy of a bootstrapped curve (log-linear on discount factors).

    QuantLib stores index fixings globally and re-bootstraps curves when they change; pricing a
    swap with today's fixing loaded would silently re-calibrate the Euribor curve on it. Freezing
    the nodes after the (fixing-free) bootstrap keeps the oracle on the convention of ADR-027.
    """
    nodes = curve.nodes()
    frozen = ql.DiscountCurve([d for d, _ in nodes], [df for _, df in nodes], ql.Actual365Fixed())
    frozen.enableExtrapolation()
    return frozen
