"""Bumps, risk measures and the risk engine, on analytic pricers and on the real pipeline."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import ClassVar

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rate_derivatives_pricer.core.engines.base import engine_for
from rate_derivatives_pricer.core.errors import (
    CalibrationError,
    MarketDataError,
    RiskComputationError,
    UnsupportedRiskMeasureError,
)
from rate_derivatives_pricer.core.instruments.swaps import Instrument, Swap
from rate_derivatives_pricer.core.market.bootstrap import CurveBuilder
from rate_derivatives_pricer.core.market.quotes import (
    DepositQuote,
    IborSwapQuote,
    OisSwapQuote,
    Quote,
    QuoteUnit,
)
from rate_derivatives_pricer.core.market.snapshot import MarketSnapshot
from rate_derivatives_pricer.core.registry import INSTRUMENTS, RISK_MEASURES
from rate_derivatives_pricer.core.risk import (
    Bumper,
    BumpMode,
    BumpSpec,
    BumpTarget,
    RiskEngine,
    RiskMeasure,
    RiskResult,
    RiskSettings,
)
from rate_derivatives_pricer.core.risk.measures import Pricer
from tests.unit.helpers import VALUATION_DATE

BP = 1e-4


@dataclass(frozen=True, slots=True)
class StubInstrument(Instrument):
    """Instrument without behaviour: the analytic pricers below ignore it."""

    instrument_type: ClassVar[str] = "stub"

    @property
    def currency(self) -> str:
        return "EUR"


def small_snapshot() -> MarketSnapshot:
    quotes: tuple[Quote, ...] = (
        DepositQuote("ON", 1.9, QuoteUnit.PERCENT),
        OisSwapQuote("1Y", 2.0, QuoteUnit.PERCENT),
        OisSwapQuote("5Y", 2.2, QuoteUnit.PERCENT),
        IborSwapQuote("1Y", 2.3, QuoteUnit.PERCENT, index_name="EURIBOR6M"),
        IborSwapQuote("5Y", 2.5, QuoteUnit.PERCENT, index_name="EURIBOR6M"),
    )
    return MarketSnapshot("small", "EUR", VALUATION_DATE, quotes)


def quadratic_pricer(weights: dict[tuple[str, str], float], gamma: float) -> Pricer:
    """``PV = sum_k a_k q_k + gamma / 2 * (sum_k q_k)^2``: derivatives are known exactly."""

    def pricer(_: Instrument, snapshot: MarketSnapshot) -> float:
        total = sum(q.rate for q in snapshot.quotes)
        return sum(weights[q.key] * q.rate for q in snapshot.quotes) + 0.5 * gamma * total**2

    return pricer


WEIGHTS = {
    ("deposit", "ON"): 1e5,
    ("ois_swap", "1Y"): -3e6,
    ("ois_swap", "5Y"): 2e7,
    ("ibor_swap", "EURIBOR6M/1Y"): 1.5e6,
    ("ibor_swap", "EURIBOR6M/5Y"): 4e7,
}
GAMMA = 7e8


# --------------------------------------------------------------------------- bumps
def test_bumper_shifts_only_the_selected_quotes_and_returns_a_new_snapshot() -> None:
    snapshot = small_snapshot()
    bumped = Bumper().apply(snapshot, BumpTarget(curves=frozenset({"EURIBOR6M"})), 2.0)
    assert bumped is not snapshot
    for before, after in zip(snapshot.quotes, bumped.quotes, strict=True):
        expected = before.rate + (2 * BP if before.curve_label == "EURIBOR6M" else 0.0)
        assert after.rate == pytest.approx(expected, abs=1e-15)
        assert after.unit is before.unit  # the quote keeps its unit
    assert snapshot.quotes[0].rate == pytest.approx(0.019)  # the original is untouched


def test_bump_target_criteria_are_combined() -> None:
    quotes = small_snapshot().quotes
    one_ois = BumpTarget(curves=frozenset({"OIS"}), instrument_types=frozenset({"ois_swap"}))
    assert [q.tenor_label for q in quotes if one_ois.matches(q)] == ["1Y", "5Y"]
    assert all(BumpTarget().matches(q) for q in quotes)
    assert BumpTarget().describe() == "all quotes"
    assert "curves=['OIS']" in one_ois.describe()


def test_a_target_selecting_nothing_is_an_error() -> None:
    with pytest.raises(RiskComputationError, match="selects no quote"):
        Bumper().apply(small_snapshot(), BumpTarget(curves=frozenset({"EURIBOR3M"})), 1.0)


@pytest.mark.parametrize(
    ("mode", "shifts"),
    [(BumpMode.UP, (0.5,)), (BumpMode.DOWN, (-0.5,)), (BumpMode.CENTRAL, (0.5, -0.5))],
)
def test_bump_spec_shifts(mode: BumpMode, shifts: tuple[float, ...]) -> None:
    assert BumpSpec(BumpTarget(), 0.5, mode).shifts_bp == shifts


@pytest.mark.parametrize("size", [0.0, -1.0, float("nan"), float("inf")])
def test_bump_size_must_be_positive_and_finite(size: float) -> None:
    with pytest.raises(RiskComputationError):
        BumpSpec(BumpTarget(), size)


# --------------------------------------------------------------------------- measures
def test_measures_recover_the_exact_derivatives_of_a_quadratic_pricer() -> None:
    snapshot, pricer = small_snapshot(), quadratic_pricer(WEIGHTS, GAMMA)
    report = RiskEngine(pricer).compute(
        StubInstrument(), snapshot, ["parallel_dv01", "key_rate_dv01", "curve_dv01", "convexity"]
    )
    total_rate = sum(q.rate for q in snapshot.quotes)
    count = len(snapshot.quotes)
    # d PV / d q_k = a_k + gamma * S ; a bp is 1e-4. A central difference is exact on a quadratic.
    assert report.result("parallel_dv01").value == pytest.approx(
        (sum(WEIGHTS.values()) + GAMMA * count * total_rate) * BP, rel=1e-9
    )
    assert report.result("convexity").value == pytest.approx(GAMMA * count**2 * BP**2, rel=1e-6)
    key_rates = report.result("key_rate_dv01")
    assert [(b.curve, b.label) for b in key_rates.buckets] == [
        ("OIS", "ON"),
        ("OIS", "1Y"),
        ("OIS", "5Y"),
        ("EURIBOR6M", "1Y"),
        ("EURIBOR6M", "5Y"),
    ]
    for bucket, quote in zip(key_rates.buckets, snapshot.quotes, strict=True):
        assert bucket.value == pytest.approx(
            (WEIGHTS[quote.key] + GAMMA * total_rate) * BP, rel=1e-9
        )
    assert key_rates.value == pytest.approx(report.result("parallel_dv01").value, rel=1e-9)
    curves = {b.curve: b.value for b in report.result("curve_dv01").buckets}
    ois_count = 3
    ois_weight = (
        WEIGHTS[("deposit", "ON")] + WEIGHTS[("ois_swap", "1Y")] + WEIGHTS[("ois_swap", "5Y")]
    )
    assert curves["OIS"] == pytest.approx(
        (ois_weight + GAMMA * ois_count * total_rate) * BP, rel=1e-9
    )
    assert sum(curves.values()) == pytest.approx(report.result("parallel_dv01").value, rel=1e-9)
    assert report.base_pv == pricer(StubInstrument(), snapshot)


@pytest.mark.parametrize(("mode", "sign"), [(BumpMode.UP, 1.0), (BumpMode.DOWN, -1.0)])
def test_one_sided_differences_carry_the_second_order_error(mode: BumpMode, sign: float) -> None:
    snapshot, pricer = small_snapshot(), quadratic_pricer(WEIGHTS, GAMMA)
    size = 0.5
    result = (
        RiskEngine(pricer)
        .compute(StubInstrument(), snapshot, ["parallel_dv01"], RiskSettings(size, mode))
        .result("parallel_dv01")
    )
    count = len(snapshot.quotes)
    first = (sum(WEIGHTS.values()) + GAMMA * count * sum(q.rate for q in snapshot.quotes)) * BP
    second = GAMMA * count**2 * BP**2
    assert result.value == pytest.approx(first + sign * 0.5 * second * size, rel=1e-9)


def test_the_bump_size_does_not_change_a_per_bp_sensitivity_on_a_quadratic() -> None:
    snapshot, pricer = small_snapshot(), quadratic_pricer(WEIGHTS, GAMMA)
    values = [
        RiskEngine(pricer).compute(
            StubInstrument(), snapshot, ["parallel_dv01", "convexity"], RiskSettings(size)
        )
        for size in (0.1, 1.0, 25.0)
    ]
    for name in ("parallel_dv01", "convexity"):
        assert values[0].result(name).value == pytest.approx(values[2].result(name).value, rel=1e-6)


@settings(max_examples=40, deadline=None)
@given(
    weights=st.lists(st.floats(-1e8, 1e8, allow_nan=False), min_size=5, max_size=5),
    gamma=st.floats(-1e9, 1e9, allow_nan=False),
)
def test_the_key_rate_dv01s_add_up_to_the_parallel_dv01(weights: list[float], gamma: float) -> None:
    snapshot = small_snapshot()
    table = {q.key: w for q, w in zip(snapshot.quotes, weights, strict=True)}
    report = RiskEngine(quadratic_pricer(table, gamma)).compute(
        StubInstrument(), snapshot, ["parallel_dv01", "key_rate_dv01", "curve_dv01"]
    )
    parallel = report.result("parallel_dv01").value
    scale = max(abs(parallel), 1.0)
    assert report.result("key_rate_dv01").value == pytest.approx(parallel, abs=1e-6 * scale)
    assert report.result("curve_dv01").value == pytest.approx(parallel, abs=1e-6 * scale)


# --------------------------------------------------------------------------- engine
class CountingPricer:
    def __init__(self, inner: Pricer) -> None:
        self.inner = inner
        self.calls = 0

    def __call__(self, instrument: Instrument, snapshot: MarketSnapshot) -> float:
        self.calls += 1
        return self.inner(instrument, snapshot)


def test_valuations_shared_between_measures_are_computed_once() -> None:
    pricer = CountingPricer(quadratic_pricer(WEIGHTS, GAMMA))
    RiskEngine(pricer).compute(
        StubInstrument(), small_snapshot(), ["parallel_dv01", "convexity", "parallel_dv01"]
    )
    # base, parallel +1bp, parallel -1bp: shared by the DV01, the convexity and the repeat.
    assert pricer.calls == 3


def test_an_unknown_measure_fails_before_any_valuation() -> None:
    pricer = CountingPricer(quadratic_pricer(WEIGHTS, GAMMA))
    with pytest.raises(UnsupportedRiskMeasureError) as error:
        RiskEngine(pricer).compute(StubInstrument(), small_snapshot(), ["parallel_dv01", "vega"])
    assert "key_rate_dv01" in error.value.details["available"]
    assert pricer.calls == 0


def test_a_failing_bumped_valuation_is_reported_as_a_risk_failure() -> None:
    base = quadratic_pricer(WEIGHTS, GAMMA)
    snapshot = small_snapshot()

    def pricer(instrument: Instrument, bumped: MarketSnapshot) -> float:
        if bumped.quotes != snapshot.quotes:
            raise CalibrationError("bootstrap failed on pillar 5Y", pillar="5Y", residual=1e-4)
        return base(instrument, bumped)

    with pytest.raises(RiskComputationError, match="parallel_dv01 failed") as error:
        RiskEngine(pricer).compute(StubInstrument(), snapshot, ["parallel_dv01"])
    assert error.value.details["measure"] == "parallel_dv01"
    assert error.value.details["pillar"] == "5Y"
    assert isinstance(error.value.__cause__, CalibrationError)


def test_a_trade_that_cannot_be_valued_at_all_is_not_a_risk_failure() -> None:
    def pricer(_: Instrument, __: MarketSnapshot) -> float:
        raise MarketDataError("missing EURIBOR6M fixing")

    with pytest.raises(MarketDataError):
        RiskEngine(pricer).compute(StubInstrument(), small_snapshot(), ["parallel_dv01"])


# --------------------------------------------------------------------------- extensibility
@pytest.fixture
def registered_test_measure() -> Iterator[str]:
    """Register a measure from a test module: no line of ``risk/`` or of the engines changes."""

    class QuoteCount(RiskMeasure):
        name: ClassVar[str] = "quote_count"
        unit: ClassVar[str] = "quotes"

        def compute(
            self,
            instrument: Instrument,
            snapshot: MarketSnapshot,
            pricer: Pricer,
            settings: RiskSettings,
        ) -> RiskResult:
            return RiskResult(self.name, float(len(snapshot.quotes)), self.unit)

    RISK_MEASURES.add("quote_count", QuoteCount)
    try:
        yield "quote_count"
    finally:
        RISK_MEASURES._entries.pop("quote_count")


def test_a_new_measure_plugs_in_through_the_registry(registered_test_measure: str) -> None:
    assert registered_test_measure in RISK_MEASURES
    report = RiskEngine(quadratic_pricer(WEIGHTS, GAMMA)).compute(
        StubInstrument(), small_snapshot(), [registered_test_measure, "parallel_dv01"]
    )
    assert report.result("quote_count").value == 5.0
    assert [r.measure for r in report.results] == ["quote_count", "parallel_dv01"]
    with pytest.raises(KeyError):
        report.result("convexity")


# --------------------------------------------------------------------------- real pipeline
def _real_report(snapshot: MarketSnapshot, instrument_type: str, params: dict[str, object]):
    builder = CurveBuilder()
    engine = engine_for(instrument_type)
    instrument = INSTRUMENTS.get(instrument_type)(params, snapshot.valuation_date)

    def pricer(priced: Instrument, bumped: MarketSnapshot) -> float:
        # Curves only as far as the trade needs, as the service does.
        horizon = priced.last_cashflow_date if isinstance(priced, Swap) else None
        return engine.present_value(priced, builder.build(bumped, horizon=horizon))

    return RiskEngine(pricer).compute(
        instrument, snapshot, ["parallel_dv01", "key_rate_dv01", "curve_dv01", "convexity"]
    )


def test_an_ois_swap_has_no_exposure_to_the_euribor_curves(snapshot: MarketSnapshot) -> None:
    report = _real_report(
        snapshot,
        "ois_swap",
        {"notional": 1e7, "fixed_rate": 0.02, "direction": "pay_fixed", "maturity": "3Y"},
    )
    by_curve = {b.curve: b.value for b in report.result("curve_dv01").buckets}
    assert by_curve["EURIBOR6M"] == 0.0 == by_curve["EURIBOR3M"]
    assert by_curve["OIS"] == pytest.approx(report.result("parallel_dv01").value, rel=1e-6)
    assert report.result("parallel_dv01").value > 0.0  # a payer gains when rates rise


def test_an_irs_is_mostly_exposed_to_its_own_projection_curve(snapshot: MarketSnapshot) -> None:
    report = _real_report(
        snapshot,
        "vanilla_irs",
        {"notional": 1e7, "fixed_rate": 0.025, "direction": "pay_fixed", "maturity": "5Y"},
    )
    by_curve = {b.curve: b.value for b in report.result("curve_dv01").buckets}
    assert by_curve["EURIBOR3M"] == 0.0
    # ~ N * annuity * 1bp = 1e7 * 4.69 * 1e-4: the 5Y Euribor 6M par rate dominates.
    assert by_curve["EURIBOR6M"] == pytest.approx(4200.0, rel=0.15)
    assert abs(by_curve["OIS"]) < 0.01 * by_curve["EURIBOR6M"]
    buckets = {(b.curve, b.label): b.value for b in report.result("key_rate_dv01").buckets}
    assert buckets[("EURIBOR6M", "5Y")] == max(buckets.values())
    assert buckets[("OIS", "30Y")] == 0.0  # beyond the trade: bumping it cannot change the PV
    assert sum(buckets.values()) == pytest.approx(report.result("parallel_dv01").value, rel=1e-6)
    assert report.result("convexity").value != 0.0
