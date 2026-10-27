"""Bounded in-memory ring buffer of log events, read by ``GET /v1/logs/{request_id}``.

Every record that reaches the root logger (including the ones emitted by ``core``) is also kept
here as a JSON-ready dict, so that support can rebuild the timeline of a calculation from its
``request_id`` without access to the log collector (docs/API.md section 7.5, ADR-037).

The buffer is per process and bounded: the oldest events are dropped first. Stack traces are not
kept (they stay on stdout only): the endpoint is reachable by any client, which must never see
one (docs/API.md section 5).
"""

from __future__ import annotations

import logging
import math
from collections import deque
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from threading import Lock
from typing import Any

from rate_derivatives_pricer.api.logging_config import RequestIdFilter, record_payload

DEFAULT_LOG_BUFFER_SIZE = 10_000


def _jsonable(value: Any) -> Any:
    """JSON-safe copy of a log field: nested containers kept, NaN/inf and objects as text.

    Strict JSON has no NaN (a par rate can be undefined), and extra fields may carry dates or
    domain objects: they are rendered with ``str()`` as the JSON formatter does.
    """
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence | set | frozenset):
        return [_jsonable(item) for item in value]
    return str(value)


class LogRingBuffer(logging.Handler):
    """Logging handler keeping the last ``capacity`` events in memory.

    Args:
        capacity: Maximum number of events kept (>= 1); the oldest are evicted first.
        level: Minimum level captured (``"INFO"`` by default; ``"DEBUG"`` also keeps the
            per-pillar and per-bump calculation details, at the cost of a faster turnover).
    """

    def __init__(self, capacity: int = DEFAULT_LOG_BUFFER_SIZE, level: str = "INFO") -> None:
        if capacity < 1:
            raise ValueError("log buffer capacity must be at least 1")
        super().__init__(level=level.upper())
        self.addFilter(RequestIdFilter())
        self._events: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._events_lock = Lock()
        self._evicted = 0

    @property
    def capacity(self) -> int:
        """Maximum number of events kept."""
        return self._events.maxlen or 0

    def emit(self, record: logging.LogRecord) -> None:
        """Store the record as a JSON-ready event (never raises into the caller)."""
        try:
            event = _jsonable(record_payload(record))
            if record.exc_info and record.exc_info[0] is not None:
                # The exception type helps support; the trace itself stays on stdout.
                event["exception"] = record.exc_info[0].__name__
            with self._events_lock:
                if len(self._events) == self.capacity:
                    self._evicted += 1
                self._events.append(event)
        except Exception:  # pragma: no cover - logging must never break a request
            self.handleError(record)

    def events_for(self, request_id: str) -> list[dict[str, Any]]:
        """Events of one request, oldest first (empty if unknown or already evicted)."""
        with self._events_lock:
            return [event for event in self._events if event.get("request_id") == request_id]

    def stats(self) -> dict[str, int]:
        """Size, capacity and number of events evicted since start."""
        with self._events_lock:
            return {
                "size": len(self._events),
                "capacity": self.capacity,
                "evicted": self._evicted,
            }


@contextmanager
def attached(buffer: LogRingBuffer) -> Iterator[LogRingBuffer]:
    """Attach ``buffer`` to the root logger for the lifetime of the application.

    The root level is lowered to the buffer level if needed (the stdout handler keeps its own
    level), then restored, so that successive applications (tests) never stack handlers.
    """
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(buffer)
    if root.getEffectiveLevel() > buffer.level:
        root.setLevel(buffer.level)
    try:
        yield buffer
    finally:
        root.removeHandler(buffer)
        root.setLevel(previous_level)
