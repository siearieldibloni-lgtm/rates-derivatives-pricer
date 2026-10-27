from __future__ import annotations

import pytest

from rate_derivatives_pricer.core.errors import ConventionError, UnsupportedInstrumentError
from rate_derivatives_pricer.core.registry import (
    CALENDARS,
    CONVENTIONS,
    DAY_COUNTS,
    ENGINES,
    INSTRUMENTS,
    INTERPOLATORS,
    Registry,
)


def test_builtin_components_are_registered() -> None:
    assert {"TARGET", "NULL", "WEEKENDS_ONLY", "US_GOVERNMENT_BOND", "US_SOFR"} <= set(
        CALENDARS.keys()
    )
    assert set(DAY_COUNTS.keys()) == {"ACT/360", "ACT/365F", "30/360", "30E/360"}
    assert set(INTERPOLATORS.keys()) == {"log_linear_df", "linear_zero"}
    assert "ois_swap" in INSTRUMENTS
    assert ("ois_swap", "discounting") in ENGINES
    assert list(CONVENTIONS) == ["EUR", "USD"]
    assert len(CONVENTIONS) == 2
    assert INSTRUMENTS.kind == "instrument type"


def test_unknown_keys_raise_the_registry_error_type() -> None:
    with pytest.raises(UnsupportedInstrumentError) as error:
        INSTRUMENTS.get("swaption")
    assert error.value.details["available"] == ["ois_swap", "vanilla_irs"]
    with pytest.raises(ConventionError):
        INTERPOLATORS.get("cubic")


def test_registration_by_decorator_and_duplicates() -> None:
    registry: Registry[str, type] = Registry("widget", ConventionError)

    @registry.register("a")
    class Widget:
        pass

    assert registry.get("a") is Widget
    registry.add("a", Widget)  # re-registering the same object is harmless
    with pytest.raises(ValueError, match="duplicate"):
        registry.add("a", int)


def test_failed_loader_can_be_retried() -> None:
    calls: list[int] = []

    def loader() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient")
        registry.add("x", 1)

    registry: Registry[str, int] = Registry("number", ConventionError, loader)
    with pytest.raises(RuntimeError):
        registry.keys()
    assert registry.get("x") == 1
    assert len(calls) == 2
