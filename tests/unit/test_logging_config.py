import io
import json
import logging
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rate_derivatives_pricer.api.logging_config import (
    NO_REQUEST_ID,
    LogFormat,
    configure_logging,
    get_request_id,
    request_context,
)

# Logger name a core module gets from `logging.getLogger(__name__)`.
CORE_LOGGER_NAME = "rate_derivatives_pricer.core.market.bootstrap"


@contextmanager
def capture_logs(log_format: LogFormat = "json") -> Iterator[io.StringIO]:
    """Route the root logger to an in-memory stream, then restore the previous configuration."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    buffer = io.StringIO()
    try:
        configure_logging(level="DEBUG", log_format=log_format, stream=buffer)
        yield buffer
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


@pytest.fixture
def stream() -> Iterator[io.StringIO]:
    with capture_logs() as buffer:
        yield buffer


def read_records(buffer: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in buffer.getvalue().splitlines()]


def test_core_log_is_json_with_stable_fields(stream: io.StringIO) -> None:
    logging.getLogger(CORE_LOGGER_NAME).info(
        "curve_build_completed", extra={"duration_ms": 12.5, "currency": "EUR"}
    )

    (record,) = read_records(stream)
    assert record["level"] == "INFO"
    assert record["logger"] == "core.market.bootstrap"
    assert record["event"] == "curve_build_completed"
    assert record["duration_ms"] == 12.5
    assert record["currency"] == "EUR"
    assert str(record["timestamp"]).endswith("+00:00")


def test_request_id_is_injected_into_core_logs(stream: io.StringIO) -> None:
    core_logger = logging.getLogger(CORE_LOGGER_NAME)

    core_logger.info("outside_request")
    with request_context("abc123") as request_id:
        core_logger.info("inside_request")
    core_logger.info("after_request")

    assert request_id == "abc123"
    assert [r["request_id"] for r in read_records(stream)] == [
        NO_REQUEST_ID,
        "abc123",
        NO_REQUEST_ID,
    ]


def test_request_context_generates_an_id_when_none_given() -> None:
    with request_context() as request_id:
        assert get_request_id() == request_id
        assert len(request_id) == 32
    assert get_request_id() == NO_REQUEST_ID


def test_request_ids_are_isolated_between_threads() -> None:
    def bound_id(request_id: str) -> str:
        with request_context(request_id):
            return get_request_id()

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = [f"req-{i}" for i in range(20)]
        assert list(pool.map(bound_id, ids)) == ids


def test_exception_stack_trace_is_logged(stream: io.StringIO) -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger(CORE_LOGGER_NAME).exception("pricing_failed")

    (record,) = read_records(stream)
    assert record["level"] == "ERROR"
    assert "ValueError: boom" in str(record["exc_info"])


def test_text_format_is_human_readable() -> None:
    with capture_logs("text") as buffer, request_context("req-1"):
        logging.getLogger(CORE_LOGGER_NAME).info("pricing_completed", extra={"pv": 1.5})

    line = buffer.getvalue().strip()
    assert "[req-1] core.market.bootstrap pricing_completed pv=1.5" in line


def test_configure_logging_is_idempotent(stream: io.StringIO) -> None:
    configure_logging(level="INFO", log_format="json", stream=stream)
    logging.getLogger(CORE_LOGGER_NAME).info("once")
    assert len(read_records(stream)) == 1


@given(message=st.text(), value=st.one_of(st.integers(), st.floats(allow_nan=False), st.text()))
def test_every_record_is_one_valid_json_line(message: str, value: object) -> None:
    with capture_logs() as buffer:
        logging.getLogger(CORE_LOGGER_NAME).info(message, extra={"field": value})

    lines = buffer.getvalue().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == message
