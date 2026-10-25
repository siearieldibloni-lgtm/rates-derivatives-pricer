"""Market quotes of calibration instruments, with an explicit unit (percent, decimal, bp)."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import ClassVar

from rate_derivatives_pricer.core.conventions.tenor import Tenor
from rate_derivatives_pricer.core.errors import MarketDataError

PERCENT_TO_DECIMAL = 1e-2
BASIS_POINT_TO_DECIMAL = 1e-4


class QuoteUnit(StrEnum):
    """Unit in which a quote value is expressed (a classic source of production bugs)."""

    PERCENT = "percent"
    DECIMAL = "decimal"
    BP = "bp"

    @property
    def to_decimal(self) -> float:
        """Multiplier converting a value in this unit to a decimal rate."""
        match self:
            case QuoteUnit.PERCENT:
                return PERCENT_TO_DECIMAL
            case QuoteUnit.BP:
                return BASIS_POINT_TO_DECIMAL
            case QuoteUnit.DECIMAL:
                return 1.0


@dataclass(frozen=True, slots=True)
class Quote:
    """Market quote of one calibration instrument.

    Attributes:
        tenor_label: Market tenor label as quoted (``"ON"``, ``"1W"``, ``"10Y"``); also the
            pillar label in calibration reports.
        value: Quoted value, expressed in ``unit``.
        unit: Unit of ``value``.
    """

    #: Stable instrument type identifier (``"deposit"``, ``"ois_swap"``).
    instrument_type: ClassVar[str] = "quote"

    tenor_label: str
    value: float
    unit: QuoteUnit = QuoteUnit.DECIMAL

    def __post_init__(self) -> None:
        if not math.isfinite(self.value):
            raise MarketDataError(
                "quote value must be finite",
                {"instrument_type": self.instrument_type, "tenor": self.tenor_label},
            )
        # Validate the label eagerly: a malformed tenor must fail at load time, not mid-bootstrap.
        Tenor.parse(self.tenor_label)

    @property
    def tenor(self) -> Tenor:
        """Parsed tenor (``"ON"`` is one business day)."""
        return Tenor.parse(self.tenor_label)

    @property
    def rate(self) -> float:
        """Quote value as a decimal rate (0.025 = 2.5%)."""
        return self.value * self.unit.to_decimal

    @property
    def key(self) -> tuple[str, str]:
        """Identity of the quoted instrument: ``(instrument_type, tenor_label)``."""
        return (self.instrument_type, self.tenor_label.upper())

    def with_rate(self, rate: float) -> Quote:
        """Copy of this quote holding ``rate`` (decimal), keeping the original unit."""
        return replace(self, value=rate / self.unit.to_decimal)


@dataclass(frozen=True, slots=True)
class DepositQuote(Quote):
    """Cash deposit rate (simple interest), e.g. the €STR overnight deposit."""

    instrument_type: ClassVar[str] = "deposit"


@dataclass(frozen=True, slots=True)
class OisSwapQuote(Quote):
    """Par fixed rate of a spot-starting OIS on the currency's overnight index."""

    instrument_type: ClassVar[str] = "ois_swap"


#: Quote classes by instrument type, used to parse snapshots.
QUOTE_TYPES: dict[str, type[Quote]] = {
    cls.instrument_type: cls for cls in (DepositQuote, OisSwapQuote)
}
