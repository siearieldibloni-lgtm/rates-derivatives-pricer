import importlib

import pytest

import rate_derivatives_pricer

LAYER_PACKAGES = [
    "core",
    "core.conventions",
    "core.schedule",
    "core.market",
    "core.instruments",
    "core.engines",
    "core.risk",
    "core.registry",
    "services",
    "api",
    "sdk",
    "ui",
]


@pytest.mark.parametrize("name", LAYER_PACKAGES)
def test_layer_package_is_importable(name: str) -> None:
    importlib.import_module(f"rate_derivatives_pricer.{name}")


def test_version_is_exposed() -> None:
    assert rate_derivatives_pricer.__version__
