"""Dashboard: presentation helpers and a smoke run of the Streamlit script against the API."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from rate_derivatives_pricer.sdk import PricerClient, ois_swap
from rate_derivatives_pricer.ui import views
from tests.api.conftest import SNAPSHOT

APP_SCRIPT = Path(views.__file__).with_name("app.py")
APP_TIMEOUT_SECONDS = 60


def test_views_format_api_results(client: TestClient) -> None:
    sdk = PricerClient(http_client=client)
    curve = sdk.build_curves(SNAPSHOT).result.curves[0]
    figure = views.curve_figure(curve)
    assert len(figure.data) == 4
    rows = views.calibration_rows(curve)
    assert rows[0]["pillar"] == "ON"
    assert rows[0]["market quote (%)"] == pytest.approx(1.921)

    result = sdk.price(SNAPSHOT, ois_swap(1e7, 0.0225, "pay_fixed", "2Y")).result
    cashflows = views.cashflow_rows(result)
    assert len(cashflows) == len(result.cashflows)
    summary = views.pricing_summary(result)
    assert summary["PV"].endswith("EUR")
    assert "PV fixed (pay)" in summary
    assert views.pricing_summary(result.model_copy(update={"par_rate": None}))["Par rate"] == "n/a"


def test_default_api_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(views.API_URL_ENVIRONMENT_VARIABLE, raising=False)
    assert views.default_api_url() == views.DEFAULT_API_URL
    monkeypatch.setenv(views.API_URL_ENVIRONMENT_VARIABLE, "http://api:8000")
    assert views.default_api_url() == "http://api:8000"
    assert isinstance(views.make_client("http://api:8000"), PricerClient)


def test_dashboard_runs_end_to_end(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # The script builds its client through views.make_client: route it to the in-process API.
    monkeypatch.setattr(views, "make_client", lambda base_url: PricerClient(http_client=client))
    app = AppTest.from_file(str(APP_SCRIPT), default_timeout=APP_TIMEOUT_SECONDS).run()
    assert not app.exception
    assert app.selectbox(key="snapshot").value == SNAPSHOT
    assert "EUR-ESTR" in app.subheader[0].value
    app.button(key="price").click().run()
    assert not app.exception
    assert not app.error
    assert {"PV", "Par rate", "Annuity", "PVBP"} <= {metric.label for metric in app.metric}


def test_dashboard_reports_an_unreachable_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        views,
        "make_client",
        lambda base_url: PricerClient(base_url="http://127.0.0.1:9", timeout=0.5),
    )
    app = AppTest.from_file(str(APP_SCRIPT), default_timeout=APP_TIMEOUT_SECONDS).run()
    assert app.error
    assert "unreachable" in app.error[0].value
