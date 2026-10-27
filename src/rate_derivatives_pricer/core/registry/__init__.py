"""Registries of the quant core: key -> component, filled by decorators (Open/Closed principle).

| Registry          | Key                                   | Value                          |
|-------------------|---------------------------------------|--------------------------------|
| ``CALENDARS``     | ``"TARGET"``                          | :class:`Calendar` subclass     |
| ``DAY_COUNTS``    | ``"ACT/360"``                         | :class:`DayCountConvention` subclass |
| ``INTERPOLATORS`` | ``"log_linear_df"``                   | :class:`Interpolator` subclass |
| ``INSTRUMENTS``   | ``"ois_swap"``                        | instrument factory             |
| ``ENGINES``       | ``("ois_swap", "discounting")``       | :class:`PricingEngine` subclass |
| ``RISK_MEASURES`` | ``"key_rate_dv01"``                   | :class:`RiskMeasure` subclass  |
| ``CONVENTIONS``   | ``"EUR"``                             | :class:`CurrencyConventions`   |
| ``SCENARIOS``     | ``"steepener"``                       | :class:`Scenario` (preset)     |

Built-in components are imported lazily by :func:`load_builtin_components` on the first lookup.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from threading import RLock
from typing import TYPE_CHECKING, Any

from rate_derivatives_pricer.core.errors import (
    ConventionError,
    UnsupportedInstrumentError,
    UnsupportedRiskMeasureError,
    UnsupportedScenarioError,
)
from rate_derivatives_pricer.core.registry.base import Registry

if TYPE_CHECKING:
    from rate_derivatives_pricer.core.conventions.calendar import Calendar
    from rate_derivatives_pricer.core.conventions.currency import CurrencyConventions
    from rate_derivatives_pricer.core.conventions.day_count import DayCountConvention
    from rate_derivatives_pricer.core.engines.base import PricingEngine
    from rate_derivatives_pricer.core.instruments.swaps import Instrument
    from rate_derivatives_pricer.core.market.interpolation import Interpolator
    from rate_derivatives_pricer.core.risk.measures import RiskMeasure
    from rate_derivatives_pricer.core.risk.scenarios import Scenario


_builtins_lock = RLock()
_builtins_loaded = False


def load_builtin_components() -> None:
    """Import every module that registers a built-in component (idempotent, shared by all).

    Re-entrant: loading conventions looks calendars up, which calls this function again; the
    shared flag makes that nested call a no-op.
    """
    global _builtins_loaded
    with _builtins_lock:
        if _builtins_loaded:
            return
        _builtins_loaded = True
        try:
            # Local imports: these modules import this package to register themselves.
            from rate_derivatives_pricer.core.conventions import calendar, currency, day_count
            from rate_derivatives_pricer.core.engines import discounting
            from rate_derivatives_pricer.core.instruments import factories
            from rate_derivatives_pricer.core.market import interpolation
            from rate_derivatives_pricer.core.risk import measures, scenarios

            # Currency conventions are configuration files, not classes: load them explicitly.
            currency.register_conventions_from_package_data()
            # Preset scenarios are configuration too (core/risk/data/scenarios.json).
            scenarios.register_scenarios_from_package_data()
        except BaseException:
            _builtins_loaded = False
            raise
        del calendar, day_count, discounting, factories, interpolation, measures, scenarios


CALENDARS: Registry[str, type[Calendar]] = Registry(
    "calendar", ConventionError, load_builtin_components
)
DAY_COUNTS: Registry[str, type[DayCountConvention]] = Registry(
    "day count convention", ConventionError, load_builtin_components
)
INTERPOLATORS: Registry[str, type[Interpolator]] = Registry(
    "interpolator", ConventionError, load_builtin_components
)
# A factory takes the trade parameters and the valuation date and returns an Instrument.
type InstrumentFactory = Callable[[Mapping[str, Any], date], Instrument]

INSTRUMENTS: Registry[str, InstrumentFactory] = Registry(
    "instrument type", UnsupportedInstrumentError, load_builtin_components
)
ENGINES: Registry[tuple[str, str], type[PricingEngine]] = Registry(
    "pricing engine", UnsupportedInstrumentError, load_builtin_components
)
RISK_MEASURES: Registry[str, type[RiskMeasure]] = Registry(
    "risk measure", UnsupportedRiskMeasureError, load_builtin_components
)
CONVENTIONS: Registry[str, CurrencyConventions] = Registry(
    "currency", ConventionError, load_builtin_components
)
SCENARIOS: Registry[str, Scenario] = Registry(
    "scenario", UnsupportedScenarioError, load_builtin_components
)

__all__ = [
    "CALENDARS",
    "CONVENTIONS",
    "DAY_COUNTS",
    "ENGINES",
    "INSTRUMENTS",
    "INTERPOLATORS",
    "RISK_MEASURES",
    "SCENARIOS",
    "Registry",
    "load_builtin_components",
]
