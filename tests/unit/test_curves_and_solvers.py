from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pytest

from rate_derivatives_pricer.core.conventions.day_count import Act360, Act365Fixed
from rate_derivatives_pricer.core.errors import MarketDataError, PricingError
from rate_derivatives_pricer.core.market.curves import DiscountCurve
from rate_derivatives_pricer.core.market.interpolation import (
    LinearOnZeroRate,
    LogLinearOnDiscountFactor,
)
from rate_derivatives_pricer.core.market.solvers import BrentSolver, NewtonSolver
from tests.unit.helpers import VALUATION_DATE, flat_curve

NODE_TIMES = np.array([0.0, 1.0, 2.0])
NODE_DFS = np.array([1.0, math.exp(-0.02), math.exp(-0.05)])


# --------------------------------------------------------------------------- interpolation
def test_log_linear_interpolates_log_discount_factors() -> None:
    times = np.array([0.0, 0.5, 1.0, 1.5, 3.0])
    dfs = LogLinearOnDiscountFactor().discount_factors(times, NODE_TIMES, NODE_DFS)
    # Piecewise-flat forwards: 2% on [0, 1], 3% on [1, 2] and beyond (flat extrapolation).
    expected = np.exp(-np.array([0.0, 0.01, 0.02, 0.035, 0.08]))
    np.testing.assert_allclose(dfs, expected, rtol=0, atol=1e-15)


def test_linear_zero_interpolates_zero_rates() -> None:
    times = np.array([0.0, 0.5, 1.0, 1.5, 2.0, 3.0])
    dfs = LinearOnZeroRate().discount_factors(times, NODE_TIMES, NODE_DFS)
    zero_rates = np.array([0.02, 0.02, 0.02, 0.0225, 0.025])
    np.testing.assert_allclose(dfs[:5], np.exp(-zero_rates * times[:5]), rtol=0, atol=1e-15)
    # Beyond the last node: flat instantaneous forward f = z + t z' = 0.025 + 2 * 0.005 = 3.5%.
    assert dfs[5] == pytest.approx(math.exp(-(0.05 + 0.035 * 1.0)), abs=1e-15)
    single = LinearOnZeroRate().discount_factors(times, NODE_TIMES[:1], NODE_DFS[:1])
    np.testing.assert_array_equal(single, np.ones_like(times))


# --------------------------------------------------------------------------- curves
def test_discount_curve_queries() -> None:
    curve = flat_curve(0.03)
    one_year = date(2027, 10, 19)
    assert curve.reference_date == VALUATION_DATE
    assert curve.discount_factor(VALUATION_DATE) == 1.0
    assert curve.discount_factor(one_year) == pytest.approx(math.exp(-0.03), abs=1e-15)
    assert curve.zero_rate(one_year) == pytest.approx(0.03, abs=1e-14)
    assert curve.zero_rate(VALUATION_DATE) == pytest.approx(0.03, abs=1e-12)
    expected_forward = (math.exp(0.03 * 365 / 365) - 1) / (365 / 360)
    assert curve.forward_rate(VALUATION_DATE, one_year, Act360()) == pytest.approx(
        expected_forward, abs=1e-14
    )
    with pytest.raises(PricingError):
        curve.discount_factor(VALUATION_DATE - timedelta(days=1))


def _curve(dates: tuple[date, ...], dfs: tuple[float, ...]) -> DiscountCurve:
    return DiscountCurve(VALUATION_DATE, dates, dfs, LogLinearOnDiscountFactor(), Act365Fixed())


def test_discount_curve_validation() -> None:
    with pytest.raises(MarketDataError, match="length"):
        _curve((date(2027, 1, 1),), ())
    with pytest.raises(MarketDataError, match="increasing"):
        _curve((date(2027, 1, 1), date(2026, 12, 1)), (0.99, 0.98))
    with pytest.raises(MarketDataError, match="increasing"):
        _curve((VALUATION_DATE,), (1.0,))
    with pytest.raises(MarketDataError, match="positive"):
        _curve((date(2027, 1, 1),), (-0.5,))
    rebuilt = _curve((date(2027, 1, 1),), (0.99,)).with_pillars((date(2028, 1, 1),), (0.97,))
    assert rebuilt.pillar_dates == (date(2028, 1, 1),)


# --------------------------------------------------------------------------- solvers
def cubic(x: float) -> float:
    return x**3 - 2.0 * x - 5.0  # root ~ 2.0945514815423265 (Newton's historical example)


CUBIC_ROOT = 2.0945514815423265


@pytest.mark.parametrize(
    "solver", [BrentSolver(), NewtonSolver(), NewtonSolver(derivative=lambda x: 3 * x * x - 2)]
)
def test_solvers_find_the_root(solver: BrentSolver | NewtonSolver) -> None:
    result = solver.solve(cubic, 2.0, 3.0, 2.5)
    assert result.converged
    assert result.root == pytest.approx(CUBIC_ROOT, abs=1e-14)
    assert abs(result.residual) < 1e-12
    assert result.iterations > 0


def test_brent_edge_cases() -> None:
    solver = BrentSolver()
    assert solver.solve(lambda x: x - 1.0, 1.0, 2.0, 1.5).root == 1.0
    assert solver.solve(lambda x: x - 2.0, 1.0, 2.0, 1.5).root == 2.0
    not_bracketed = solver.solve(lambda x: x * x + 1.0, -1.0, 2.0, 0.0)
    assert not not_bracketed.converged
    assert BrentSolver(max_iterations=2).solve(cubic, 2.0, 3.0, 2.5).converged is False


def test_newton_edge_cases() -> None:
    # Flat start without a bracket: the step is undefined and the search stops.
    flat = NewtonSolver().solve(lambda x: x * x + 1.0, -1.0, 1.0, 0.0)
    assert not flat.converged
    # A bad derivative sends Newton outside the bracket: bisection keeps it on track.
    safeguarded = NewtonSolver(derivative=lambda x: 1e-6).solve(cubic, 2.0, 3.0, 2.9)
    assert safeguarded.converged
    assert safeguarded.root == pytest.approx(CUBIC_ROOT, abs=1e-12)
    assert NewtonSolver().solve(lambda x: x, -1.0, 1.0, 0.0).residual == 0.0
    assert not NewtonSolver(max_iterations=1).solve(cubic, 2.0, 3.0, 3.0).converged
