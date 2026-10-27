"""Risk engine: runs a list of risk measures on one trade and gathers a :class:`RiskReport`."""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from rate_derivatives_pricer.core.errors import RatesPricerError, RiskComputationError
from rate_derivatives_pricer.core.instruments.swaps import Instrument
from rate_derivatives_pricer.core.market.snapshot import MarketSnapshot
from rate_derivatives_pricer.core.registry import RISK_MEASURES
from rate_derivatives_pricer.core.risk.measures import Pricer, RiskResult, RiskSettings

logger = logging.getLogger(__name__)

MILLISECONDS_PER_SECOND = 1000.0

# Identity of the market data a valuation depends on: valuation date and quotes (key, value, unit).
type _SnapshotSignature = tuple[date, tuple[tuple[tuple[str, str], float, str], ...]]


@dataclass(frozen=True, slots=True)
class RiskReport:
    """Risk of one instrument.

    Attributes:
        instrument_type: Instrument type of the trade.
        base_pv: PV on the unbumped market.
        settings: Bump size and finite difference scheme used.
        results: One result per requested measure, in request order.
        duration_ms: Total wall-clock time.
    """

    instrument_type: str
    base_pv: float
    settings: RiskSettings
    results: tuple[RiskResult, ...]
    duration_ms: float

    def result(self, measure: str) -> RiskResult:
        """Result of ``measure``.

        Raises:
            KeyError: If the measure was not requested.
        """
        for result in self.results:
            if result.measure == measure:
                return result
        raise KeyError(measure)


class _MemoizedPricer:
    """Pricer remembering the PV of every snapshot it has already valued.

    Measures share bumps (the parallel ``+h`` / ``-h`` snapshots serve both the parallel DV01 and
    the convexity, the base snapshot serves everything); a bootstrap is by far the dominant cost
    of a risk run, so a snapshot is valued once per run.
    """

    def __init__(self, pricer: Pricer) -> None:
        self._pricer = pricer
        self._values: dict[tuple[int, _SnapshotSignature], float] = {}
        self.calls = 0
        self.hits = 0

    def __call__(self, instrument: Instrument, snapshot: MarketSnapshot) -> float:
        signature: _SnapshotSignature = (
            snapshot.valuation_date,
            tuple((quote.key, quote.rate, quote.instrument_type) for quote in snapshot.quotes),
        )
        key = (id(instrument), signature)
        self.calls += 1
        if key in self._values:
            self.hits += 1
            return self._values[key]
        value = self._pricer(instrument, snapshot)
        self._values[key] = value
        return value


class RiskEngine:
    """Computes risk measures through an abstract pricer (no dependency on pricing engines).

    Args:
        pricer: Values an instrument on a market snapshot: ``(instrument, snapshot) -> PV``. The
            caller decides how (typically: bootstrap the snapshot, then price with an engine).
    """

    def __init__(self, pricer: Pricer) -> None:
        self._pricer = pricer

    def compute(
        self,
        instrument: Instrument,
        snapshot: MarketSnapshot,
        measures: Sequence[str],
        settings: RiskSettings | None = None,
    ) -> RiskReport:
        """Run ``measures`` on ``instrument``.

        Args:
            instrument: Trade to analyse.
            snapshot: Base market data, whose quotes are bumped.
            measures: Names registered in ``RISK_MEASURES`` (``"key_rate_dv01"``...).
            settings: Bump size and scheme (1bp, central by default).

        Raises:
            UnsupportedRiskMeasureError: If a measure name is unknown (checked before any work).
            RiskComputationError: If a bumped valuation fails.
            MarketDataError: If the base market data cannot value the trade (not wrapped).
        """
        started = time.perf_counter()
        settings = settings or RiskSettings()
        # Fail fast on an unknown measure, before any bootstrap.
        implementations = [RISK_MEASURES.get(name)() for name in measures]

        pricer = _MemoizedPricer(self._pricer)
        # The base valuation runs outside the error wrapping: a trade that cannot be valued at
        # all is an input error, not a risk computation failure.
        base_pv = pricer(instrument, snapshot)

        results = []
        for measure in implementations:
            try:
                results.append(measure.compute(instrument, snapshot, pricer, settings))
            except RiskComputationError:
                raise
            except RatesPricerError as error:
                raise RiskComputationError(
                    f"risk measure {measure.name} failed: {error.message}",
                    {"measure": measure.name, "cause": type(error).__name__, **error.details},
                ) from error

        duration_ms = (time.perf_counter() - started) * MILLISECONDS_PER_SECOND
        logger.info(
            "risk_completed",
            extra={
                "trade_type": instrument.instrument_type,
                "measures": list(measures),
                "base_pv": base_pv,
                "valuations": pricer.calls - pricer.hits,
                "reused_valuations": pricer.hits,
                "duration_ms": duration_ms,
            },
        )
        return RiskReport(
            instrument_type=instrument.instrument_type,
            base_pv=base_pv,
            settings=settings,
            results=tuple(results),
            duration_ms=duration_ms,
        )
