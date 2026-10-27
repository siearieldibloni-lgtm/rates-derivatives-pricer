"""Bumps of market quotes: the shocks of the bump-and-reprice risk approach (ADR-010).

Risk is computed on the *calibration instruments*: a bump shifts the par rates (or deposit
rates) quoted in a snapshot, and the caller re-bootstraps the curves and re-prices. A bump never
mutates anything: it returns a new :class:`MarketSnapshot`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from rate_derivatives_pricer.core.errors import RiskComputationError
from rate_derivatives_pricer.core.market.quotes import Quote
from rate_derivatives_pricer.core.market.snapshot import MarketSnapshot

BASIS_POINT = 1e-4


class BumpMode(StrEnum):
    """How a sensitivity is estimated from bumped valuations."""

    #: Forward difference: reprice with ``+h`` only (``(PV(+h) - PV) / h``).
    UP = "up"
    #: Backward difference: reprice with ``-h`` only (``(PV - PV(-h)) / h``).
    DOWN = "down"
    #: Central difference: reprice with ``+h`` and ``-h`` (second-order accurate, the default).
    CENTRAL = "central"


@dataclass(frozen=True, slots=True)
class BumpTarget:
    """Selects the quotes of a snapshot that a bump shifts.

    A quote is selected when it satisfies *every* criterion that is set; ``None`` means "do not
    filter on this criterion". The default target selects every quote (parallel bump).

    Attributes:
        curves: Curve labels (``"OIS"``, ``"EURIBOR6M"``, see :attr:`Quote.curve_label`).
        instrument_types: Quote instrument types (``"ois_swap"``).
        quote_keys: Exact quote identities (:attr:`Quote.key`), used for one bucket.
    """

    curves: frozenset[str] | None = None
    instrument_types: frozenset[str] | None = None
    quote_keys: frozenset[tuple[str, str]] | None = None

    def matches(self, quote: Quote) -> bool:
        """Whether ``quote`` is selected by this target."""
        return (
            (self.curves is None or quote.curve_label in self.curves)
            and (self.instrument_types is None or quote.instrument_type in self.instrument_types)
            and (self.quote_keys is None or quote.key in self.quote_keys)
        )

    def describe(self) -> str:
        """Short text for logs and error messages."""
        parts = [
            f"{name}={sorted(value)}"
            for name, value in (
                ("curves", self.curves),
                ("instrument_types", self.instrument_types),
                ("quote_keys", self.quote_keys),
            )
            if value is not None
        ]
        return ", ".join(parts) or "all quotes"


@dataclass(frozen=True, slots=True)
class BumpSpec:
    """Description of a shock: which quotes, how large, and how it is used.

    Attributes:
        target: Quotes to shift.
        size_bp: Bump size ``h`` in basis points (> 0).
        mode: Finite difference scheme, which fixes the signs of the shifts (:attr:`shifts_bp`).

    Raises:
        RiskComputationError: If ``size_bp`` is not a positive finite number.
    """

    target: BumpTarget
    size_bp: float
    mode: BumpMode = BumpMode.CENTRAL

    def __post_init__(self) -> None:
        if not (self.size_bp > 0.0 and math.isfinite(self.size_bp)):
            raise RiskComputationError(
                "bump size must be a positive number of basis points", {"size_bp": self.size_bp}
            )

    @property
    def shifts_bp(self) -> tuple[float, ...]:
        """Signed shifts, in basis points, to reprice with (one or two depending on the mode)."""
        match self.mode:
            case BumpMode.UP:
                return (self.size_bp,)
            case BumpMode.DOWN:
                return (-self.size_bp,)
            case BumpMode.CENTRAL:
                return (self.size_bp, -self.size_bp)


class Bumper:
    """Applies a shock to a snapshot and returns a new snapshot (stateless)."""

    def apply(
        self, snapshot: MarketSnapshot, target: BumpTarget, shift_bp: float
    ) -> MarketSnapshot:
        """Shift the selected quotes by ``shift_bp`` basis points (added to the decimal rate).

        Args:
            snapshot: Market data to shock (left untouched).
            target: Quotes to shift.
            shift_bp: Signed shift in basis points.

        Raises:
            RiskComputationError: If no quote of the snapshot is selected (a silent no-op bump
                would hide a wrong target).
        """
        shift = shift_bp * BASIS_POINT
        selected = 0
        quotes: list[Quote] = []
        for quote in snapshot.quotes:
            if target.matches(quote):
                quotes.append(quote.with_rate(quote.rate + shift))
                selected += 1
            else:
                quotes.append(quote)
        if selected == 0:
            raise RiskComputationError(
                "bump target selects no quote",
                {"snapshot": snapshot.name, "target": target.describe()},
            )
        return snapshot.with_quotes(tuple(quotes))
