"""Market snapshot: valuation date, quotes and historical fixings (the unit of risk bumps)."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from rate_derivatives_pricer.core.errors import MarketDataError
from rate_derivatives_pricer.core.market.quotes import Quote


@dataclass(frozen=True, slots=True)
class Fixing:
    """Published value of an index on a fixing date.

    Attributes:
        index: Index name (``"ESTR"``).
        fixing_date: Date of the fixing (for €STR, the start of the overnight period).
        rate: Fixing as a decimal rate.
    """

    index: str
    fixing_date: date
    rate: float


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """Immutable set of market data as of a valuation date.

    The valuation date is explicit (no global evaluation date, ADR-009). Risk bumps produce new
    snapshots and replay the pipeline.

    Attributes:
        name: Identifier (``"EUR_2026-10-19"``).
        currency: ISO 4217 code selecting the market conventions.
        valuation_date: Valuation (curve reference) date.
        quotes: Calibration quotes, one per instrument.
        fixings: Historical index fixings, all on or before the valuation date.
        description: Free text (source of the data, e.g. "synthetic").
    """

    name: str
    currency: str
    valuation_date: date
    quotes: tuple[Quote, ...]
    fixings: tuple[Fixing, ...] = ()
    description: str = ""

    def __post_init__(self) -> None:
        if not self.quotes:
            raise MarketDataError("snapshot has no quotes", {"snapshot": self.name})
        seen: set[tuple[str, str]] = set()
        for quote in self.quotes:
            if quote.key in seen:
                raise MarketDataError(
                    "duplicate quote in snapshot",
                    {"snapshot": self.name, "instrument_type": quote.key[0], "tenor": quote.key[1]},
                )
            seen.add(quote.key)
        fixing_keys: set[tuple[str, date]] = set()
        for fixing in self.fixings:
            details = {
                "snapshot": self.name,
                "index": fixing.index,
                "date": fixing.fixing_date.isoformat(),
            }
            if fixing.fixing_date > self.valuation_date:
                raise MarketDataError("fixing dated after the valuation date", details)
            if not math.isfinite(fixing.rate):
                raise MarketDataError("fixing rate must be finite", details)
            if (fixing.index, fixing.fixing_date) in fixing_keys:
                raise MarketDataError("duplicate fixing", details)
            fixing_keys.add((fixing.index, fixing.fixing_date))

    def fixings_by_index(self) -> Mapping[str, Mapping[date, float]]:
        """Fixings grouped by index: ``{"ESTR": {date: rate}}``."""
        grouped: dict[str, dict[date, float]] = {}
        for fixing in self.fixings:
            grouped.setdefault(fixing.index, {})[fixing.fixing_date] = fixing.rate
        return grouped

    def with_quotes(self, quotes: tuple[Quote, ...]) -> MarketSnapshot:
        """Copy of this snapshot with other quotes (used by risk bumps)."""
        return MarketSnapshot(
            name=self.name,
            currency=self.currency,
            valuation_date=self.valuation_date,
            quotes=quotes,
            fixings=self.fixings,
            description=self.description,
        )
