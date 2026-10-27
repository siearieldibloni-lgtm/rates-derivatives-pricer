"""Pricing engine interface and engine lookup by registry (engines are stateless, ADR-007)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import ClassVar

from rate_derivatives_pricer.core.engines.results import PricingResult
from rate_derivatives_pricer.core.errors import UnsupportedInstrumentError
from rate_derivatives_pricer.core.instruments.swaps import Instrument
from rate_derivatives_pricer.core.market.environment import MarketEnvironment
from rate_derivatives_pricer.core.registry import ENGINES

DEFAULT_PRICING_METHOD = "discounting"


class PricingEngine(ABC):
    """Prices instruments of the supported types from a market environment."""

    #: Pricing method identifier (second part of the registry key).
    method: ClassVar[str]
    #: Instrument types this engine prices.
    supported_instruments: ClassVar[tuple[str, ...]]

    def supports(self, instrument: Instrument) -> bool:
        """Whether this engine prices ``instrument``."""
        return instrument.instrument_type in self.supported_instruments

    @abstractmethod
    def price(self, instrument: Instrument, env: MarketEnvironment) -> PricingResult:
        """Price ``instrument``.

        Raises:
            UnsupportedInstrumentError: If the instrument type is not supported.
        """

    def present_value(self, instrument: Instrument, env: MarketEnvironment) -> float:
        """PV of ``instrument`` alone, without the result tables (repeated valuations, risk).

        Engines may override this with a lighter path; it must agree with
        ``price(...).present_value`` up to rounding.
        """
        return self.price(instrument, env).present_value

    def cashflows_paid(
        self, instrument: Instrument, env: MarketEnvironment, start: date, end: date
    ) -> float:
        """Signed undiscounted amounts paid in ``(start, end]`` (carry/roll-down, ADR-034).

        Raises:
            UnsupportedInstrumentError: If the engine does not support carry/roll-down.
        """
        raise UnsupportedInstrumentError(
            f"{type(self).__name__} does not support carry/roll-down",
            {"instrument_type": instrument.instrument_type},
        )

    def accrued_interest(
        self, instrument: Instrument, env: MarketEnvironment, start: date, end: date
    ) -> float:
        """Signed net interest accrued over ``[start, end)`` (carry/roll-down, ADR-034).

        Raises:
            UnsupportedInstrumentError: If the engine does not support carry/roll-down.
        """
        raise UnsupportedInstrumentError(
            f"{type(self).__name__} does not support carry/roll-down",
            {"instrument_type": instrument.instrument_type},
        )


def engine_for(instrument_type: str, method: str = DEFAULT_PRICING_METHOD) -> PricingEngine:
    """Instantiate the engine registered for ``(instrument_type, method)``.

    Raises:
        UnsupportedInstrumentError: If no engine is registered for this pair.
    """
    engine_class = ENGINES.get((instrument_type, method))
    engine = engine_class()
    if instrument_type not in engine.supported_instruments:  # pragma: no cover - wiring bug
        raise UnsupportedInstrumentError(
            "engine registered for an instrument type it does not support",
            {"instrument_type": instrument_type, "method": method},
        )
    return engine
