"""Market quotes of calibration instruments, with an explicit unit (percent, decimal, bp)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import ClassVar

from rate_derivatives_pricer.core.conventions.tenor import Tenor
from rate_derivatives_pricer.core.errors import MarketDataError

PERCENT_TO_DECIMAL = 1e-2
BASIS_POINT_TO_DECIMAL = 1e-4
# Label of the curve calibrated by deposit and OIS quotes (the currency's overnight curve).
OVERNIGHT_CURVE_LABEL = "OIS"


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
    def curve_label(self) -> str:
        """Curve calibrated by this quote: ``"OIS"`` (overnight curve) or an Ibor index name."""
        return OVERNIGHT_CURVE_LABEL

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


@dataclass(frozen=True, slots=True)
class IborQuote(Quote):
    """Quote of an instrument that calibrates the projection curve of an Ibor index.

    Several Ibor curves (Euribor 3M and 6M) can live in one snapshot, so the index is part of the
    quote's identity.

    Attributes:
        index_name: Index whose projection curve the quote calibrates (``"EURIBOR6M"``).
    """

    index_name: str = field(kw_only=True)

    def __post_init__(self) -> None:
        # Explicit base call: zero-argument super() does not work in slotted dataclasses.
        Quote.__post_init__(self)
        name = self.index_name.strip().upper()
        if not name:
            raise MarketDataError(
                "Ibor quote needs an index name",
                {"instrument_type": self.instrument_type, "tenor": self.tenor_label},
            )
        object.__setattr__(self, "index_name", name)

    @property
    def curve_label(self) -> str:
        """The projection curve of the index."""
        return self.index_name

    @property
    def key(self) -> tuple[str, str]:
        """Identity of the quoted instrument: ``(instrument_type, "<INDEX>/<TENOR>")``."""
        return (self.instrument_type, f"{self.index_name}/{self.tenor_label.upper()}")


@dataclass(frozen=True, slots=True)
class IborDepositQuote(IborQuote):
    """Deposit at the index rate: spot-starting and lasting the tenor of the index."""

    instrument_type: ClassVar[str] = "ibor_deposit"


@dataclass(frozen=True, slots=True)
class IborSwapQuote(IborQuote):
    """Par fixed rate of a spot-starting vanilla IRS (fixed leg vs the Ibor index)."""

    instrument_type: ClassVar[str] = "ibor_swap"


#: Quote classes by instrument type, used to parse snapshots.
QUOTE_TYPES: dict[str, type[Quote]] = {
    cls.instrument_type: cls
    for cls in (DepositQuote, OisSwapQuote, IborDepositQuote, IborSwapQuote)
}
