"""Discount curve interpolation schemes (Strategy pattern).

An interpolator maps node times ``t_0 = 0 < t_1 < ... < t_n`` (years, curve day count) and node
discount factors ``P_0 = 1, P_1, ..., P_n`` to discount factors at arbitrary times ``t >= 0``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

import numpy as np
import numpy.typing as npt

from rate_derivatives_pricer.core.registry import INTERPOLATORS

FloatArray = npt.NDArray[np.float64]


class Interpolator(ABC):
    """Interpolation of discount factors between curve nodes (stateless)."""

    #: Stable identifier used in the API and the catalog (registry key).
    name: ClassVar[str]
    #: Whether the value at ``t`` only depends on the two nodes around ``t`` (and, beyond the last
    #: node, on the last two). The bootstrap relies on it to reuse pillars already solved.
    local: ClassVar[bool] = False

    @abstractmethod
    def discount_factors(
        self, times: FloatArray, node_times: FloatArray, node_discount_factors: FloatArray
    ) -> FloatArray:
        """Interpolated discount factors at ``times``.

        Args:
            times: Query times in years, all ``>= 0``.
            node_times: Strictly increasing node times, starting at ``0``.
            node_discount_factors: Discount factors at the nodes, starting at ``1``.
        """


def _extend_last_segment(
    times: FloatArray, node_times: FloatArray, node_values: FloatArray
) -> FloatArray:
    """Piecewise-linear interpolation, extrapolating the last segment linearly beyond it."""
    values = np.interp(times, node_times, node_values)
    beyond = times > node_times[-1]
    if np.any(beyond) and len(node_times) >= 2:
        slope = (node_values[-1] - node_values[-2]) / (node_times[-1] - node_times[-2])
        values[beyond] = node_values[-1] + slope * (times[beyond] - node_times[-1])
    return values


@INTERPOLATORS.register("log_linear_df")
class LogLinearOnDiscountFactor(Interpolator):
    """Linear interpolation of ``ln P(t)``: piecewise-constant instantaneous forward rates.

    Beyond the last node the last forward rate is kept (flat forward extrapolation). This is
    the market default for bootstrapped OIS curves (QuantLib ``PiecewiseLogLinearDiscount``).
    """

    name: ClassVar[str] = "log_linear_df"
    local: ClassVar[bool] = True

    def discount_factors(
        self, times: FloatArray, node_times: FloatArray, node_discount_factors: FloatArray
    ) -> FloatArray:
        """``exp`` of the linearly interpolated log discount factors."""
        log_discount = _extend_last_segment(times, node_times, np.log(node_discount_factors))
        return np.exp(log_discount)


@INTERPOLATORS.register("linear_zero")
class LinearOnZeroRate(Interpolator):
    """Linear interpolation of the continuously compounded zero rate ``z(t) = -ln P(t) / t``.

    The zero rate at ``t = 0`` is set equal to the first node's (as QuantLib's
    ``PiecewiseLinearZero``). Beyond the last node ``t_n`` the instantaneous forward rate at
    ``t_n``, ``f_n = z_n + t_n z'(t_n)``, is kept constant (flat forward extrapolation)::

        -ln P(t) = z_n t_n + f_n (t - t_n)
    """

    name: ClassVar[str] = "linear_zero"
    local: ClassVar[bool] = True

    def discount_factors(
        self, times: FloatArray, node_times: FloatArray, node_discount_factors: FloatArray
    ) -> FloatArray:
        """``exp(-z(t) t)`` with ``z`` linearly interpolated between node zero rates."""
        if len(node_times) < 2:
            return np.ones_like(times)
        zero_rates = np.empty_like(node_times)
        zero_rates[1:] = -np.log(node_discount_factors[1:]) / node_times[1:]
        zero_rates[0] = zero_rates[1]
        log_discount = -np.interp(times, node_times, zero_rates) * times
        beyond = times > node_times[-1]
        if np.any(beyond):
            last_time, last_zero = node_times[-1], zero_rates[-1]
            slope = (zero_rates[-1] - zero_rates[-2]) / (node_times[-1] - node_times[-2])
            last_forward = last_zero + last_time * slope
            log_discount[beyond] = -(
                last_zero * last_time + last_forward * (times[beyond] - last_time)
            )
        return np.exp(log_discount)
