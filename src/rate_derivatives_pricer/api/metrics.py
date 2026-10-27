"""Prometheus metrics of the service, exposed by ``GET /metrics`` (ADR-038).

Each application owns its :class:`ApiMetrics` and its own ``CollectorRegistry`` (no global
registry): two applications in one process (tests) never share counters. As the curve cache,
the metrics are per process: with several Uvicorn workers, each worker exposes its own.

HTTP metrics are labelled by the **route template** (``/v1/logs/{request_id}``), never by the raw
path, so that the number of time series stays bounded.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

from prometheus_client import CollectorRegistry, Counter, Histogram, Info, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, Metric
from prometheus_client.exposition import CONTENT_TYPE_LATEST
from prometheus_client.registry import Collector

from rate_derivatives_pricer.services.curve_cache import CacheStats

METRIC_PREFIX = "pricer"
UNMATCHED_ROUTE = "unmatched"
# Latency buckets in seconds: a cached price takes milliseconds, a 30Y key-rate several seconds.
LATENCY_BUCKETS_SECONDS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)


class _CurveCacheCollector(Collector):
    """Reads the curve cache counters at scrape time (the cache stays the source of truth)."""

    def __init__(self, cache_stats: Callable[[], CacheStats]) -> None:
        self._cache_stats = cache_stats

    def collect(self) -> Iterator[Metric]:
        """Current cache counters (they restart from zero after ``DELETE /v1/cache``)."""
        stats = self._cache_stats()
        for name, documentation, value in (
            ("hits", "Curve cache lookups served from the cache.", stats.hits),
            ("misses", "Curve cache lookups that required a bootstrap.", stats.misses),
            ("evictions", "Curve cache entries evicted (full or expired).", stats.evictions),
        ):
            yield CounterMetricFamily(
                f"{METRIC_PREFIX}_curve_cache_{name}", documentation, value=value
            )
        yield GaugeMetricFamily(
            f"{METRIC_PREFIX}_curve_cache_size", "Curve cache entries.", value=stats.size
        )
        yield GaugeMetricFamily(
            f"{METRIC_PREFIX}_curve_cache_max_size", "Curve cache capacity.", value=stats.max_size
        )


class ApiMetrics:
    """Counters and histograms of one application.

    Args:
        cache_stats: Source of the curve cache counters (``PricerService.cache_stats``).
        version: Service version, published as ``pricer_build_info``.
        commit: Deployed commit, published as ``pricer_build_info``.
    """

    def __init__(self, cache_stats: Callable[[], CacheStats], version: str, commit: str) -> None:
        self.registry = CollectorRegistry()
        self._requests = Counter(
            f"{METRIC_PREFIX}_http_requests",
            "HTTP requests by method, route template and status code.",
            ["method", "route", "status"],
            registry=self.registry,
        )
        self._latency = Histogram(
            f"{METRIC_PREFIX}_http_request_duration_seconds",
            "HTTP request duration in seconds, by method and route template.",
            ["method", "route"],
            buckets=LATENCY_BUCKETS_SECONDS,
            registry=self.registry,
        )
        self._errors = Counter(
            f"{METRIC_PREFIX}_errors",
            "Error responses by stable error code (docs/API.md section 5).",
            ["code"],
            registry=self.registry,
        )
        self._batch_trades = Counter(
            f"{METRIC_PREFIX}_batch_trades",
            "Trades priced by /v1/batch/price, by outcome (ok or error code).",
            ["outcome"],
            registry=self.registry,
        )
        Info(f"{METRIC_PREFIX}_build", "Service build.", registry=self.registry).info(
            {"version": version, "commit": commit}
        )
        self.registry.register(_CurveCacheCollector(cache_stats))

    def observe_request(self, method: str, route: str, status: int, seconds: float) -> None:
        """Count one HTTP request and record its duration."""
        self._requests.labels(method=method, route=route, status=str(status)).inc()
        self._latency.labels(method=method, route=route).observe(seconds)

    def count_error(self, code: str) -> None:
        """Count one error response with its stable code."""
        self._errors.labels(code=code).inc()

    def count_batch_trade(self, outcome: str) -> None:
        """Count one trade of a batch: ``"ok"`` or its error code."""
        self._batch_trades.labels(outcome=outcome).inc()

    def render(self) -> tuple[bytes, str]:
        """Exposition text and its content type."""
        return generate_latest(self.registry), CONTENT_TYPE_LATEST
