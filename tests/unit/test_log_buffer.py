"""In-memory ring buffer of log events (api.log_buffer)."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from rate_derivatives_pricer.api.log_buffer import LogRingBuffer, attached
from rate_derivatives_pricer.api.logging_config import NO_REQUEST_ID, request_context

LOGGER_NAME = "rate_derivatives_pricer.core.market.bootstrap"


@pytest.fixture
def quiet_root() -> Iterator[logging.Logger]:
    """Root logger at WARNING with no handler, restored afterwards."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    root.handlers[:] = []
    root.setLevel(logging.WARNING)
    try:
        yield root
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_events_are_kept_per_request_in_order(quiet_root: logging.Logger) -> None:
    buffer = LogRingBuffer(capacity=10)
    logger = logging.getLogger(LOGGER_NAME)
    with attached(buffer):
        assert quiet_root.level == logging.INFO  # lowered so that INFO events are captured
        with request_context("req-a"):
            logger.info("curve_build_completed", extra={"pillar_count": 24})
        with request_context("req-b"):
            logger.info("pricing_completed")
        with request_context("req-a"):
            logger.debug("curve_pillar_calibrated")  # below the buffer level
            logger.info("calculation_audit")
        logger.info("outside_any_request")
    assert quiet_root.level == logging.WARNING  # restored
    assert buffer not in quiet_root.handlers

    events = buffer.events_for("req-a")
    assert [e["event"] for e in events] == ["curve_build_completed", "calculation_audit"]
    assert events[0]["logger"] == "core.market.bootstrap"
    assert events[0]["pillar_count"] == 24
    assert [e["event"] for e in buffer.events_for(NO_REQUEST_ID)] == ["outside_any_request"]
    assert buffer.events_for("unknown") == []


def test_oldest_events_are_evicted_first(quiet_root: logging.Logger) -> None:
    buffer = LogRingBuffer(capacity=3)
    with attached(buffer), request_context("req"):
        for position in range(5):
            logging.getLogger(LOGGER_NAME).info(f"event_{position}")
    assert [e["event"] for e in buffer.events_for("req")] == ["event_2", "event_3", "event_4"]
    assert buffer.stats() == {"size": 3, "capacity": 3, "evicted": 2}


def test_events_are_json_safe_and_carry_no_stack_trace(quiet_root: logging.Logger) -> None:
    buffer = LogRingBuffer(capacity=10, level="DEBUG")
    logger = logging.getLogger(LOGGER_NAME)
    with attached(buffer), request_context("req"):
        assert quiet_root.level == logging.DEBUG
        logger.debug(
            "pricing_completed",
            extra={"par_rate": float("nan"), "legs": ({"pv": 1.0},), "tags": {"x"}, "obj": object},
        )
        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("request_failed")
    first, second = buffer.events_for("req")
    assert first["par_rate"] == "nan"
    assert first["legs"] == [{"pv": 1.0}]
    assert first["tags"] == ["x"]
    assert isinstance(first["obj"], str)
    assert second["exception"] == "ValueError"
    assert "exc_info" not in second
    assert "Traceback" not in str(second)


def test_capacity_must_be_positive() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        LogRingBuffer(capacity=0)
