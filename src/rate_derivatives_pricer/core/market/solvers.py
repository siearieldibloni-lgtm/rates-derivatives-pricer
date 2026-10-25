"""One-dimensional root finders used by the bootstrap (Strategy pattern, no SciPy, ADR-004)."""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar

# Default absolute tolerance on the root. The bootstrap solves for discount factors of order 1,
# so this is close to machine precision (QuantLib validation requires 1e-10 on DFs).
DEFAULT_ROOT_TOLERANCE = 1e-15
DEFAULT_MAX_ITERATIONS = 100
# Relative step of the central finite difference used by Newton when no derivative is given.
NEWTON_DERIVATIVE_RELATIVE_STEP = 1e-7
MACHINE_EPSILON = 2.220446049250313e-16

ObjectiveFunction = Callable[[float], float]


@dataclass(frozen=True, slots=True)
class SolverResult:
    """Outcome of a root search.

    Attributes:
        root: Best estimate of the root (``nan`` if the search could not start).
        residual: Objective value at ``root``.
        iterations: Number of objective evaluations after the initial ones.
        converged: Whether the tolerance was reached.
    """

    root: float
    residual: float
    iterations: int
    converged: bool


class Solver(ABC):
    """Finds ``x`` in ``[lower, upper]`` such that ``f(x) = 0``."""

    #: Stable identifier (registry / API option).
    name: ClassVar[str]

    @abstractmethod
    def solve(
        self, f: ObjectiveFunction, lower: float, upper: float, initial_guess: float
    ) -> SolverResult:
        """Search a root of ``f`` in ``[lower, upper]``, starting from ``initial_guess``."""


@dataclass(frozen=True, slots=True)
class BrentSolver(Solver):
    """Brent's method (inverse quadratic interpolation + secant + bisection safeguard).

    Guaranteed to converge when ``f(lower)`` and ``f(upper)`` have opposite signs; superlinear
    near a simple root. Implementation follows Brent (1973), *Algorithms for Minimization
    without Derivatives*, chapter 4 ("zero").

    Attributes:
        tolerance: Absolute tolerance on the root.
        max_iterations: Maximum number of iterations.
    """

    tolerance: float = DEFAULT_ROOT_TOLERANCE
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    name: ClassVar[str] = "brent"

    def solve(
        self, f: ObjectiveFunction, lower: float, upper: float, initial_guess: float
    ) -> SolverResult:
        """Brent root search; ``initial_guess`` is unused (the bracket drives the search)."""
        a, b = lower, upper
        fa, fb = f(a), f(b)
        if fa == 0.0:
            return SolverResult(a, fa, 0, True)
        if fb == 0.0:
            return SolverResult(b, fb, 0, True)
        if math.copysign(1.0, fa) == math.copysign(1.0, fb):
            # Not bracketed: report the best end point, flagged as not converged.
            best, residual = (a, fa) if abs(fa) < abs(fb) else (b, fb)
            return SolverResult(best, residual, 0, False)

        c, fc = a, fa
        d = e = b - a
        for iteration in range(1, self.max_iterations + 1):
            if math.copysign(1.0, fb) == math.copysign(1.0, fc):
                # Keep the root bracketed between b and c.
                c, fc = a, fa
                d = e = b - a
            if abs(fc) < abs(fb):
                # b must be the best estimate so far.
                a, b, c = b, c, b
                fa, fb, fc = fb, fc, fb
            tol = 2.0 * MACHINE_EPSILON * abs(b) + 0.5 * self.tolerance
            midpoint = 0.5 * (c - b)
            if abs(midpoint) <= tol or fb == 0.0:
                return SolverResult(b, fb, iteration, True)
            if abs(e) >= tol and abs(fa) > abs(fb):
                # Attempt interpolation: secant if a == c, inverse quadratic otherwise.
                s = fb / fa
                if a == c:
                    p, q = 2.0 * midpoint * s, 1.0 - s
                else:
                    q, r = fa / fc, fb / fc
                    p = s * (2.0 * midpoint * q * (q - r) - (b - a) * (r - 1.0))
                    q = (q - 1.0) * (r - 1.0) * (s - 1.0)
                if p > 0.0:
                    q = -q
                else:
                    p = -p
                # Accept the interpolation only if it stays well inside the bracket and keeps
                # shrinking fast enough; otherwise fall back to bisection.
                if 2.0 * p < min(3.0 * midpoint * q - abs(tol * q), abs(e * q)):
                    e, d = d, p / q
                else:
                    d = e = midpoint
            else:
                d = e = midpoint
            a, fa = b, fb
            b += d if abs(d) > tol else math.copysign(tol, midpoint)
            fb = f(b)
        return SolverResult(b, fb, self.max_iterations, False)


@dataclass(frozen=True, slots=True)
class NewtonSolver(Solver):
    """Safeguarded Newton-Raphson with a finite-difference derivative.

    A Newton step leaving the current bracket is replaced by a bisection step, so the method
    keeps the robustness of bisection when ``f(lower)`` and ``f(upper)`` have opposite signs.

    Attributes:
        tolerance: Absolute tolerance on the root (step size).
        max_iterations: Maximum number of iterations.
        derivative: Optional analytical derivative ``f'``; central differences otherwise.
    """

    tolerance: float = DEFAULT_ROOT_TOLERANCE
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    derivative: ObjectiveFunction | None = None
    name: ClassVar[str] = "newton"

    def _derivative(self, f: ObjectiveFunction, x: float) -> float:
        if self.derivative is not None:
            return self.derivative(x)
        h = NEWTON_DERIVATIVE_RELATIVE_STEP * max(1.0, abs(x))
        return (f(x + h) - f(x - h)) / (2.0 * h)

    def solve(
        self, f: ObjectiveFunction, lower: float, upper: float, initial_guess: float
    ) -> SolverResult:
        """Newton iterations from ``initial_guess``, safeguarded by the bracket."""
        lo, hi = lower, upper
        f_lo = f(lo)
        bracketed = math.copysign(1.0, f_lo) != math.copysign(1.0, f(hi))
        x = min(max(initial_guess, lo), hi)
        fx = f(x)
        for iteration in range(1, self.max_iterations + 1):
            if fx == 0.0:
                return SolverResult(x, fx, iteration, True)
            if bracketed:
                # Shrink the bracket around the root with the latest evaluation.
                if math.copysign(1.0, fx) == math.copysign(1.0, f_lo):
                    lo, f_lo = x, fx
                else:
                    hi = x
            slope = self._derivative(f, x)
            step = fx / slope if slope != 0.0 and math.isfinite(slope) else math.inf
            candidate = x - step
            if not (lo <= candidate <= hi) or not math.isfinite(candidate):
                if not bracketed:
                    return SolverResult(x, fx, iteration, False)
                candidate = 0.5 * (lo + hi)
            converged = abs(candidate - x) <= self.tolerance + 2.0 * MACHINE_EPSILON * abs(x)
            x = candidate
            fx = f(x)
            if converged:
                return SolverResult(x, fx, iteration, True)
        return SolverResult(x, fx, self.max_iterations, False)


#: Solvers by name (bootstrap option).
SOLVERS: dict[str, type[Solver]] = {cls.name: cls for cls in (BrentSolver, NewtonSolver)}
