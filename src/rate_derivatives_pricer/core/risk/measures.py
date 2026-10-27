"""Risk measures: finite differences of the PV with respect to the calibration quotes.

Every measure depends on an abstract *pricer* ``(instrument, snapshot) -> PV`` and never on a
pricing engine: replacing the engine or the product needs no change here (ADR-010). A measure is
registered under a stable name in ``RISK_MEASURES`` and found by the :class:`RiskEngine`.

Conventions (ADR-029):

* a bump shifts a *par rate* (deposit or swap quote) by ``h`` basis points and the curves are
  re-bootstrapped; sensitivities are expressed **per 1 bp** whatever ``h`` is;
* **DV01** is the PV change for ``+1bp``: a payer swap gains when rates rise, so its DV01 is
  positive, and the sign follows the PV (currency of the trade per basis point);
* **convexity** is the second derivative in basis points squared, so that
  ``PV(dx) ~ PV + DV01 * dx + convexity * dx^2 / 2`` for a shift of ``dx`` bp.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import ClassVar

from rate_derivatives_pricer.core.conventions.tenor import Tenor, TimeUnit
from rate_derivatives_pricer.core.instruments.swaps import Instrument
from rate_derivatives_pricer.core.market.quotes import OVERNIGHT_CURVE_LABEL, Quote
from rate_derivatives_pricer.core.market.snapshot import MarketSnapshot
from rate_derivatives_pricer.core.registry import RISK_MEASURES
from rate_derivatives_pricer.core.risk.bumps import Bumper, BumpMode, BumpSpec, BumpTarget

logger = logging.getLogger(__name__)

DEFAULT_BUMP_SIZE_BP = 1.0
MILLISECONDS_PER_SECOND = 1000.0
# Rough day counts of a tenor unit, used only to order buckets from short to long.
_APPROXIMATE_DAYS = {TimeUnit.DAYS: 1, TimeUnit.WEEKS: 7, TimeUnit.MONTHS: 30, TimeUnit.YEARS: 365}

#: Valuation of an instrument on a (possibly bumped) snapshot (bootstrap, price): returns the PV.
type Pricer = Callable[[Instrument, MarketSnapshot], float]


@dataclass(frozen=True, slots=True)
class RiskSettings:
    """Numerical settings shared by the risk measures.

    Attributes:
        bump_size_bp: Bump size ``h`` in basis points (1bp by default).
        mode: Finite difference scheme of the first-order sensitivities.
    """

    bump_size_bp: float = DEFAULT_BUMP_SIZE_BP
    mode: BumpMode = BumpMode.CENTRAL


@dataclass(frozen=True, slots=True)
class RiskBucket:
    """Sensitivity attributed to one bucket (a calibration quote or a curve).

    Attributes:
        curve: Curve the bucket belongs to (``"OIS"``, ``"EURIBOR6M"``).
        label: Bucket label: the quote tenor (``"5Y"``), or the curve name for a curve bucket.
        instrument_type: Quote type (``"ois_swap"``), or ``"curve"`` for a curve bucket.
        value: Sensitivity, in the measure's unit.
    """

    curve: str
    label: str
    instrument_type: str
    value: float


@dataclass(frozen=True, slots=True)
class RiskResult:
    """Outcome of one risk measure.

    Attributes:
        measure: Registry name of the measure.
        value: Headline number (total over the buckets for a bucketed measure).
        unit: Unit of ``value`` and of the buckets.
        buckets: Breakdown, empty for a scalar measure.
        bump_count: Number of repricings the measure used.
        duration_ms: Wall-clock time of the measure.
    """

    measure: str
    value: float
    unit: str
    buckets: tuple[RiskBucket, ...] = ()
    bump_count: int = 0
    duration_ms: float = 0.0


def _tenor_days(tenor: Tenor) -> int:
    return tenor.length * _APPROXIMATE_DAYS[tenor.unit]


def _bucket_order(quote: Quote) -> tuple[int, str, int, str]:
    """Overnight curve first, then Ibor curves by name, each from short to long maturity."""
    return (
        quote.curve_label != OVERNIGHT_CURVE_LABEL,
        quote.curve_label,
        _tenor_days(quote.tenor),
        quote.instrument_type,
    )


class RiskMeasure(ABC):
    """A sensitivity of the PV computed by bumping the snapshot and repricing."""

    #: Registry key and API name (``"key_rate_dv01"``).
    name: ClassVar[str]
    #: Unit of the result values.
    unit: ClassVar[str]

    @abstractmethod
    def compute(
        self,
        instrument: Instrument,
        snapshot: MarketSnapshot,
        pricer: Pricer,
        settings: RiskSettings,
    ) -> RiskResult:
        """Compute the measure for ``instrument`` on ``snapshot``.

        Raises:
            RiskComputationError: If a bump or a repricing fails.
        """


@dataclass(slots=True)
class _Repricing:
    """Bumped valuations of one measure (counts the repricings for the result)."""

    measure: str
    instrument: Instrument
    snapshot: MarketSnapshot
    pricer: Pricer
    bumper: Bumper = field(default_factory=Bumper)
    count: int = 0

    def base_pv(self) -> float:
        """PV on the unbumped snapshot."""
        return self.pricer(self.instrument, self.snapshot)

    def pvs(self, spec: BumpSpec) -> dict[float, float]:
        """PV after each signed shift of ``spec`` (``{shift_bp: PV}``)."""
        values: dict[float, float] = {}
        for shift_bp in spec.shifts_bp:
            bumped = self.bumper.apply(self.snapshot, spec.target, shift_bp)
            values[shift_bp] = self.pricer(self.instrument, bumped)
            self.count += 1
            logger.debug(
                "risk_bump_repriced",
                extra={
                    "measure": self.measure,
                    "target": spec.target.describe(),
                    "shift_bp": shift_bp,
                    "pv_bumped": values[shift_bp],
                },
            )
        return values

    def dv01(self, spec: BumpSpec) -> float:
        """First derivative per basis point from the finite difference scheme of ``spec``."""
        pvs = self.pvs(spec)
        h = spec.size_bp
        match spec.mode:
            case BumpMode.UP:
                return (pvs[h] - self.base_pv()) / h
            case BumpMode.DOWN:
                return (self.base_pv() - pvs[-h]) / h
            case BumpMode.CENTRAL:
                return (pvs[h] - pvs[-h]) / (2.0 * h)


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * MILLISECONDS_PER_SECOND


def _finish(
    repricing: _Repricing,
    started: float,
    unit: str,
    value: float,
    buckets: tuple[RiskBucket, ...] = (),
) -> RiskResult:
    """Build the result of a measure and log its completion."""
    result = RiskResult(
        measure=repricing.measure,
        value=value,
        unit=unit,
        buckets=buckets,
        bump_count=repricing.count,
        duration_ms=_elapsed_ms(started),
    )
    logger.info(
        "risk_measure_completed",
        extra={
            "measure": result.measure,
            "value": result.value,
            "bump_count": result.bump_count,
            "duration_ms": result.duration_ms,
        },
    )
    return result


@RISK_MEASURES.register("parallel_dv01")
class ParallelDV01(RiskMeasure):
    """PV change for a parallel +1bp shift of every calibration quote (all curves together)."""

    name: ClassVar[str] = "parallel_dv01"
    unit: ClassVar[str] = "currency per bp"

    def compute(
        self,
        instrument: Instrument,
        snapshot: MarketSnapshot,
        pricer: Pricer,
        settings: RiskSettings,
    ) -> RiskResult:
        """Bump every quote, re-bootstrap every curve, reprice."""
        started = time.perf_counter()
        repricing = _Repricing(self.name, instrument, snapshot, pricer)
        spec = BumpSpec(BumpTarget(), settings.bump_size_bp, settings.mode)
        return _finish(repricing, started, self.unit, repricing.dv01(spec))


@RISK_MEASURES.register("key_rate_dv01")
class KeyRateDV01(RiskMeasure):
    """DV01 per bucket: one calibration instrument is bumped at a time (par-rate key rates).

    The buckets are the quotes of the snapshot, ordered by curve and maturity. Their sum is the
    parallel DV01 up to the finite difference error (cross terms cancel in a central scheme).
    """

    name: ClassVar[str] = "key_rate_dv01"
    unit: ClassVar[str] = "currency per bp"

    def compute(
        self,
        instrument: Instrument,
        snapshot: MarketSnapshot,
        pricer: Pricer,
        settings: RiskSettings,
    ) -> RiskResult:
        """Bump each quote alone, re-bootstrap, reprice."""
        started = time.perf_counter()
        repricing = _Repricing(self.name, instrument, snapshot, pricer)
        buckets = []
        for quote in sorted(snapshot.quotes, key=_bucket_order):
            spec = BumpSpec(
                BumpTarget(quote_keys=frozenset({quote.key})),
                settings.bump_size_bp,
                settings.mode,
            )
            buckets.append(
                RiskBucket(
                    curve=quote.curve_label,
                    label=quote.tenor_label,
                    instrument_type=quote.instrument_type,
                    value=repricing.dv01(spec),
                )
            )
        total = sum(bucket.value for bucket in buckets)
        return _finish(repricing, started, self.unit, total, tuple(buckets))


@RISK_MEASURES.register("curve_dv01")
class CurveSpecificDV01(RiskMeasure):
    """DV01 per curve: the OIS (discounting) curve versus each Ibor (projection) curve.

    Bumping the ``OIS`` quotes moves the discount curve *and*, since Ibor curves are calibrated
    on top of it, re-calibrates the Ibor curves (their par rates are held fixed); bumping an Ibor
    index moves only that projection curve. The curve DV01s add up to the parallel DV01.
    """

    name: ClassVar[str] = "curve_dv01"
    unit: ClassVar[str] = "currency per bp"

    def compute(
        self,
        instrument: Instrument,
        snapshot: MarketSnapshot,
        pricer: Pricer,
        settings: RiskSettings,
    ) -> RiskResult:
        """Bump the quotes of one curve at a time, re-bootstrap, reprice."""
        started = time.perf_counter()
        repricing = _Repricing(self.name, instrument, snapshot, pricer)
        labels = sorted({quote.curve_label for quote in snapshot.quotes}, key=_curve_order)
        buckets = []
        for label in labels:
            spec = BumpSpec(
                BumpTarget(curves=frozenset({label})), settings.bump_size_bp, settings.mode
            )
            buckets.append(
                RiskBucket(
                    curve=label, label=label, instrument_type="curve", value=repricing.dv01(spec)
                )
            )
        total = sum(bucket.value for bucket in buckets)
        return _finish(repricing, started, self.unit, total, tuple(buckets))


def _curve_order(label: str) -> tuple[bool, str]:
    return (label != OVERNIGHT_CURVE_LABEL, label)


@RISK_MEASURES.register("convexity")
class Convexity(RiskMeasure):
    """Parallel convexity (gamma): second derivative of the PV in bp^2 for a parallel shift.

    Always a central second difference ``(PV(+h) - 2 PV + PV(-h)) / h^2`` whatever the mode of
    the settings, since a one-sided scheme would not give a second derivative.
    """

    name: ClassVar[str] = "convexity"
    unit: ClassVar[str] = "currency per bp^2"

    def compute(
        self,
        instrument: Instrument,
        snapshot: MarketSnapshot,
        pricer: Pricer,
        settings: RiskSettings,
    ) -> RiskResult:
        """Bump every quote up and down, re-bootstrap, reprice."""
        started = time.perf_counter()
        repricing = _Repricing(self.name, instrument, snapshot, pricer)
        h = settings.bump_size_bp
        pvs = repricing.pvs(BumpSpec(BumpTarget(), h, BumpMode.CENTRAL))
        value = (pvs[h] - 2.0 * repricing.base_pv() + pvs[-h]) / (h * h)
        return _finish(repricing, started, self.unit, value)
