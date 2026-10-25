from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from rate_derivatives_pricer.core.errors import MarketDataError, MarketDataNotFoundError
from rate_derivatives_pricer.core.market.environment import MarketEnvironment
from rate_derivatives_pricer.core.market.providers import (
    FileMarketDataProvider,
    snapshot_from_dict,
    snapshot_to_dict,
)
from rate_derivatives_pricer.core.market.quotes import (
    DepositQuote,
    OisSwapQuote,
    QuoteUnit,
)
from rate_derivatives_pricer.core.market.snapshot import Fixing, MarketSnapshot
from tests.unit.helpers import MARKET_DIR, REFERENCE_SNAPSHOT, VALUATION_DATE, flat_curve


@pytest.mark.parametrize(
    ("value", "unit", "rate"),
    [
        (2.5, QuoteUnit.PERCENT, 0.025),
        (0.025, QuoteUnit.DECIMAL, 0.025),
        (250, QuoteUnit.BP, 0.025),
    ],
)
def test_quote_units_convert_to_decimal(value: float, unit: QuoteUnit, rate: float) -> None:
    quote = OisSwapQuote("5Y", value, unit)
    assert quote.rate == pytest.approx(rate, abs=1e-15)
    bumped = quote.with_rate(0.026)
    assert bumped.unit is unit
    assert bumped.rate == pytest.approx(0.026, abs=1e-15)
    assert quote.key == ("ois_swap", "5Y")


def test_quote_validation() -> None:
    with pytest.raises(MarketDataError):
        DepositQuote("ON", float("nan"))
    with pytest.raises(Exception, match="invalid tenor"):
        OisSwapQuote("5 years", 2.0)


def _snapshot(**overrides: Any) -> MarketSnapshot:
    fields: dict[str, Any] = {
        "name": "test",
        "currency": "EUR",
        "valuation_date": VALUATION_DATE,
        "quotes": (DepositQuote("ON", 0.02), OisSwapQuote("1Y", 0.021)),
    }
    fields.update(overrides)
    return MarketSnapshot(**fields)


def test_snapshot_validation() -> None:
    with pytest.raises(MarketDataError, match="no quotes"):
        _snapshot(quotes=())
    with pytest.raises(MarketDataError, match="duplicate quote"):
        _snapshot(quotes=(OisSwapQuote("1Y", 0.02), OisSwapQuote("1y", 0.021)))
    with pytest.raises(MarketDataError, match="after the valuation date"):
        _snapshot(fixings=(Fixing("ESTR", date(2026, 10, 20), 0.02),))
    with pytest.raises(MarketDataError, match="duplicate fixing"):
        _snapshot(fixings=(Fixing("ESTR", VALUATION_DATE, 0.02),) * 2)
    with pytest.raises(MarketDataError, match="finite"):
        _snapshot(fixings=(Fixing("ESTR", VALUATION_DATE, float("inf")),))


def test_snapshot_helpers() -> None:
    snapshot = _snapshot(fixings=(Fixing("ESTR", date(2026, 10, 16), 0.0192),))
    assert snapshot.fixings_by_index() == {"ESTR": {date(2026, 10, 16): 0.0192}}
    replaced = snapshot.with_quotes((OisSwapQuote("2Y", 0.022),))
    assert replaced.quotes == (OisSwapQuote("2Y", 0.022),)
    assert replaced.fixings == snapshot.fixings


def test_reference_snapshot_file(snapshot: MarketSnapshot) -> None:
    assert snapshot.name == REFERENCE_SNAPSHOT
    assert snapshot.valuation_date == VALUATION_DATE
    assert "SYNTHETIC" in snapshot.description
    assert snapshot.quotes[0] == DepositQuote("ON", 1.921, QuoteUnit.PERCENT)
    estr = snapshot.fixings_by_index()["ESTR"]
    assert max(estr) == date(2026, 10, 16)
    assert all(0.0 < rate < 0.05 for rate in estr.values())


def test_snapshot_dict_round_trip(snapshot: MarketSnapshot) -> None:
    data = snapshot_to_dict(snapshot)
    assert json.loads(json.dumps(data)) == data
    assert snapshot_from_dict(data) == snapshot


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda d: d.pop("quotes"), "snapshot.quotes"),
        (lambda d: d.pop("valuation_date"), "valuation_date"),
        (lambda d: d["quotes"][0].update(type="cap"), "unknown quote type"),
        (lambda d: d["quotes"][0].update(unit="permille"), "invalid unit"),
        (lambda d: d["quotes"][0].update(value="2.0"), "invalid number"),
        (lambda d: d["quotes"][0].update(value=True), "invalid number"),
        (lambda d: d.update(valuation_date="19/10/2026"), "invalid date"),
        (lambda d: d["fixings"]["ESTR"][0].pop("value"), "value"),
    ],
)
def test_snapshot_from_dict_errors(snapshot: MarketSnapshot, mutation: Any, message: str) -> None:
    data = snapshot_to_dict(snapshot)
    mutation(data)
    with pytest.raises(MarketDataError, match=message):
        snapshot_from_dict(data)


def test_file_provider(tmp_path: Path) -> None:
    provider = FileMarketDataProvider(MARKET_DIR)
    assert provider.directory == MARKET_DIR
    assert REFERENCE_SNAPSHOT in provider.list_snapshots()
    with pytest.raises(MarketDataNotFoundError):
        provider.load_snapshot("EUR_1999-01-01")
    with pytest.raises(MarketDataError, match="invalid snapshot name"):
        provider.load_snapshot("../secrets")

    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "renamed.json").write_text(
        (MARKET_DIR / f"{REFERENCE_SNAPSHOT}.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    local = FileMarketDataProvider(tmp_path)
    assert local.list_snapshots() == ("broken", "renamed")
    with pytest.raises(MarketDataError, match="not valid JSON"):
        local.load_snapshot("broken")
    with pytest.raises(MarketDataError, match="does not match"):
        local.load_snapshot("renamed")
    assert FileMarketDataProvider(tmp_path / "missing").list_snapshots() == ()


def test_environment_lookups() -> None:
    curve = flat_curve(0.02)
    env = MarketEnvironment(
        valuation_date=VALUATION_DATE,
        currency="EUR",
        discount_curve=curve,
        projection_curves={"ESTR": curve},
        fixings={"ESTR": {date(2026, 10, 16): 0.0192}},
    )
    assert env.projection_curve("ESTR") is curve
    assert env.fixing("ESTR", date(2026, 10, 16)) == 0.0192
    assert env.fixing("ESTR", date(2026, 10, 15)) is None
    assert env.fixing("SOFR", date(2026, 10, 16)) is None
    with pytest.raises(MarketDataError, match="no projection curve"):
        env.projection_curve("EURIBOR3M")
    with pytest.raises(TypeError):
        env.fixings["ESTR"][date(2026, 10, 15)] = 0.0  # type: ignore[index]
