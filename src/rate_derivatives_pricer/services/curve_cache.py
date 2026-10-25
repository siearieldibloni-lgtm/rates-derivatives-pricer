"""In-memory LRU cache of bootstrapped market environments (ADR-005).

The cache is per process: with several Uvicorn workers, each worker has its own cache. The
``CurveCache`` protocol allows a shared implementation (Redis) to be plugged in later.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import Protocol

from rate_derivatives_pricer.core.market.environment import MarketEnvironment

DEFAULT_CACHE_SIZE = 32


@dataclass(frozen=True, slots=True)
class CacheStats:
    """Cache counters since start (or since the last ``clear``).

    Attributes:
        hits: Lookups served from the cache.
        misses: Lookups that required a bootstrap.
        evictions: Entries dropped because the cache was full or expired.
        size: Current number of entries.
        max_size: Capacity.
        ttl_seconds: Time to live of an entry, ``None`` if entries never expire.
    """

    hits: int
    misses: int
    evictions: int
    size: int
    max_size: int
    ttl_seconds: float | None


class CurveCache(Protocol):
    """Store of market environments keyed by an input fingerprint."""

    def get(self, key: str) -> MarketEnvironment | None:
        """Cached environment for ``key``, or ``None``."""
        ...

    def put(self, key: str, environment: MarketEnvironment) -> None:
        """Store ``environment`` under ``key``."""
        ...

    def clear(self) -> None:
        """Drop every entry and reset the counters."""
        ...

    def stats(self) -> CacheStats:
        """Current counters."""
        ...


class LruCurveCache:
    """Thread-safe least-recently-used cache with an optional time to live.

    Args:
        max_size: Maximum number of entries (>= 1).
        ttl_seconds: Entry lifetime in seconds; ``None`` keeps entries until evicted.
        clock: Monotonic time source in seconds (injectable for tests).
    """

    def __init__(
        self,
        max_size: int = DEFAULT_CACHE_SIZE,
        ttl_seconds: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_size < 1:
            raise ValueError("cache size must be at least 1")
        self._max_size = max_size
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = Lock()
        self._entries: OrderedDict[str, tuple[float, MarketEnvironment]] = OrderedDict()
        self._hits = self._misses = self._evictions = 0

    def get(self, key: str) -> MarketEnvironment | None:
        """Cached environment, refreshed as most recently used; ``None`` on miss or expiry."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and self._expired(entry[0]):
                del self._entries[key]
                self._evictions += 1
                entry = None
            if entry is None:
                self._misses += 1
                return None
            self._entries.move_to_end(key)
            self._hits += 1
            return entry[1]

    def put(self, key: str, environment: MarketEnvironment) -> None:
        """Insert or refresh an entry, evicting the least recently used one if full."""
        with self._lock:
            self._entries[key] = (self._clock(), environment)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_size:
                self._entries.popitem(last=False)
                self._evictions += 1

    def clear(self) -> None:
        """Drop every entry and reset the counters."""
        with self._lock:
            self._entries.clear()
            self._hits = self._misses = self._evictions = 0

    def stats(self) -> CacheStats:
        """Current counters."""
        with self._lock:
            return CacheStats(
                hits=self._hits,
                misses=self._misses,
                evictions=self._evictions,
                size=len(self._entries),
                max_size=self._max_size,
                ttl_seconds=self._ttl_seconds,
            )

    def _expired(self, stored_at: float) -> bool:
        return self._ttl_seconds is not None and self._clock() - stored_at > self._ttl_seconds
