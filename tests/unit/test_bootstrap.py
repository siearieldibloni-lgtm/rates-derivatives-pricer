from __future__ import annotations

import logging
from datetime import date

import pytest

from rate_derivatives_pricer.core.conventions.currency import CurrencyConventions
from rate_derivatives_pricer.core.errors import CalibrationError, ConventionError, MarketDataError
from rate_derivatives_pricer.core.market.bootstrap import CurveBuilder, CurveBuildOptions
from rate_derivatives_pricer.core.market.calibration import (
    DepositCalibration,
    OisCalibration,
    calibration_instrument_for,
)
from rate_derivatives_pricer.core.market.environment import MarketEnvironment
from rate_derivatives_pricer.core.market.quotes import (
    DepositQuote,
    IborQuote,
    OisSwapQuote,
    Quote,
)
from rate_derivatives_pricer.core.market.snapshot import MarketSnapshot
from tests.unit.helpers import VALUATION_DATE


def _overnight_quotes(snapshot: MarketSnapshot) -> list[Quote]:
    return [q for q in snapshot.quotes if not isinstance(q, IborQuote)]


def test_reprices_every_calibration_quote(env: MarketEnvironment, snapshot: MarketSnapshot) -> None:
    report = env.calibration[0]
    assert report.curve_name == "EUR-ESTR"
    assert report.interpolation == "log_linear_df"
    assert report.solver == "brent"
    assert len(report.pillars) == len(_overnight_quotes(snapshot))
    assert report.max_abs_residual < 1e-13
    assert 0 < report.max_iterations < 20
    for pillar in report.pillars:
        assert pillar.implied_quote == pytest.approx(pillar.market_quote, abs=1e-13)
    assert [p.pillar_date for p in report.pillars] == sorted(p.pillar_date for p in report.pillars)


def test_mono_curve_environment(env: MarketEnvironment) -> None:
    assert env.projection_curve("ESTR") is env.discount_curve
    assert env.fixing("ESTR", date(2026, 10, 16)) is not None
    assert env.valuation_date == VALUATION_DATE


@pytest.mark.parametrize(
    ("interpolation", "solver"),
    [("linear_zero", "brent"), ("log_linear_df", "newton"), ("linear_zero", "newton")],
)
def test_other_options_also_reprice_the_quotes(
    snapshot: MarketSnapshot, interpolation: str, solver: str
) -> None:
    options = CurveBuildOptions(interpolation=interpolation, solver=solver)
    builder = CurveBuilder(options)
    assert builder.options == options
    report = builder.build(snapshot).calibration[0]
    assert report.max_abs_residual < 1e-12


def test_solvers_agree_on_discount_factors(
    snapshot: MarketSnapshot, env: MarketEnvironment
) -> None:
    newton = CurveBuilder(CurveBuildOptions(solver="newton")).build(snapshot)
    for ours, theirs in zip(env.calibration[0].pillars, newton.calibration[0].pillars, strict=True):
        assert ours.discount_factor == pytest.approx(theirs.discount_factor, abs=1e-13)


def test_unknown_options() -> None:
    with pytest.raises(ConventionError):
        CurveBuilder(CurveBuildOptions(interpolation="cubic_spline"))
    with pytest.raises(ConventionError, match="solver"):
        CurveBuilder(CurveBuildOptions(solver="bisection"))


def test_duplicate_pillar_dates_are_rejected(snapshot: MarketSnapshot) -> None:
    # 12M and 1Y define the same swap, hence the same pillar.
    quotes = (*snapshot.quotes, OisSwapQuote("12M", 1.885))
    with pytest.raises(MarketDataError, match="same pillar"):
        CurveBuilder().build(snapshot.with_quotes(quotes))


def test_unreachable_quote_raises_calibration_error(snapshot: MarketSnapshot) -> None:
    # A 1W rate of 5000% is outside the solver's bracket.
    quotes = tuple(
        OisSwapQuote("1W", 5000.0, q.unit) if q.tenor_label == "1W" else q for q in snapshot.quotes
    )
    with pytest.raises(CalibrationError) as error:
        CurveBuilder().build(snapshot.with_quotes(quotes))
    assert error.value.pillar == "1W"
    assert error.value.details["pillar"] == "1W"


def test_calibration_instruments(eur: CurrencyConventions) -> None:
    deposit = calibration_instrument_for(DepositQuote("ON", 0.019), eur, VALUATION_DATE)
    assert isinstance(deposit, DepositCalibration)
    assert (deposit.start_date, deposit.pillar_date) == (VALUATION_DATE, date(2026, 10, 20))
    assert deposit.label == "ON"
    ois = calibration_instrument_for(OisSwapQuote("1Y", 0.019), eur, VALUATION_DATE)
    assert isinstance(ois, OisCalibration)
    # Spot 21 Oct 2026, maturity 21 Oct 2027, paid one business day later.
    assert ois.swap.start_date == date(2026, 10, 21)
    assert ois.pillar_date == date(2027, 10, 22)
    with pytest.raises(MarketDataError):
        calibration_instrument_for(Quote("1Y", 0.01), eur, VALUATION_DATE)


def test_weekend_valuation_date_rolls_the_deposit_to_monday() -> None:
    late = MarketSnapshot(
        name="late",
        currency="EUR",
        valuation_date=date(2026, 10, 24),  # Saturday: deposit starts and ends on Monday
        quotes=(DepositQuote("ON", 0.019), OisSwapQuote("1Y", 0.02)),
    )
    env = CurveBuilder().build(late)
    assert env.calibration[0].pillars[0].pillar_date == date(2026, 10, 27)


def test_bootstrap_logs_a_summary(
    snapshot: MarketSnapshot, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger="rate_derivatives_pricer"):
        CurveBuilder().build(snapshot)
    events = [record.getMessage() for record in caplog.records]
    # One calibration event per quote (overnight and Ibor curves), one summary per curve.
    assert events.count("curve_pillar_calibrated") == len(snapshot.quotes)
    summaries = {
        r.curve: r  # type: ignore[attr-defined]
        for r in caplog.records
        if r.getMessage() == "curve_build_completed"
    }
    assert set(summaries) == {"EUR-ESTR", "EUR-EURIBOR3M", "EUR-EURIBOR6M"}
    summary = summaries["EUR-ESTR"]
    assert summary.pillar_count == len(_overnight_quotes(snapshot))  # type: ignore[attr-defined]
    assert summary.snapshot_name == snapshot.name  # type: ignore[attr-defined]
    assert events.count("curves_build_completed") == 1
