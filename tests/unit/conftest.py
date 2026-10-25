from __future__ import annotations

import pytest

from rate_derivatives_pricer.core.conventions.currency import CurrencyConventions, get_conventions
from rate_derivatives_pricer.core.market.bootstrap import CurveBuilder
from rate_derivatives_pricer.core.market.environment import MarketEnvironment
from rate_derivatives_pricer.core.market.providers import FileMarketDataProvider
from rate_derivatives_pricer.core.market.snapshot import MarketSnapshot
from tests.unit.helpers import MARKET_DIR, REFERENCE_SNAPSHOT


@pytest.fixture(scope="session")
def eur() -> CurrencyConventions:
    return get_conventions("EUR")


@pytest.fixture(scope="session")
def snapshot() -> MarketSnapshot:
    return FileMarketDataProvider(MARKET_DIR).load_snapshot(REFERENCE_SNAPSHOT)


@pytest.fixture(scope="session")
def env(snapshot: MarketSnapshot) -> MarketEnvironment:
    return CurveBuilder().build(snapshot)
