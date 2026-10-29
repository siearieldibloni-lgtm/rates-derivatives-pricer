from __future__ import annotations

from typing import Any

import pytest

from rate_derivatives_pricer.core.errors import (
    ConventionError,
    RatesPricerError,
    UnsupportedInstrumentError,
    UnsupportedRiskMeasureError,
)
from rate_derivatives_pricer.core.market.environment import MarketEnvironment
from rate_derivatives_pricer.core.market.providers import FileMarketDataProvider, snapshot_to_dict
from rate_derivatives_pricer.core.market.snapshot import MarketSnapshot
from rate_derivatives_pricer.services.curve_cache import LruCurveCache
from rate_derivatives_pricer.services.mappers import (
    canonical_hash,
    market_fingerprint,
    snapshot_from_input,
    trade_fingerprint,
)
from rate_derivatives_pricer.services.pricer_service import PricerService
from rate_derivatives_pricer.services.requests import (
    BatchTrade,
    CalculationOptions,
    InlineMarket,
    RiskOptions,
    SnapshotReference,
    TradeInput,
)
from tests.unit.helpers import MARKET_DIR, REFERENCE_SNAPSHOT, flat_environment

PROVIDER = FileMarketDataProvider(MARKET_DIR)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_lru_cache_evicts_the_least_recently_used_entry() -> None:
    cache = LruCurveCache(max_size=2)
    first, second, third = (flat_environment(r) for r in (0.01, 0.02, 0.03))
    cache.put("a", first)
    cache.put("b", second)
    assert cache.get("a") is first  # "a" becomes the most recently used
    cache.put("c", third)  # evicts "b"
    assert cache.get("b") is None
    assert cache.get("c") is third
    stats = cache.stats()
    assert (stats.hits, stats.misses, stats.evictions, stats.size, stats.max_size) == (
        2,
        1,
        1,
        2,
        2,
    )
    cache.clear()
    assert cache.stats().size == cache.stats().hits == 0


def test_lru_cache_ttl() -> None:
    clock = FakeClock()
    cache = LruCurveCache(max_size=4, ttl_seconds=60.0, clock=clock)
    env = flat_environment(0.02)
    cache.put("a", env)
    clock.now = 59.0
    assert cache.get("a") is env
    clock.now = 121.0
    assert cache.get("a") is None
    assert cache.stats().evictions == 1
    assert cache.stats().ttl_seconds == 60.0


def test_lru_cache_rejects_zero_size() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        LruCurveCache(max_size=0)


def test_canonical_hash_ignores_key_order() -> None:
    assert canonical_hash({"a": 1, "b": [1, 2]}) == canonical_hash({"b": [1, 2], "a": 1})
    assert canonical_hash({"a": 1}) != canonical_hash({"a": 2})


def test_market_fingerprint_depends_on_data_options_and_version(snapshot: MarketSnapshot) -> None:
    options = CalculationOptions()
    base = market_fingerprint(snapshot, options, "0.1.0")
    renamed = MarketSnapshot(
        name="other",
        currency=snapshot.currency,
        valuation_date=snapshot.valuation_date,
        quotes=snapshot.quotes,
        fixings=snapshot.fixings,
    )
    assert market_fingerprint(renamed, options, "0.1.0") == base
    assert (
        market_fingerprint(snapshot, CalculationOptions(interpolation="linear_zero"), "0.1.0")
        != base
    )
    assert market_fingerprint(snapshot, options, "0.2.0") != base
    bumped = snapshot.with_quotes(
        (snapshot.quotes[0].with_rate(snapshot.quotes[0].rate + 1e-4), *snapshot.quotes[1:])
    )
    assert market_fingerprint(bumped, options, "0.1.0") != base


def test_trade_fingerprint() -> None:
    trade = TradeInput("ois_swap", {"notional": 1e6, "maturity": "5Y"})
    same = TradeInput("ois_swap", {"maturity": "5Y", "notional": 1e6})
    assert trade_fingerprint(trade) == trade_fingerprint(same)


def test_snapshot_from_input(snapshot: MarketSnapshot) -> None:
    by_reference = snapshot_from_input(
        SnapshotReference(REFERENCE_SNAPSHOT), CalculationOptions(), PROVIDER
    )
    assert by_reference == snapshot
    data = snapshot_to_dict(snapshot)
    inline = InlineMarket(
        valuation_date=data["valuation_date"],
        quotes=tuple(data["quotes"]),
        fixings={k: tuple(v) for k, v in data["fixings"].items()},
    )
    by_value = snapshot_from_input(inline, CalculationOptions(currency="EUR"), PROVIDER)
    assert by_value.name == "inline"
    assert by_value.quotes == snapshot.quotes
    assert by_value.fixings == snapshot.fixings


def _service() -> PricerService:
    return PricerService(PROVIDER, LruCurveCache())


def test_service_metadata() -> None:
    service = _service()
    catalog: dict[str, Any] = service.catalog()
    assert catalog["instrument_types"] == ["ois_swap", "vanilla_irs"]
    assert "key_rate_dv01" in catalog["risk_measures"]
    assert catalog["solvers"] == ["brent", "newton"]
    assert all(service.readiness().values())
    assert REFERENCE_SNAPSHOT in service.list_snapshots()
    assert service.get_snapshot(REFERENCE_SNAPSHOT)["name"] == REFERENCE_SNAPSHOT
    assert set(service.version_info()) == {"api", "core", "python", "numpy"}


def test_service_reuses_cached_environments(env: MarketEnvironment) -> None:
    service = _service()
    market, options = SnapshotReference(REFERENCE_SNAPSHOT), CalculationOptions()
    first = service.build_curves(market, options)
    trade = TradeInput(
        "ois_swap",
        {"notional": 1e6, "fixed_rate": 0.02, "direction": "pay_fixed", "maturity": "2Y"},
    )
    second = service.price(market, trade, options)
    assert (first.diagnostics.curve_cache, second.diagnostics.curve_cache) == ("miss", "hit")
    assert second.diagnostics.timings_ms["curve_build"] == 0.0
    assert service.cache_stats().hits == 1
    nodes = first.result["curves"][0]["nodes"]
    assert [n["discount_factor"] for n in nodes] == [
        p.discount_factor for p in env.calibration[0].pillars
    ]


def test_matured_trade_reports_an_undefined_par_rate() -> None:
    trade = TradeInput(
        "ois_swap",
        {
            "notional": 1e6,
            "fixed_rate": 0.02,
            "direction": "pay_fixed",
            "start_date": "2025-01-15",
            "end_date": "2026-01-15",
        },
    )
    outcome = _service().price(SnapshotReference(REFERENCE_SNAPSHOT), trade, CalculationOptions())
    assert outcome.result["par_rate"] is None
    assert outcome.diagnostics.warnings


def test_risk_validates_before_working_and_does_not_use_the_curve_cache() -> None:
    service = _service()
    market, options = SnapshotReference(REFERENCE_SNAPSHOT), CalculationOptions()
    trade = TradeInput(
        "vanilla_irs",
        {"notional": 1e6, "fixed_rate": 0.02, "direction": "pay_fixed", "maturity": "1Y"},
    )
    with pytest.raises(UnsupportedRiskMeasureError):
        service.risk(market, trade, RiskOptions(("vega",)), options)
    with pytest.raises(ConventionError, match="unknown bump mode"):
        service.risk(market, trade, RiskOptions(("parallel_dv01",), mode="sideways"), options)
    mismatched = TradeInput("ois_swap", {**trade.params, "currency": "USD"})
    with pytest.raises(ConventionError):
        service.risk(market, mismatched, RiskOptions(("parallel_dv01",)), options)

    outcome = service.risk(market, trade, RiskOptions(("parallel_dv01", "convexity")), options)
    assert outcome.diagnostics.curve_cache is None
    assert service.cache_stats().size == 0
    assert {"market_data", "risk", "total"} <= set(outcome.diagnostics.timings_ms)
    assert [m["measure"] for m in outcome.result["measures"]] == ["parallel_dv01", "convexity"]


def test_clear_cache_reports_the_dropped_entries() -> None:
    service = _service()
    service.build_curves(SnapshotReference(REFERENCE_SNAPSHOT), CalculationOptions())
    assert service.clear_cache() == 1
    assert service.cache_stats().size == 0
    assert service.clear_cache() == 0


def test_price_batch_isolates_failing_trades() -> None:
    service = _service()
    params = {"notional": 1e6, "fixed_rate": 0.02, "direction": "pay_fixed", "maturity": "2Y"}
    trades = [
        BatchTrade("ok", TradeInput("ois_swap", params)),
        BatchTrade("unknown", TradeInput("cap", params)),
        BatchTrade("usd", TradeInput("ois_swap", {**params, "currency": "USD"})),
        # Not validated by the API schemas here: the factory fails with a non-domain error.
        BatchTrade("broken", TradeInput("ois_swap", {**params, "notional": None})),
        BatchTrade("irs", TradeInput("vanilla_irs", params)),
    ]
    outcome = service.price_batch(
        SnapshotReference(REFERENCE_SNAPSHOT), trades, CalculationOptions()
    )
    assert [item.trade_id for item in outcome.items] == ["ok", "unknown", "usd", "broken", "irs"]
    errors = {item.trade_id: item.error for item in outcome.items}
    assert errors["ok"] is None
    assert errors["irs"] is None
    assert isinstance(errors["unknown"], UnsupportedInstrumentError)
    assert isinstance(errors["usd"], ConventionError)
    assert errors["broken"] is not None
    assert not isinstance(errors["broken"], RatesPricerError)
    single = service.price(
        SnapshotReference(REFERENCE_SNAPSHOT), trades[0].trade, CalculationOptions()
    )
    assert outcome.items[0].result == single.result
    assert outcome.items[0].trade_hash == single.diagnostics.input_hashes["trade"]
    assert outcome.diagnostics.curve_cache == "miss"  # one bootstrap for the whole batch
    assert service.cache_stats().misses == 1
    assert {"market_data", "curve_build", "pricing", "total"} <= set(outcome.diagnostics.timings_ms)
    assert (outcome.currency, outcome.valuation_date.isoformat()) == ("EUR", "2026-10-19")
