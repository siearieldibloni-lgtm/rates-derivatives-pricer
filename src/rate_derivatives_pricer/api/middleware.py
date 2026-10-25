"""ASGI middleware binding a ``request_id`` to every request (docs/API.md section 1 and 7).

The id is taken from the ``X-Request-ID`` header when it is well formed, generated otherwise,
bound to every log record emitted while the request is processed (including from ``core``),
and echoed back in the ``X-Request-ID`` response header and in the response body.

A plain ASGI middleware (rather than Starlette's ``BaseHTTPMiddleware``) keeps the context
variable visible to the endpoint, and lets unexpected exceptions be answered with the
structured ``INTERNAL_ERROR`` format while the ``request_id`` is still bound.
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

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
# Client-supplied ids are echoed in logs and headers: accept only short, safe tokens.
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._\-]{1,128}$")
MILLISECONDS_PER_SECOND = 1000.0


class RequestContextMiddleware:
    """Bind the ``request_id``, log one ``request_completed`` event, catch unexpected errors."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

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
                await response(scope, receive, send_with_request_id)
            finally:
                logger.info(
                    "request_completed",
                    extra={
                        "method": scope["method"],
                        "path": scope["path"],
                        "status_code": status_code,
                        "duration_ms": (time.perf_counter() - started) * MILLISECONDS_PER_SECOND,
                    },
                )
