"""Grid of validation markets derived from a reference snapshot.

Each market shifts every quote of the reference snapshot (``EUR_2026-10-19`` by default,
``USD_2026-10-19`` for SOFR) in parallel and moves the valuation date; fixings after the new
valuation date are dropped. Shifts cover positive, flat and negative rate regimes; valuation
dates cover a Monday, a month end and a pre-holiday day (Easter in both calendars).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from rate_derivatives_pricer.core.market.providers import FileMarketDataProvider
from rate_derivatives_pricer.core.market.snapshot import MarketSnapshot

MARKET_DIR = Path(__file__).resolve().parents[2] / "data" / "market"
REFERENCE_SNAPSHOT = "EUR_2026-10-19"
USD_REFERENCE_SNAPSHOT = "USD_2026-10-19"

PARALLEL_SHIFTS_BP = (0.0, -250.0, 150.0)
VALUATION_DATES = (date(2026, 10, 19), date(2026, 9, 30), date(2026, 4, 2))
BASIS_POINT = 1e-4


def reference_snapshot(name: str = REFERENCE_SNAPSHOT) -> MarketSnapshot:
    return FileMarketDataProvider(MARKET_DIR).load_snapshot(name)


def shifted_snapshot(
    shift_bp: float, valuation_date: date, reference: str = REFERENCE_SNAPSHOT
) -> MarketSnapshot:
    base = reference_snapshot(reference)
    prefix = "grid" if base.currency == "EUR" else f"grid_{base.currency}"
    return MarketSnapshot(
        name=f"{prefix}_{valuation_date.isoformat()}_{shift_bp:+.0f}bp",
        currency=base.currency,
        valuation_date=valuation_date,
        quotes=tuple(q.with_rate(q.rate + shift_bp * BASIS_POINT) for q in base.quotes),
        fixings=tuple(f for f in base.fixings if f.fixing_date <= valuation_date),
    )


def market_grid(reference: str = REFERENCE_SNAPSHOT) -> list[MarketSnapshot]:
    return [shifted_snapshot(s, d, reference) for d in VALUATION_DATES for s in PARALLEL_SHIFTS_BP]
