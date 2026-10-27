"""Scenarios, scenario P&L and carry/roll-down (phase 3), on analytic pricers and the pipeline."""

from __future__ import annotations

import itertools
from datetime import date
from typing import ClassVar

import pytest

from rate_derivatives_pricer.core.conventions.calendar import TargetCalendar
from rate_derivatives_pricer.core.conventions.currency import CurrencyConventions
from rate_derivatives_pricer.core.conventions.day_count import Act360
from rate_derivatives_pricer.core.conventions.tenor import Tenor, TimeUnit
from rate_derivatives_pricer.core.engines.base import PricingEngine, engine_for
from rate_derivatives_pricer.core.engines.results import PricingResult
from rate_derivatives_pricer.core.errors import (
    ConventionError,
    MarketDataError,
    RiskComputationError,
    UnsupportedInstrumentError,
    UnsupportedRiskMeasureError,
    UnsupportedScenarioError,
)
from rate_derivatives_pricer.core.instruments.cashflows import (
    FixedCoupon,
    OvernightCompoundedCoupon,
)
from rate_derivatives_pricer.core.instruments.swaps import Instrument, Swap
from rate_derivatives_pricer.core.market.bootstrap import CurveBuilder
from rate_derivatives_pricer.core.market.environment import MarketEnvironment
from rate_derivatives_pricer.core.market.quotes import (
    DepositQuote,
    IborSwapQuote,
    OisSwapQuote,
    Quote,
    QuoteUnit,
)
from rate_derivatives_pricer.core.market.snapshot import MarketSnapshot
from rate_derivatives_pricer.core.registry import INSTRUMENTS, SCENARIOS
from rate_derivatives_pricer.core.risk import (
    CarryRolldown,
    RiskEngine,
    RiskSettings,
    Scenario,
    ScenarioPnL,
    ScenarioPoint,
    resolve_scenarios,
    scenario_from_dict,
    unchanged_curve_snapshot,
)
from rate_derivatives_pricer.core.risk.carry import horizon_date
from rate_derivatives_pricer.core.risk.measures import Pricer
from rate_derivatives_pricer.core.risk.scenarios import tenor_in_years
from tests.unit.helpers import VALUATION_DATE, flat_environment
from tests.validation.tolerances import TAYLOR_REL

BP = 1e-4
PRESETS = ["parallel_up", "parallel_down", "steepener", "flattener", "twist"]


def small_snapshot() -> MarketSnapshot:
    quotes: tuple[Quote, ...] = (
        DepositQuote("ON", 1.9, QuoteUnit.PERCENT),
        OisSwapQuote("1Y", 2.0, QuoteUnit.PERCENT),
        OisSwapQuote("5Y", 2.2, QuoteUnit.PERCENT),
        IborSwapQuote("1Y", 230.0, QuoteUnit.BP, index_name="EURIBOR6M"),
        IborSwapQuote("10Y", 0.025, QuoteUnit.DECIMAL, index_name="EURIBOR6M"),
    )
    return MarketSnapshot("small", "EUR", VALUATION_DATE, quotes)


def linear_pricer(weights: dict[tuple[str, str], float]) -> Pricer:
    """``PV = sum_k a_k q_k``: a scenario P&L is exactly ``sum_k a_k shift_k``."""

    def pricer(_: Instrument, snapshot: MarketSnapshot) -> float:
        return sum(weights[q.key] * q.rate for q in snapshot.quotes)

    return pricer


class PipelineValuer:
    """Bootstrap + discounting engine: what the service layer plugs into the risk components."""

    def __init__(self) -> None:
        self.builder = CurveBuilder()

    def present_value(self, instrument: Instrument, snapshot: MarketSnapshot) -> float:
        engine = engine_for(instrument.instrument_type)
        return engine.present_value(instrument, self.builder.build(snapshot))

    def cashflows_paid(
        self, instrument: Instrument, snapshot: MarketSnapshot, start: date, end: date
    ) -> float:
        engine = engine_for(instrument.instrument_type)
        return engine.cashflows_paid(instrument, self.builder.build(snapshot), start, end)

    def accrued_interest(
        self, instrument: Instrument, snapshot: MarketSnapshot, start: date, end: date
    ) -> float:
        engine = engine_for(instrument.instrument_type)
        return engine.accrued_interest(instrument, self.builder.build(snapshot), start, end)


# --------------------------------------------------------------------------- scenario shapes
def test_tenor_in_years() -> None:
    assert tenor_in_years(Tenor.parse("ON")) == pytest.approx(1 / 365)
    assert tenor_in_years(Tenor.parse("2W")) == pytest.approx(14 / 365)
    assert tenor_in_years(Tenor.parse("18M")) == 1.5
    assert tenor_in_years(Tenor.parse("30Y")) == 30.0


@pytest.mark.parametrize(
    ("name", "tenor", "expected_bp"),
    [
        ("parallel_up", "ON", 100.0),
        ("parallel_up", "30Y", 100.0),
        ("parallel_down", "5Y", -100.0),
        ("steepener", "ON", -25.0),
        ("steepener", "2Y", -25.0),
        ("steepener", "6Y", 0.0),
        ("steepener", "10Y", 25.0),
        ("steepener", "30Y", 25.0),
        ("flattener", "1Y", 25.0),
        ("flattener", "8Y", -12.5),
        ("twist", "6M", -25.0),
        ("twist", "3Y", -12.5),
        ("twist", "5Y", 0.0),
        ("twist", "17Y", 12.0),
        ("twist", "30Y", 25.0),
    ],
)
def test_preset_shapes(name: str, tenor: str, expected_bp: float) -> None:
    assert SCENARIOS.get(name).shift_bp(Tenor.parse(tenor)) == pytest.approx(expected_bp)


def test_presets_are_loaded_from_configuration_in_file_order() -> None:
    assert list(SCENARIOS) == PRESETS
    assert [s.name for s in resolve_scenarios()] == PRESETS


def test_points_are_sorted_by_tenor() -> None:
    scenario = Scenario("s", (ScenarioPoint("10Y", 5.0), ScenarioPoint("6M", 1.0)))
    assert [p.tenor_label for p in scenario.points] == ["6M", "10Y"]


@pytest.mark.parametrize(
    "points",
    [
        (),
        (ScenarioPoint("1Y", 1.0), ScenarioPoint("12M", 2.0)),
        (ScenarioPoint("1Q", 1.0),),
        (ScenarioPoint("1Y", float("nan")),),
    ],
)
def test_ill_defined_scenarios_are_refused(points: tuple[ScenarioPoint, ...]) -> None:
    with pytest.raises(UnsupportedScenarioError):
        Scenario("bad", points)


def test_scenario_from_dict_and_errors() -> None:
    scenario = scenario_from_dict(
        {"name": "custom", "curves": ["ois"], "points": [{"tenor": "5Y", "shift_bp": 10}]}
    )
    assert scenario.curves == frozenset({"OIS"})
    assert scenario.shift_bp(Tenor.parse("1Y")) == 10.0
    with pytest.raises(UnsupportedScenarioError):
        scenario_from_dict({"name": "custom", "points": [{"tenor": "5Y"}]})
    with pytest.raises(UnsupportedScenarioError):
        Scenario(" ", (ScenarioPoint("1Y", 1.0),))


def test_resolve_scenarios_errors() -> None:
    with pytest.raises(UnsupportedScenarioError) as unknown:
        resolve_scenarios(["parallel_sideways"])
    # Mapped like an unknown risk measure (400) and lists what is available.
    assert isinstance(unknown.value, UnsupportedRiskMeasureError)
    assert unknown.value.details["available"] == PRESETS
    custom = Scenario("steepener", (ScenarioPoint("1Y", 1.0),))
    with pytest.raises(UnsupportedScenarioError):
        resolve_scenarios(["steepener"], [custom])
    assert [s.name for s in resolve_scenarios([], [custom])] == ["steepener"]


def test_apply_shifts_selected_curves_and_keeps_units() -> None:
    snapshot = small_snapshot()
    shocked = SCENARIOS.get("steepener").apply(snapshot)
    by_key = {q.key: q for q in shocked.quotes}
    assert by_key[("deposit", "ON")].rate == pytest.approx(0.019 - 25 * BP)
    assert by_key[("ibor_swap", "EURIBOR6M/1Y")].unit is QuoteUnit.BP
    assert by_key[("ibor_swap", "EURIBOR6M/1Y")].value == pytest.approx(230.0 - 25.0)
    assert by_key[("ibor_swap", "EURIBOR6M/10Y")].rate == pytest.approx(0.025 + 25 * BP)
    assert snapshot.quotes[0].value == 1.9  # untouched

    ois_only = Scenario("ois", (ScenarioPoint("1Y", 10.0),), curves=frozenset({"OIS"}))
    shifts = {(s.curve, s.label): s.shift_bp for s in ois_only.quote_shifts(snapshot)}
    assert shifts[("OIS", "5Y")] == 10.0
    assert shifts[("EURIBOR6M", "10Y")] == 0.0
    with pytest.raises(UnsupportedScenarioError) as error:
        Scenario("x", (ScenarioPoint("1Y", 1.0),), curves=frozenset({"SOFR"})).apply(snapshot)
    assert error.value.details["available"] == ["EURIBOR6M", "OIS"]


# --------------------------------------------------------------------------- scenario P&L
class _StubInstrument(Instrument):
    """Instrument without behaviour: the analytic pricers ignore it."""

    instrument_type: ClassVar[str] = "stub"

    @property
    def currency(self) -> str:
        return "EUR"


WEIGHTS = {
    ("deposit", "ON"): 1e5,
    ("ois_swap", "1Y"): -2e5,
    ("ois_swap", "5Y"): 3e5,
    ("ibor_swap", "EURIBOR6M/1Y"): 4e5,
    ("ibor_swap", "EURIBOR6M/10Y"): -5e5,
}


def test_scenario_pnl_of_a_linear_pricer_is_exact() -> None:
    snapshot = small_snapshot()
    report = ScenarioPnL(linear_pricer(WEIGHTS)).compute(
        _StubInstrument(), snapshot, resolve_scenarios()
    )
    assert report.base_pv == pytest.approx(sum(WEIGHTS[q.key] * q.rate for q in snapshot.quotes))
    up = report.result("parallel_up")
    assert up.pnl == pytest.approx(sum(WEIGHTS.values()) * 100 * BP)
    steepener = report.result("steepener")
    expected = sum(
        WEIGHTS[q.key] * SCENARIOS.get("steepener").shift_bp(q.tenor) * BP for q in snapshot.quotes
    )
    assert steepener.pnl == pytest.approx(expected)
    assert steepener.present_value == pytest.approx(report.base_pv + expected)
    assert len(steepener.shifts) == len(snapshot.quotes)
    with pytest.raises(KeyError):
        report.result("unknown")


def test_failed_scenario_is_a_risk_computation_error() -> None:
    snapshot = small_snapshot()

    def fragile(_: Instrument, market: MarketSnapshot) -> float:
        if market.quotes[0].rate != snapshot.quotes[0].rate:
            raise MarketDataError("cannot calibrate", {"pillar": "ON"})
        return 1.0

    with pytest.raises(RiskComputationError) as error:
        ScenarioPnL(fragile).compute(_StubInstrument(), snapshot, resolve_scenarios(["twist"]))
    assert error.value.details["scenario"] == "twist"
    assert error.value.details["pillar"] == "ON"


@pytest.mark.parametrize("maturity", ["2Y", "7Y", "30Y"])
@pytest.mark.parametrize("shift_bp", [-5.0, -1.0, 1.0, 5.0])
def test_small_parallel_scenarios_agree_with_dv01_and_convexity(
    snapshot: MarketSnapshot, shift_bp: float, maturity: str
) -> None:
    """Taylor check (ROADMAP phase 3): PV(dx) - PV ~ DV01 dx + convexity dx^2 / 2."""
    swap = INSTRUMENTS.get("vanilla_irs")(
        {"notional": 1e8, "fixed_rate": 0.025, "direction": "pay_fixed", "maturity": maturity},
        snapshot.valuation_date,
    )
    builder = CurveBuilder()
    engine = engine_for("vanilla_irs")

    def pricer(instrument: Instrument, market: MarketSnapshot) -> float:
        return engine.present_value(instrument, builder.build(market))

    risk = RiskEngine(pricer).compute(
        swap, snapshot, ["parallel_dv01", "convexity"], RiskSettings(bump_size_bp=1.0)
    )
    dv01, convexity = risk.result("parallel_dv01").value, risk.result("convexity").value
    scenario = Scenario("small", (ScenarioPoint("1Y", shift_bp),))
    pnl = ScenarioPnL(pricer).compute(swap, snapshot, [scenario]).result("small").pnl
    taylor = dv01 * shift_bp + 0.5 * convexity * shift_bp**2
    assert pnl == pytest.approx(taylor, rel=TAYLOR_REL)
    # The second-order term matters: first order alone is visibly worse for 5bp.
    if abs(shift_bp) == 5.0:
        assert abs(pnl - dv01 * shift_bp) > abs(pnl - taylor)


# --------------------------------------------------------------------------- coupon accruals
def test_fixed_coupon_accrues_on_the_overlap_only() -> None:
    coupon = FixedCoupon(
        notional=1e6,
        accrual_start=date(2026, 1, 1),
        accrual_end=date(2027, 1, 1),
        payment_date=date(2027, 1, 1),
        day_count=Act360(),
        fixed_rate=0.036,
    )
    env = flat_environment(0.02)
    assert coupon.accrued_amount(env, date(2026, 10, 19), date(2027, 1, 19)) == pytest.approx(
        1e6 * 0.036 * 74 / 360
    )
    assert coupon.accrued_amount(env, date(2027, 1, 1), date(2027, 6, 1)) == 0.0


def test_overnight_accrual_over_the_full_period_equals_the_amount(eur: CurrencyConventions) -> None:
    coupon = OvernightCompoundedCoupon(
        notional=1e7,
        accrual_start=date(2026, 10, 21),
        accrual_end=date(2027, 4, 21),
        payment_date=date(2027, 4, 22),
        day_count=eur.ois.floating_day_count,
        index=eur.overnight_index,
    )
    env = flat_environment(0.0215)
    # Daily compounding of projected overnight forwards = telescoped projection.
    assert coupon.accrued_amount(env, date(2026, 1, 1), date(2028, 1, 1)) == pytest.approx(
        coupon.amount(env), rel=1e-12
    )
    assert coupon.accrued_amount(env, date(2027, 4, 21), date(2028, 1, 1)) == 0.0


def test_overnight_accrual_needs_past_fixings(eur: CurrencyConventions) -> None:
    coupon = OvernightCompoundedCoupon(
        notional=1e7,
        accrual_start=date(2026, 10, 1),
        accrual_end=date(2027, 10, 1),
        payment_date=date(2027, 10, 4),
        day_count=eur.ois.floating_day_count,
        index=eur.overnight_index,
    )
    with pytest.raises(MarketDataError):
        coupon.accrued_amount(flat_environment(0.02), date(2026, 10, 1), date(2026, 10, 30))


# --------------------------------------------------------------------------- carry / roll-down
def flat_ois_snapshot(rate_percent: float) -> MarketSnapshot:
    """Every overnight quote at the same rate: the hand-computable case of ADR-034."""
    quotes: tuple[Quote, ...] = (
        DepositQuote("ON", rate_percent, QuoteUnit.PERCENT),
        *(OisSwapQuote(t, rate_percent, QuoteUnit.PERCENT) for t in ("3M", "6M", "1Y", "2Y")),
    )
    return MarketSnapshot("flat", "EUR", VALUATION_DATE, quotes)


def test_horizon_date_and_unchanged_curve_snapshot(snapshot: MarketSnapshot) -> None:
    end = horizon_date(snapshot, Tenor.parse("3M"))
    assert end == date(2027, 1, 19)
    with pytest.raises(ConventionError):
        horizon_date(snapshot, Tenor(0, TimeUnit.MONTHS))

    rolled, assumed = unchanged_curve_snapshot(snapshot, end)
    assert rolled.valuation_date == end
    assert rolled.quotes == snapshot.quotes
    deposits = {q.curve_label: q.rate for q in snapshot.quotes if "deposit" in q.instrument_type}
    assert assumed == {
        "ESTR": deposits["OIS"],
        "EURIBOR6M": deposits["EURIBOR6M"],
        "EURIBOR3M": deposits["EURIBOR3M"],
    }
    fixings = rolled.fixings_by_index()
    target = TargetCalendar()
    for day in target.business_days(snapshot.valuation_date, date(2027, 1, 18)):
        assert fixings["ESTR"][day] == deposits["OIS"]
        assert day in fixings["EURIBOR6M"]
    # Published fixings are kept (the Euribor fixing of the valuation date is in the snapshot).
    published = snapshot.fixings_by_index()["EURIBOR6M"][snapshot.valuation_date]
    assert fixings["EURIBOR6M"][snapshot.valuation_date] == published
    assert date(2027, 1, 19) not in fixings["ESTR"]  # no fixing on the new valuation date


def test_unchanged_curve_snapshot_needs_the_overnight_deposit() -> None:
    quotes = (OisSwapQuote("1Y", 2.0, QuoteUnit.PERCENT),)
    market = MarketSnapshot("no_deposit", "EUR", VALUATION_DATE, quotes)
    with pytest.raises(MarketDataError):
        unchanged_curve_snapshot(market, date(2027, 1, 19))


def test_carry_of_a_spot_ois_on_a_flat_curve_is_computable_by_hand() -> None:
    """Payer 1Y OIS at 3% on a flat 2% curve, 3M horizon.

    Over [spot, horizon) = [2026-10-21, 2027-01-19) the trade pays 3% simple on 90 days and
    receives 2% compounded daily on TARGET overnight periods (the unchanged curve keeps the
    overnight rate at 2%). Nothing is paid before the horizon.
    """
    market = flat_ois_snapshot(2.0)
    notional, fixed, overnight = 1e8, 0.03, 0.02
    swap = INSTRUMENTS.get("ois_swap")(
        {"notional": notional, "fixed_rate": fixed, "direction": "pay_fixed", "maturity": "1Y"},
        VALUATION_DATE,
    )
    result = CarryRolldown(PipelineValuer()).compute(swap, market, Tenor.parse("3M"))

    spot, end = date(2026, 10, 21), date(2027, 1, 19)
    fixed_accrual = notional * fixed * (end - spot).days / 360
    days = TargetCalendar().business_days(spot, end)
    growth = 1.0
    for start_day, end_day in itertools.pairwise(days):
        growth *= 1 + overnight * (end_day - start_day).days / 360
    floating_accrual = notional * (growth - 1)

    assert fixed_accrual == pytest.approx(750_000.0)
    assert floating_accrual == pytest.approx(500_000.0, rel=5e-3)
    assert result.carry == pytest.approx(floating_accrual - fixed_accrual, rel=1e-12)
    assert result.horizon_date == end
    assert result.cashflows == 0.0
    assert result.assumed_fixings == {"ESTR": overnight}
    assert result.total == pytest.approx(result.horizon_pv - result.base_pv)
    assert result.rolldown == pytest.approx(result.total - result.carry)
    # On a flat curve "unchanged curve" and "forwards realised" nearly coincide: the total P&L is
    # close to zero (the negative carry was already in today's PV) and the roll-down, here the
    # pull of the off-market swap's clean value towards par, offsets the carry.
    assert abs(result.total) < 0.02 * abs(result.carry)
    assert result.rolldown == pytest.approx(-result.carry, rel=0.02)


def test_carry_includes_coupons_paid_before_the_horizon(snapshot: MarketSnapshot) -> None:
    """A seasoned 1Y OIS paying on 2027-01-18: inside a 3M horizon from 2026-10-19."""
    swap = INSTRUMENTS.get("ois_swap")(
        {
            "notional": 1e7,
            "fixed_rate": 0.02,
            "direction": "receive_fixed",
            "maturity": "1Y",
            "start_date": "2026-01-15",
        },
        snapshot.valuation_date,
    )
    valuer = PipelineValuer()
    result = CarryRolldown(valuer).compute(swap, snapshot, Tenor.parse("3M"))
    assert result.horizon_pv == 0.0  # every cashflow paid by the horizon
    rolled, _ = unchanged_curve_snapshot(snapshot, result.horizon_date)
    rolled_env = valuer.builder.build(rolled)
    assert isinstance(swap, Swap)
    expected_cash = sum(
        leg.direction.sign * cf.amount(rolled_env) for leg in swap.legs for cf in leg.cashflows
    )
    assert result.cashflows == pytest.approx(expected_cash)
    assert result.total == pytest.approx(result.cashflows - result.base_pv)


def test_carry_of_a_vanilla_irs_uses_euribor_assumptions(snapshot: MarketSnapshot) -> None:
    swap = INSTRUMENTS.get("vanilla_irs")(
        {"notional": 1e7, "fixed_rate": 0.025, "direction": "pay_fixed", "maturity": "5Y"},
        snapshot.valuation_date,
    )
    result = CarryRolldown(PipelineValuer()).compute(swap, snapshot, Tenor.parse("6M"))
    assert set(result.assumed_fixings) == {"ESTR", "EURIBOR6M", "EURIBOR3M"}
    # Paying 2.5% against Euribor 6M around 2.0%: negative carry over six months.
    assert -1e7 * 0.006 * 0.5 < result.carry < 0.0
    assert result.total == pytest.approx(result.carry + result.rolldown)


def test_carry_failure_at_the_horizon_is_a_risk_computation_error(
    snapshot: MarketSnapshot,
) -> None:
    class Failing(PipelineValuer):
        def accrued_interest(
            self, instrument: Instrument, snapshot: MarketSnapshot, start: date, end: date
        ) -> float:
            raise MarketDataError("missing fixing", {"index": "X"})

    swap = INSTRUMENTS.get("ois_swap")(
        {"notional": 1e7, "fixed_rate": 0.02, "direction": "pay_fixed", "maturity": "2Y"},
        snapshot.valuation_date,
    )
    with pytest.raises(RiskComputationError) as error:
        CarryRolldown(Failing()).compute(swap, snapshot, Tenor.parse("1M"))
    assert error.value.details["horizon"] == "1M"


def test_engines_without_carry_support_refuse_it() -> None:
    class PvOnlyEngine(PricingEngine):
        method: ClassVar[str] = "pv_only"
        supported_instruments: ClassVar[tuple[str, ...]] = ("stub",)

        def price(self, instrument: Instrument, env: MarketEnvironment) -> PricingResult:
            raise NotImplementedError

    engine, env = PvOnlyEngine(), flat_environment(0.02)
    with pytest.raises(UnsupportedInstrumentError):
        engine.cashflows_paid(_StubInstrument(), env, VALUATION_DATE, date(2027, 1, 19))
    with pytest.raises(UnsupportedInstrumentError):
        engine.accrued_interest(_StubInstrument(), env, VALUATION_DATE, date(2027, 1, 19))
    with pytest.raises(NotImplementedError):
        engine.present_value(_StubInstrument(), env)
