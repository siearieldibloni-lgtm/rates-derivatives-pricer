"""ASGI middleware binding a ``request_id`` to every request (docs/API.md section 1 and 7).

The id is taken from the ``X-Request-ID`` header when it is well formed, generated otherwise,
bound to every log record emitted while the request is processed (including from ``core``),
and echoed back in the ``X-Request-ID`` response header and in the response body.

A plain ASGI middleware (rather than Starlette's ``BaseHTTPMiddleware``) keeps the context
variable visible to the endpoint, and lets unexpected exceptions be answered with the
structured ``INTERNAL_ERROR`` format while the ``request_id`` is still bound.

The middleware also feeds the HTTP metrics (count and latency by route template, ADR-038) and
logs a ``request_started`` / ``request_completed`` pair framing every request in the timeline
returned by ``GET /v1/logs/{request_id}``.
"""

from __future__ import annotations

import logging
import re
import time

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from rate_derivatives_pricer.api.errors import (
    INTERNAL_ERROR,
    INTERNAL_ERROR_MESSAGE,
    error_response,
)
from rate_derivatives_pricer.api.logging_config import request_context
from rate_derivatives_pricer.api.metrics import UNMATCHED_ROUTE, ApiMetrics

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
# Client-supplied ids are echoed in logs and headers: accept only short, safe tokens.
REQUEST_ID_PATTERN = r"^[A-Za-z0-9._\-]{1,128}$"
_VALID_REQUEST_ID = re.compile(REQUEST_ID_PATTERN)
MILLISECONDS_PER_SECOND = 1000.0


def _route_template(scope: Scope) -> str:
    """Route template matched by the router (bounded metric label), set after routing."""
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else UNMATCHED_ROUTE


class RequestContextMiddleware:
    """Bind the ``request_id``, log the request, record its metrics, catch unexpected errors.

    Args:
        app: Wrapped ASGI application.
        metrics: Metrics of the application (``None``: no metrics recorded).
    """

    def __init__(self, app: ASGIApp, metrics: ApiMetrics | None = None) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Process one ASGI event (HTTP requests only; other scopes pass through)."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get(REQUEST_ID_HEADER)
        candidate = incoming if incoming and _VALID_REQUEST_ID.match(incoming) else None
        with request_context(candidate) as request_id:
            scope.setdefault("state", {})["request_id"] = request_id
            started = time.perf_counter()
            status_code = 500
            response_started = False
            logger.info("request_started", extra={"method": scope["method"], "path": scope["path"]})

            async def send_with_request_id(message: Message) -> None:
                nonlocal status_code, response_started
                if message["type"] == "http.response.start":
                    response_started = True
                    status_code = message["status"]
                    MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
                await send(message)

            try:
                await self.app(scope, receive, send_with_request_id)
            except Exception:
                logger.exception(
                    "request_failed", extra={"method": scope["method"], "path": scope["path"]}
                )
                if response_started:  # pragma: no cover - nothing left to answer with
                    raise
                response = error_response(500, INTERNAL_ERROR, INTERNAL_ERROR_MESSAGE)
                if self.metrics is not None:
                    self.metrics.count_error(INTERNAL_ERROR)
                await response(scope, receive, send_with_request_id)
            finally:
                elapsed_seconds = time.perf_counter() - started
                if self.metrics is not None:
                    self.metrics.observe_request(
                        scope["method"], _route_template(scope), status_code, elapsed_seconds
                    )
                logger.info(
                    "request_completed",
                    extra={
                        "method": scope["method"],
                        "path": scope["path"],
                        "status_code": status_code,
                        "duration_ms": elapsed_seconds * MILLISECONDS_PER_SECOND,
                    },
                )
