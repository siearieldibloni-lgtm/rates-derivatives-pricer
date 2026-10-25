"""Swap legs: a list of cashflows, a notional and a pay/receive direction."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from rate_derivatives_pricer.core.instruments.cashflows import Cashflow


class PayReceive(StrEnum):
    """Direction of a leg from the trade holder's point of view."""

    PAY = "pay"
    RECEIVE = "receive"

    @property
    def sign(self) -> float:
        """``+1`` for a received leg, ``-1`` for a paid leg."""
        return 1.0 if self is PayReceive.RECEIVE else -1.0


@dataclass(frozen=True, slots=True)
class Leg:
    """One leg of a swap (composition of cashflows).

    Attributes:
        name: Leg identifier in results (``"fixed"``, ``"overnight"``).
        cashflows: Cashflows by increasing payment date; amounts are unsigned.
        notional: Leg notional (positive).
        direction: Whether the holder pays or receives the cashflows.
    """

    name: str
    cashflows: tuple[Cashflow, ...]
    notional: float
    direction: PayReceive
