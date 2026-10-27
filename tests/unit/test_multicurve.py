"""Multi-curve bootstrap: OIS discounting, Euribor projection, memoisation and horizon."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from rate_derivatives_pricer.core.conventions.currency import CurrencyConventions
from rate_derivatives_pricer.core.conventions.tenor import Tenor
from rate_derivatives_pricer.core.engines.base import engine_for
from rate_derivatives_pricer.core.engines.discounting import DiscountingSwapEngine
from rate_derivatives_pricer.core.errors import ConventionError, MarketDataError
from rate_derivatives_pricer.core.instruments.factories import build_spot_vanilla_irs
from rate_derivatives_pricer.core.instruments.swaps import SwapDirection
from rate_derivatives_pricer.core.market.bootstrap import CurveBuilder, CurveBuildOptions
from rate_derivatives_pricer.core.market.environment import MarketEnvironment
from rate_derivatives_pricer.core.market.quotes import (
    DepositQuote,
    IborDepositQuote,
    IborQuote,
    IborSwapQuote,
    OisSwapQuote,
)
from rate_derivatives_pricer.core.market.snapshot import Fixing, MarketSnapshot
from rate_derivatives_pricer.core.risk import Bumper, BumpTarget
from tests.unit.helpers import VALUATION_DATE

CURVE_SAMPLE_STEP_DAYS = 37


def _overnight_only(snapshot: MarketSnapshot) -> MarketSnapshot:
    return snapshot.with_quotes(tuple(q for q in snapshot.quotes if not isinstance(q, IborQuote)))


def _sample_dates(horizon_years: int) -> list[date]:
    return [
        VALUATION_DATE + timedelta(days=k)
        for k in range(0, horizon_years * 365, CURVE_SAMPLE_STEP_DAYS)
    ]


def test_environment_holds_one_discount_curve_and_one_curve_per_index(
    env: MarketEnvironment, snapshot: MarketSnapshot
) -> None:
    assert [r.curve_name for r in env.calibration] == ["EUR-ESTR", "EUR-EURIBOR3M", "EUR-EURIBOR6M"]
    assert [r.is_discount_curve for r in env.calibration] == [True, False, False]
    assert set(env.projection_curves) == {"ESTR", "EURIBOR3M", "EURIBOR6M"}
    assert env.projection_curve("ESTR") is env.discount_curve
    assert env.projection_curve("EURIBOR6M") is not env.discount_curve
    assert env.fixing("EURIBOR6M", date(2026, 10, 16)) is not None
    for report in env.calibration[1:]:
        count = sum(
            isinstance(q, IborQuote) and q.index_name == report.index_name for q in snapshot.quotes
        )
        assert len(report.pillars) == count
        assert report.max_abs_residual < 1e-13


def test_discount_curve_ignores_the_ibor_quotes(
    env: MarketEnvironment, snapshot: MarketSnapshot
) -> None:
    overnight_only = CurveBuilder().build(_overnight_only(snapshot))
    dates = _sample_dates(30)
    assert list(env.discount_curve.discount_factors(dates)) == list(
        overnight_only.discount_curve.discount_factors(dates)
    )


def test_ibor_curve_sits_below_the_discount_curve_by_the_basis(env: MarketEnvironment) -> None:
    # Euribor 6M quotes are ~15-18bp above OIS: its projection curve discounts faster.
    horizon = date(2031, 10, 21)
    assert env.projection_curve("EURIBOR6M").discount_factor(horizon) < (
        env.discount_curve.discount_factor(horizon)
    )
    assert env.projection_curve("EURIBOR3M").discount_factor(horizon) > (
        env.projection_curve("EURIBOR6M").discount_factor(horizon)
    )


def test_every_ibor_quote_is_repriced_by_the_calibrated_curves(
    env: MarketEnvironment, snapshot: MarketSnapshot, eur: CurrencyConventions
) -> None:
    engine = DiscountingSwapEngine()
    for quote in snapshot.quotes:
        if isinstance(quote, IborSwapQuote):
            swap = build_spot_vanilla_irs(
                eur,
                eur.ibor_index(quote.index_name),
                VALUATION_DATE,
                quote.tenor,
                1.0,
                quote.rate,
                SwapDirection.PAY_FIXED,
            )
            assert engine.par_rate(swap, env) == pytest.approx(quote.rate, abs=1e-13), quote.key
        elif isinstance(quote, IborDepositQuote):
            report = next(r for r in env.calibration if r.index_name == quote.index_name)
            pillar = next(p for p in report.pillars if p.instrument_type == "ibor_deposit")
            assert pillar.implied_quote == pytest.approx(quote.rate, abs=1e-13)


def test_quote_pillars_are_the_swap_end_dates(env: MarketEnvironment) -> None:
    pillars = {p.label: p.pillar_date for p in env.calibration[2].pillars}  # EURIBOR6M
    assert pillars["6M"] == date(2027, 4, 21)
    assert pillars["1Y"] == date(2027, 10, 21)
    assert pillars["2Y"] == date(2028, 10, 23)  # 2028-10-21 is a Saturday: Modified Following


def test_calibration_does_not_see_the_fixings(snapshot: MarketSnapshot) -> None:
    # A spot-starting swap's first fixing is the valuation date's: it must stay projected during
    # the bootstrap, so removing or changing the fixings leaves the curves untouched (ADR-027).
    changed = replace(
        snapshot,
        fixings=tuple(
            Fixing(
                f.index, f.fixing_date, f.rate + 0.01 if f.index.startswith("EURIBOR") else f.rate
            )
            for f in snapshot.fixings
        ),
    )
    stripped = replace(snapshot, fixings=())
    reference = CurveBuilder().build(snapshot)
    dates = _sample_dates(30)
    for other in (CurveBuilder().build(changed), CurveBuilder().build(stripped)):
        for name in ("ESTR", "EURIBOR6M", "EURIBOR3M"):
            assert list(other.projection_curve(name).discount_factors(dates)) == list(
                reference.projection_curve(name).discount_factors(dates)
            )


def test_ibor_deposit_tenor_must_match_its_index(snapshot: MarketSnapshot) -> None:
    quotes = (
        *_overnight_only(snapshot).quotes,
        IborDepositQuote("3M", 0.02, index_name="EURIBOR6M"),
    )
    with pytest.raises(MarketDataError, match="tenor of its index"):
        CurveBuilder().build(snapshot.with_quotes(quotes))


def test_unknown_index_is_a_convention_error(snapshot: MarketSnapshot) -> None:
    quotes = (*_overnight_only(snapshot).quotes, IborSwapQuote("2Y", 0.02, index_name="EURIBOR1M"))
    with pytest.raises(ConventionError):
        CurveBuilder().build(snapshot.with_quotes(quotes))


def test_snapshot_without_overnight_quotes_is_rejected() -> None:
    snapshot = MarketSnapshot(
        "ibor-only", "EUR", VALUATION_DATE, (IborSwapQuote("2Y", 0.02, index_name="EURIBOR6M"),)
    )
    with pytest.raises(MarketDataError, match="no overnight"):
        CurveBuilder().build(snapshot)


def test_linear_zero_interpolation_also_calibrates_the_ibor_curves(
    snapshot: MarketSnapshot,
) -> None:
    env = CurveBuilder(CurveBuildOptions(interpolation="linear_zero")).build(snapshot)
    assert all(report.max_abs_residual < 1e-12 for report in env.calibration)


def test_newton_solver_agrees_with_brent(snapshot: MarketSnapshot, env: MarketEnvironment) -> None:
    newton = CurveBuilder(CurveBuildOptions(solver="newton")).build(snapshot)
    dates = _sample_dates(30)
    for name in ("ESTR", "EURIBOR6M"):
        for ours, reference in zip(
            newton.projection_curve(name).discount_factors(dates),
            env.projection_curve(name).discount_factors(dates),
            strict=True,
        ):
            assert ours == pytest.approx(reference, abs=1e-12)


# --------------------------------------------------------------------------- memo and horizon
def test_a_rebuild_with_the_same_builder_is_identical_and_solves_nothing_new(
    snapshot: MarketSnapshot,
) -> None:
    builder = CurveBuilder()
    first = builder.build(snapshot)
    remembered = len(builder._pillar_memo)
    second = builder.build(snapshot)
    assert len(builder._pillar_memo) == remembered
    dates = _sample_dates(30)
    for name in ("ESTR", "EURIBOR6M", "EURIBOR3M"):
        assert list(second.projection_curve(name).discount_factors(dates)) == list(
            first.projection_curve(name).discount_factors(dates)
        )


@pytest.mark.parametrize(
    "target",
    [
        BumpTarget(),
        BumpTarget(curves=frozenset({"OIS"})),
        BumpTarget(curves=frozenset({"EURIBOR6M"})),
        BumpTarget(quote_keys=frozenset({("ois_swap", "3Y")})),
        BumpTarget(quote_keys=frozenset({("ibor_swap", "EURIBOR3M/7Y")})),
        BumpTarget(quote_keys=frozenset({("ibor_deposit", "EURIBOR6M/6M")})),
    ],
)
def test_a_memoised_bumped_rebuild_equals_a_fresh_one_bit_for_bit(
    snapshot: MarketSnapshot, target: BumpTarget
) -> None:
    bumped = Bumper().apply(snapshot, target, 1.0)
    warm = CurveBuilder()
    warm.build(snapshot)  # fills the memo with the base pillars
    reused = warm.build(bumped)
    fresh = CurveBuilder().build(bumped)
    dates = _sample_dates(30)
    for name in ("ESTR", "EURIBOR6M", "EURIBOR3M"):
        assert list(reused.projection_curve(name).discount_factors(dates)) == list(
            fresh.projection_curve(name).discount_factors(dates)
        ), name
    assert list(reused.discount_curve.discount_factors(dates)) == list(
        fresh.discount_curve.discount_factors(dates)
    )


def test_a_horizon_builds_fewer_pillars_with_identical_values_up_to_it(
    snapshot: MarketSnapshot, env: MarketEnvironment, eur: CurrencyConventions
) -> None:
    swap = build_spot_vanilla_irs(
        eur,
        eur.ibor_index(),
        VALUATION_DATE,
        Tenor.parse("5Y"),
        1e7,
        0.025,
        SwapDirection.PAY_FIXED,
    )
    horizon = swap.last_cashflow_date
    short = CurveBuilder().build(snapshot, horizon=horizon)
    for full, truncated in zip(env.calibration, short.calibration, strict=True):
        assert len(truncated.pillars) < len(full.pillars)
        assert truncated.pillars[-1].pillar_date >= horizon or truncated.index_name == "EURIBOR3M"
    # The 5Y swap is priced to the last bit like on the complete curves.
    engine = engine_for("vanilla_irs")
    assert engine.present_value(swap, short) == engine.present_value(swap, env)
    assert engine.price(swap, short).par_rate == engine.price(swap, env).par_rate


def test_the_overnight_curve_reaches_as_far_as_the_ibor_pillars(
    snapshot: MarketSnapshot,
) -> None:
    # Horizon just after a pillar: the Ibor stage stops at the next pillar and the overnight stage
    # must reach it too, since the Ibor swaps are discounted on it.
    short = CurveBuilder().build(snapshot, horizon=date(2028, 10, 24))
    last_ibor = max(r.pillars[-1].pillar_date for r in short.calibration[1:])
    assert short.calibration[0].pillars[-1].pillar_date >= last_ibor


def test_an_overnight_only_snapshot_still_builds_a_single_curve(snapshot: MarketSnapshot) -> None:
    env = CurveBuilder().build(_overnight_only(snapshot))
    assert len(env.calibration) == 1
    assert set(env.projection_curves) == {"ESTR"}
    # Quotes of mixed kinds (deposit + OIS) only.
    assert {type(q) for q in _overnight_only(snapshot).quotes} == {DepositQuote, OisSwapQuote}
