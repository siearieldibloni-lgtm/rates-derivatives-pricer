from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rate_derivatives_pricer.api.main import create_app
from rate_derivatives_pricer.api.settings import Settings

MARKET_DIR = Path(__file__).resolve().parents[2] / "data" / "market"
SNAPSHOT = "EUR_2026-10-19"


def ois_trade(**overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "notional": 10_000_000,
        "fixed_rate": 0.0225,
        "direction": "pay_fixed",
        "maturity": "5Y",
    }
    params.update(overrides)
    return {"type": "ois_swap", "params": params}


@pytest.fixture
def app() -> FastAPI:
    return create_app(Settings(market_data_dir=MARKET_DIR), configure_logs=False)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
