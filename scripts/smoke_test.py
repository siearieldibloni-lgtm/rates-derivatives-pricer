"""Smoke test of a running platform (containers in CI, or a local uvicorn).

Standard library only, so that it runs on a bare CI runner without the project installed::

    python3 scripts/smoke_test.py --api-url http://localhost:8000 --ui-url http://localhost:8501

Checks, in order: liveness and readiness, a priced OIS swap, the log timeline of that pricing
rebuilt from its request_id, a batch with one invalid trade, the curve cache statistics, the
Prometheus metrics and (optionally) the dashboard health. Exits with status 1 on the first
failure.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

logger = logging.getLogger("smoke_test")

DEFAULT_API_URL = "http://localhost:8000"
STARTUP_TIMEOUT_SECONDS = 60.0
POLL_INTERVAL_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 30.0
SNAPSHOT = "EUR_2026-10-19"
SWAP = {
    "type": "ois_swap",
    "params": {
        "notional": 10_000_000,
        "fixed_rate": 0.0225,
        "direction": "pay_fixed",
        "maturity": "5Y",
    },
}


class SmokeTestError(Exception):
    """A check failed."""


def call(
    base_url: str, method: str, path: str, body: Any = None, request_id: str | None = None
) -> tuple[int, dict[str, str], bytes]:
    """HTTP call returning status, headers and raw body (HTTP errors are returned, not raised)."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if request_id:
        headers["X-Request-ID"] = request_id
    request = urllib.request.Request(base_url + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


def check(condition: bool, message: str) -> None:
    """Fail the smoke test with ``message`` unless ``condition`` holds."""
    if not condition:
        raise SmokeTestError(message)
    logger.info("ok: %s", message)


def wait_until_ready(base_url: str, path: str) -> None:
    """Poll ``path`` until it answers 200 (the container may still be starting)."""
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while True:
        try:
            if call(base_url, "GET", path)[0] == 200:
                return
        except OSError:
            pass  # connection refused while the server starts
        if time.monotonic() > deadline:
            raise SmokeTestError(f"{base_url}{path} not ready after {STARTUP_TIMEOUT_SECONDS}s")
        time.sleep(POLL_INTERVAL_SECONDS)


def smoke_api(base_url: str) -> None:
    """Exercise the API end to end."""
    wait_until_ready(base_url, "/health")
    status, _, body = call(base_url, "GET", "/ready")
    check(status == 200 and json.loads(body)["status"] == "ready", "GET /ready is ready")
    version = json.loads(call(base_url, "GET", "/v1/version")[2])["result"]
    logger.info("version: %s", version)

    request_id = f"smoke-{uuid.uuid4().hex[:12]}"
    status, headers, body = call(
        base_url,
        "POST",
        "/v1/price",
        {"market": {"snapshot": SNAPSHOT}, "trade": SWAP},
        request_id=request_id,
    )
    priced = json.loads(body)
    check(status == 200, f"POST /v1/price answers 200 (got {status})")
    check(priced["request_id"] == request_id, "the request_id is echoed in the body")
    check(headers.get("X-Request-ID", headers.get("x-request-id")) == request_id, "and header")
    present_value = priced["result"]["present_value"]
    check(isinstance(present_value, float), f"the swap has a PV ({present_value:,.2f} EUR)")

    status, _, body = call(base_url, "GET", f"/v1/logs/{request_id}")
    events = [event["event"] for event in json.loads(body)["result"]["events"]]
    check(status == 200, "GET /v1/logs/{request_id} answers 200")
    check(
        {"request_started", "pricing_completed", "calculation_audit", "request_completed"}
        <= set(events),
        f"the timeline of the pricing is rebuilt from its request_id ({len(events)} events)",
    )

    trades = [{**SWAP, "id": "good"}, {"type": "cap", "params": {}, "id": "bad"}]
    status, _, body = call(
        base_url, "POST", "/v1/batch/price", {"market": {"snapshot": SNAPSHOT}, "trades": trades}
    )
    summary = json.loads(body)["result"]["summary"]
    check(status == 200, "POST /v1/batch/price answers 200 with partial results")
    check((summary["succeeded"], summary["failed"]) == (1, 1), "an invalid trade fails alone")

    cache = json.loads(call(base_url, "GET", "/v1/cache/stats")[2])["result"]
    check(cache["size"] >= 1 and cache["hits"] >= 1, "the curve cache served the batch")

    status, _, body = call(base_url, "GET", "/metrics")
    check(status == 200 and b"pricer_http_requests_total" in body, "GET /metrics exposes metrics")


def smoke_ui(ui_url: str) -> None:
    """Check that the dashboard server answers its health probe."""
    wait_until_ready(ui_url, "/_stcore/health")
    check(call(ui_url, "GET", "/_stcore/health")[0] == 200, "the dashboard is healthy")


def main() -> int:
    """Run the smoke test; return the process exit status."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument("--ui-url", default=None, help="dashboard URL (skipped when omitted)")
    arguments = parser.parse_args()
    try:
        smoke_api(arguments.api_url.rstrip("/"))
        if arguments.ui_url:
            smoke_ui(arguments.ui_url.rstrip("/"))
    except (SmokeTestError, OSError, KeyError, ValueError) as error:
        logger.error("smoke test failed: %s", error)
        return 1
    logger.info("smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
