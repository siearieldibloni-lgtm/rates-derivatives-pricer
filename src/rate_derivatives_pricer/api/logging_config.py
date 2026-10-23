"""Structured logging configuration with ``request_id`` correlation.

The core emits, the API configures: ``core`` only calls ``logging.getLogger(__name__)`` and knows
nothing about this module. The API installs one handler on the root logger, so every record —
including the ones emitted deep inside ``core`` — goes through :class:`RequestIdFilter` and gets
the ``request_id`` of the request being processed.

The correlation id lives in a :class:`contextvars.ContextVar`, which is isolated per thread and per
asyncio task, so concurrent requests never see each other's id.

Record layout (one JSON object per line, see docs/API.md section 7.1)::

    {"timestamp": "2026-10-19T08:15:02.123456+00:00", "level": "INFO",
     "logger": "core.market.bootstrap", "event": "curve_build_completed",
     "request_id": "3f2a...", "duration_ms": 12.4, "currency": "EUR", ...}

Business fields are passed with ``extra``::

    logger.info("curve_build_completed", extra={"duration_ms": 12.4, "currency": "EUR"})
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Literal, TextIO

LogFormat = Literal["json", "text"]

# Value used when a record is emitted outside of any request (startup, background work, tests).
NO_REQUEST_ID = "-"

# Prefix stripped from logger names so that logs read "core.market.bootstrap" (docs/API.md 7.1).
PACKAGE_PREFIX = "rate_derivatives_pricer."

_request_id: ContextVar[str] = ContextVar("request_id", default=NO_REQUEST_ID)

# Attributes every LogRecord carries; anything else on a record was supplied through `extra`
# and is emitted as a business field.
_STANDARD_RECORD_ATTRIBUTES = frozenset(
    vars(logging.LogRecord("", logging.INFO, "", 0, "", None, None)).keys()
) | {"message", "asctime", "request_id", "event"}


def get_request_id() -> str:
    """Return the correlation id of the current context, or ``NO_REQUEST_ID`` outside a request."""
    return _request_id.get()


def new_request_id() -> str:
    """Generate a fresh correlation id (UUID4 hex, 32 characters)."""
    return uuid.uuid4().hex


@contextmanager
def request_context(request_id: str | None = None) -> Iterator[str]:
    """Bind a ``request_id`` to every log record emitted inside the ``with`` block.

    Args:
        request_id: Id to bind, typically taken from the ``X-Request-ID`` header. A new one is
            generated when ``None``.

    Yields:
        The bound request id, to be echoed back in the HTTP response.
    """
    bound_id = request_id or new_request_id()
    token = _request_id.set(bound_id)
    try:
        yield bound_id
    finally:
        # Restore the previous value so nested or sequential contexts do not leak into each other.
        _request_id.reset(token)


class RequestIdFilter(logging.Filter):
    """Stamp each record with the ``request_id`` of the current context."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Attach ``record.request_id``; never drops a record."""
        record.request_id = get_request_id()
        return True


def _short_logger_name(name: str) -> str:
    return name.removeprefix(PACKAGE_PREFIX)


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    return {
        key: value
        for key, value in vars(record).items()
        if key not in _STANDARD_RECORD_ATTRIBUTES and not key.startswith("_")
    }


class JsonFormatter(logging.Formatter):
    """Render a record as a single-line JSON object with stable field names."""

    def format(self, record: logging.LogRecord) -> str:
        """Serialize the record; non-JSON-native extra values fall back to ``str()``."""
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": _short_logger_name(record.name),
            # The log message is the stable event name (e.g. "pricing_completed").
            "event": record.getMessage(),
            "request_id": getattr(record, "request_id", NO_REQUEST_ID),
        }
        payload.update(_extra_fields(record))
        if record.exc_info:
            # Full stack trace stays in the logs; clients only ever get a generic message.
            payload["exc_info"] = self.formatException(record.exc_info)
        # ASCII escaping guarantees one physical line per record, even for exotic line separators.
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    """Human-readable single-line format for local development."""

    def format(self, record: logging.LogRecord) -> str:
        """Render ``timestamp level [request_id] logger event key=value ...``."""
        timestamp = datetime.fromtimestamp(record.created, tz=UTC).isoformat(
            timespec="milliseconds"
        )
        fields = " ".join(f"{key}={value}" for key, value in _extra_fields(record).items())
        line = (
            f"{timestamp} {record.levelname:<7} [{getattr(record, 'request_id', NO_REQUEST_ID)}] "
            f"{_short_logger_name(record.name)} {record.getMessage()}"
        )
        if fields:
            line = f"{line} {fields}"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


def configure_logging(
    level: str = "INFO", log_format: LogFormat = "json", stream: TextIO | None = None
) -> None:
    """Install the structured handler on the root logger, replacing any existing handlers.

    Idempotent: calling it again reconfigures logging instead of stacking handlers.

    Args:
        level: Root log level name (``LOG_LEVEL``), e.g. ``"INFO"`` or ``"DEBUG"``.
        log_format: ``"json"`` in containers, ``"text"`` for local development (``LOG_FORMAT``).
        stream: Output stream; defaults to stdout so that the infrastructure collects the logs.
    """
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(JsonFormatter() if log_format == "json" else TextFormatter())

    root = logging.getLogger()
    for existing in root.handlers[:]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())
