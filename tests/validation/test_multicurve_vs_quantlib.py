"""Euribor curves, vanilla IRS and bump-and-reprice risk: comparison with QuantLib (phase 2)."""

from __future__ import annotations

import itertools
from datetime import date, timedelta

import pytest
import QuantLib as ql

from rate_derivatives_pricer.core.engines.base import engine_for
from rate_derivatives_pricer.core.instruments.swaps import VanillaIrs
from rate_derivatives_pricer.core.market.bootstrap import CurveBuilder, CurveBuildOptions
from rate_derivatives_pricer.core.market.environment import MarketEnvironment
from rate_derivatives_pricer.core.market.snapshot import MarketSnapshot
from rate_derivatives_pricer.core.registry import INSTRUMENTS
from rate_derivatives_pricer.core.risk import (
    Bumper,
    BumpMode,
    BumpSpec,
    BumpTarget,
    RiskEngine,
    RiskSettings,
)
from tests.validation.market_grid import market_grid, reference_snapshot
from tests.validation.quantlib_oracle import (
    build_estr_curve,
    build_euribor_curve,
    evaluation_date,
    freeze_log_linear,
    from_ql,
    make_irs,
    to_ql,
)
from tests.validation.tolerances import (
    DISCOUNT_FACTOR_ABS,
    DV01_REL,
    KEY_RATE_SUM_REL,
    SWAP_REL,
)

pytestmark = pytest.mark.validation

INDICES = ["EURIBOR6M", "EURIBOR3M"]
INTERPOLATIONS = ["log_linear_df", "linear_zero"]
GRID = market_grid()
CURVE_SAMPLE_STEP_DAYS = 11
CURVE_SAMPLE_HORIZON_DAYS = 40 * 365
BASIS_POINT = 1e-4


def _ids(snapshot: MarketSnapshot) -> str:
    return snapshot.name


# --------------------------------------------------------------------------- curves
@pytest.mark.parametrize("index_name", INDICES)
@pytest.mark.parametrize("interpolation", INTERPOLATIONS)
@pytest.mark.parametrize("snapshot", GRID, ids=_ids)
def test_euribor_curves_match_quantlib(
    snapshot: MarketSnapshot, interpolation: str, index_name: str
) -> None:
    env = CurveBuilder(CurveBuildOptions(interpolation=interpolation)).build(snapshot)
    report = next(r for r in env.calibration if r.index_name == index_name)
    ours = env.projection_curve(index_name)
    with evaluation_date(snapshot.valuation_date):
        ois = build_estr_curve(snapshot, interpolation)
        reference = build_euribor_curve(snapshot, index_name, ois, interpolation)
        assert [from_ql(d) for d in reference.dates()][1:] == [
            p.pillar_date for p in report.pillars
        ]
        for pillar in report.pillars:
            assert pillar.discount_factor == pytest.approx(
                reference.discount(to_ql(pillar.pillar_date)), abs=DISCOUNT_FACTOR_ABS
            ), pillar.label
        samples = [
            snapshot.valuation_date + timedelta(days=k)
            for k in range(0, CURVE_SAMPLE_HORIZON_DAYS, CURVE_SAMPLE_STEP_DAYS)
        ]
        for d, discount_factor in zip(samples, ours.discount_factors(samples), strict=True):
            assert discount_factor == pytest.approx(
                reference.discount(to_ql(d)), abs=DISCOUNT_FACTOR_ABS
            ), d


# --------------------------------------------------------------------------- swaps
SPOT_MATURITIES = ["1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "15Y", "20Y", "30Y"]
DIRECTIONS = ["pay_fixed", "receive_fixed"]
FIXED_RATES = [-0.005, 0.0225, 0.04]
NOTIONAL = 1e7
# Seasoned and forward-starting swaps: (start date, maturity). Starts before the valuation date use
# the Euribor fixings of the snapshot; 18M and 30M exercise the short first fixed period (stub).
SEASONED = [
    (date(2026, 1, 15), "1Y"),
    (date(2026, 3, 16), "2Y"),
    (date(2026, 8, 31), "18M"),
    (date(2026, 10, 19), "6M"),
    (date(2026, 10, 1), "10Y"),
    (date(2027, 10, 21), "5Y"),
]


def _prepare(snapshot: MarketSnapshot) -> tuple[MarketEnvironment, dict[str, dict[date, float]]]:
    env = CurveBuilder().build(snapshot)
    return env, {k: dict(v) for k, v in snapshot.fixings_by_index().items()}


def _our_irs(
    snapshot: MarketSnapshot,
    env: MarketEnvironment,
    index_name: str,
    direction: str,
    fixed_rate: float,
    maturity: str,
    start: date | None = None,
) -> tuple[VanillaIrs, float, float, float]:
    params: dict[str, object] = {
        "notional": NOTIONAL,
        "fixed_rate": fixed_rate,
        "direction": direction,
        "maturity": maturity,
        "index": index_name,
    }
    if start is not None:
        params["start_date"] = start.isoformat()
    swap = INSTRUMENTS.get("vanilla_irs")(params, snapshot.valuation_date)
    assert isinstance(swap, VanillaIrs)
    result = engine_for("vanilla_irs").price(swap, env)
    return swap, result.present_value, result.par_rate, result.annuity


def _check_against_quantlib(
    snapshot: MarketSnapshot,
    index_name: str,
    direction: str,
    fixed_rate: float,
    maturity: str,
    start: date | None,
    fixings: dict[str, dict[date, float]],
    env: MarketEnvironment,
) -> None:
    _, pv, par_rate, annuity = _our_irs(
        snapshot, env, index_name, direction, fixed_rate, maturity, start
    )
    with evaluation_date(snapshot.valuation_date):
        ois = freeze_log_linear(build_estr_curve(snapshot))
        projection = freeze_log_linear(build_euribor_curve(snapshot, index_name, ois))
        reference = make_irs(
            projection,
            ois,
            fixings.get(index_name, {}),
            index_name,
            NOTIONAL,
            fixed_rate,
            direction == "pay_fixed",
            maturity,
            start,
        )
        label = f"{snapshot.name} {index_name} {direction} {maturity} {fixed_rate}"
        assert pv / NOTIONAL == pytest.approx(reference.NPV() / NOTIONAL, abs=SWAP_REL), label
        assert par_rate == pytest.approx(reference.fairRate(), rel=SWAP_REL), label
        assert annuity == pytest.approx(abs(reference.fixedLegBPS()) / BASIS_POINT, rel=SWAP_REL), (
            label
        )


@pytest.mark.parametrize("index_name", INDICES)
@pytest.mark.parametrize("snapshot", GRID, ids=_ids)
def test_spot_irs_match_quantlib(snapshot: MarketSnapshot, index_name: str) -> None:
    env, fixings = _prepare(snapshot)
    for maturity, direction, fixed_rate in itertools.product(
        SPOT_MATURITIES, DIRECTIONS, FIXED_RATES
    ):
        _check_against_quantlib(
            snapshot, index_name, direction, fixed_rate, maturity, None, fixings, env
        )


@pytest.mark.parametrize("index_name", INDICES)
@pytest.mark.parametrize(("start", "maturity"), SEASONED)
def test_seasoned_and_forward_irs_match_quantlib(
    index_name: str, start: date, maturity: str
) -> None:
    snapshot = reference_snapshot()
    env, fixings = _prepare(snapshot)
    for direction, fixed_rate in itertools.product(DIRECTIONS, FIXED_RATES):
        _check_against_quantlib(
            snapshot, index_name, direction, fixed_rate, maturity, start, fixings, env
        )


def test_irs_schedules_match_quantlib_date_by_date() -> None:
    snapshot = reference_snapshot()
    env, fixings = _prepare(snapshot)
    for index_name, maturity in itertools.product(INDICES, [*SPOT_MATURITIES, "18M", "30M"]):
        swap, *_ = _our_irs(snapshot, env, index_name, "pay_fixed", 0.02, maturity)
        with evaluation_date(snapshot.valuation_date):
            ois = freeze_log_linear(build_estr_curve(snapshot))
            projection = freeze_log_linear(build_euribor_curve(snapshot, index_name, ois))
            reference = make_irs(
                projection, ois, fixings[index_name], index_name, NOTIONAL, 0.02, True, maturity
            )
        fixed = [
            (from_ql(c.accrualStartDate()), from_ql(c.accrualEndDate()), from_ql(c.date()))
            for c in map(ql.as_fixed_rate_coupon, reference.fixedLeg())
        ]
        floating = [
            (from_ql(c.accrualStartDate()), from_ql(c.accrualEndDate()), from_ql(c.date()))
            for c in map(ql.as_floating_rate_coupon, reference.floatingLeg())
        ]
        ours_fixed = [(c.accrual_start, c.accrual_end, c.payment_date) for c in swap.fixed_coupons]
        ours_floating = [
            (c.accrual_start, c.accrual_end, c.payment_date) for c in swap.ibor_coupons
        ]
        assert ours_fixed == fixed, (index_name, maturity)
        assert ours_floating == floating, (index_name, maturity)
        fixing_dates = [
            from_ql(c.fixingDate())
            for c in map(ql.as_floating_rate_coupon, reference.floatingLeg())
        ]
        assert [c.fixing_date for c in swap.ibor_coupons] == fixing_dates


# --------------------------------------------------------------------------- risk
RISK_TRADES = [
    ("EURIBOR6M", "pay_fixed", "5Y", 0.025),
    ("EURIBOR3M", "receive_fixed", "10Y", 0.024),
    ("EURIBOR6M", "pay_fixed", "2Y", 0.02),
]


def _quantlib_pv(
    snapshot: MarketSnapshot, index_name: str, direction: str, maturity: str, fixed_rate: float
) -> float:
    """PV in QuantLib on a (bumped) snapshot: curves rebuilt from the quotes, then frozen."""
    fixings = {d: v for d, v in snapshot.fixings_by_index().get(index_name, {}).items()}
    with evaluation_date(snapshot.valuation_date):
        ois = freeze_log_linear(build_estr_curve(snapshot))
        projection = freeze_log_linear(build_euribor_curve(snapshot, index_name, ois))
        swap = make_irs(
            projection,
            ois,
            fixings,
            index_name,
            NOTIONAL,
            fixed_rate,
            direction == "pay_fixed",
            maturity,
        )
        return float(swap.NPV())


def _our_risk(
    snapshot: MarketSnapshot,
    index_name: str,
    direction: str,
    maturity: str,
    fixed_rate: float,
    measures: list[str],
):
    builder = CurveBuilder()
    engine = engine_for("vanilla_irs")
    swap = INSTRUMENTS.get("vanilla_irs")(
        {
            "notional": NOTIONAL,
            "fixed_rate": fixed_rate,
            "direction": direction,
            "maturity": maturity,
            "index": index_name,
        },
        snapshot.valuation_date,
    )
    assert isinstance(swap, VanillaIrs)

    def pricer(instrument, bumped):
        # The truncated build the service uses: validated here against QuantLib's full curves.
        return engine.present_value(
            instrument, builder.build(bumped, horizon=instrument.last_cashflow_date)
        )

    return RiskEngine(pricer).compute(swap, snapshot, measures, RiskSettings())


@pytest.mark.parametrize(("index_name", "direction", "maturity", "fixed_rate"), RISK_TRADES)
def test_dv01_per_bucket_matches_quantlib_finite_differences(
    index_name: str, direction: str, maturity: str, fixed_rate: float
) -> None:
    snapshot = reference_snapshot()
    report = _our_risk(
        snapshot, index_name, direction, maturity, fixed_rate, ["parallel_dv01", "key_rate_dv01"]
    )
    bumper = Bumper()
    scale = abs(report.result("parallel_dv01").value)
    assert scale > 0.0
    expected = {}
    for quote in snapshot.quotes:
        target = BumpTarget(quote_keys=frozenset({quote.key}))
        up = _quantlib_pv(
            bumper.apply(snapshot, target, 1.0), index_name, direction, maturity, fixed_rate
        )
        down = _quantlib_pv(
            bumper.apply(snapshot, target, -1.0), index_name, direction, maturity, fixed_rate
        )
        expected[(quote.curve_label, quote.tenor_label, quote.instrument_type)] = (up - down) / 2.0
    for bucket in report.result("key_rate_dv01").buckets:
        reference = expected[(bucket.curve, bucket.label, bucket.instrument_type)]
        assert abs(bucket.value - reference) <= DV01_REL * scale, (bucket, reference)


@pytest.mark.parametrize(("index_name", "direction", "maturity", "fixed_rate"), RISK_TRADES)
def test_parallel_dv01_and_convexity_match_quantlib(
    index_name: str, direction: str, maturity: str, fixed_rate: float
) -> None:
    snapshot = reference_snapshot()
    report = _our_risk(
        snapshot, index_name, direction, maturity, fixed_rate, ["parallel_dv01", "convexity"]
    )
    bumper = Bumper()
    pvs = {
        shift: _quantlib_pv(
            bumper.apply(snapshot, BumpTarget(), shift), index_name, direction, maturity, fixed_rate
        )
        for shift in (-1.0, 0.0, 1.0)
    }
    dv01 = (pvs[1.0] - pvs[-1.0]) / 2.0
    convexity = pvs[1.0] - 2.0 * pvs[0.0] + pvs[-1.0]
    scale = abs(dv01)
    assert abs(report.result("parallel_dv01").value - dv01) <= DV01_REL * scale
    # The convexity is a second difference of PVs of order 1e6: compare it to the QuantLib one with
    # the absolute noise of that difference (a few 1e-6 of the DV01 per bp^2 is far below it).
    assert abs(report.result("convexity").value - convexity) <= DV01_REL * scale


@pytest.mark.parametrize(("index_name", "direction", "maturity", "fixed_rate"), RISK_TRADES)
def test_bucket_dv01s_add_up_to_the_parallel_dv01(
    index_name: str, direction: str, maturity: str, fixed_rate: float
) -> None:
    report = _our_risk(
        reference_snapshot(),
        index_name,
        direction,
        maturity,
        fixed_rate,
        ["parallel_dv01", "key_rate_dv01", "curve_dv01"],
    )
    parallel = report.result("parallel_dv01").value
    for name in ("key_rate_dv01", "curve_dv01"):
        total = sum(bucket.value for bucket in report.result(name).buckets)
        assert abs(total - parallel) <= KEY_RATE_SUM_REL * abs(parallel), name


def test_one_sided_modes_agree_with_the_central_difference() -> None:
    snapshot = reference_snapshot()
    builder = CurveBuilder()
    engine = engine_for("vanilla_irs")
    swap = INSTRUMENTS.get("vanilla_irs")(
        {"notional": NOTIONAL, "fixed_rate": 0.025, "direction": "pay_fixed", "maturity": "5Y"},
        snapshot.valuation_date,
    )

    def pricer(instrument, bumped):
        return engine.present_value(instrument, builder.build(bumped))

    values = {
        mode: RiskEngine(pricer)
        .compute(swap, snapshot, ["parallel_dv01"], RiskSettings(1.0, mode))
        .result("parallel_dv01")
        .value
        for mode in BumpMode
    }
    spec = BumpSpec(BumpTarget(), 1.0)
    assert spec.shifts_bp == (1.0, -1.0)
    # Forward and backward differences bracket the central one (convexity is small but non-zero).
    assert values[BumpMode.CENTRAL] == pytest.approx(values[BumpMode.UP], rel=1e-3)
    assert values[BumpMode.CENTRAL] == pytest.approx(values[BumpMode.DOWN], rel=1e-3)
