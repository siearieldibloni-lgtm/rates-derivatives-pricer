"""Dashboard: presentation helpers and a smoke run of the Streamlit script against the API."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from rate_derivatives_pricer.sdk import PricerClient, ois_swap
from rate_derivatives_pricer.ui import views
from tests.api.conftest import SNAPSHOT
from tests.validation import tolerances

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
        lambda base_url: PricerClient(base_url="http://127.0.0.1:9", timeout=0.5, max_retries=0),
    )
    app = AppTest.from_file(str(APP_SCRIPT), default_timeout=APP_TIMEOUT_SECONDS).run()
    assert app.error
    assert "unreachable" in app.error[0].value


# --------------------------------------------------------------------------- phase 4: diagnostic
def test_diagnostic_views(client: TestClient) -> None:
    sdk = PricerClient(http_client=client)
    priced = sdk.price(SNAPSHOT, ois_swap(1e7, 0.0225, "pay_fixed", "2Y"), request_id="ui-diag")
    entry = views.history_entry("price", priced.request_id, priced.diagnostics)
    assert entry["curve_cache"] == "miss"
    rows = views.history_rows([views.history_entry("x", "old", priced.diagnostics), entry])
    assert [row["request_id"] for row in rows] == ["ui-diag", "old"]  # most recent first
    assert rows[0]["total (ms)"] == priced.diagnostics.timings_ms["total"]
    assert "total" not in views.timings_figure(entry).data[0].x

    summary = views.cache_summary(sdk.cache_stats())
    assert summary["Entries"] == "1 / 32"
    assert summary["Hit ratio"] == "0.0 %"

    timeline = sdk.logs("ui-diag")
    table = views.log_event_rows(timeline)
    assert table[0]["t (ms)"] == 0.0
    assert [row["event"] for row in table] == [event.event for event in timeline.events]
    assert (
        '"present_value"' in next(r for r in table if r["event"] == "calculation_audit")["fields"]
    )
    figure = views.log_timeline_figure(timeline)
    assert sum(len(trace.x) for trace in figure.data) == timeline.event_count


def test_validation_report_matches_the_test_tolerances() -> None:
    rows = views.validation_rows()
    assert {row["currency"] for row in rows} == {"EUR", "USD"}
    assert all(row["within tolerance"] for row in rows)
    report = json.loads(
        (APP_SCRIPT.parent / "data" / views.VALIDATION_REPORT).read_text(encoding="utf-8")
    )
    for entry in report["entries"]:
        # The dashboard must show the tolerances the validation suite actually enforces.
        if entry["tolerance_name"] is None:
            assert entry["tolerance"] == entry["max_gap"] == 0.0
        else:
            assert entry["tolerance"] == getattr(tolerances, entry["tolerance_name"])


def test_dashboard_diagnostic_tab(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(views, "make_client", lambda base_url: PricerClient(http_client=client))
    app = AppTest.from_file(str(APP_SCRIPT), default_timeout=APP_TIMEOUT_SECONDS).run()
    assert not app.exception
    lookup = app.text_input(key="lookup_request_id")
    # The timeline of the latest call (the curve build of the Market tab) is shown by default.
    assert lookup.value == app.session_state["history"][-1]["request_id"]
    assert "Entries" in {metric.label for metric in app.metric}
    app.button(key="clear_cache").click().run()
    assert not app.exception
    assert any("dropped" in message.value for message in app.success)
    lookup.set_value("never-seen").run()
    assert any("No buffered log event" in message.value for message in app.info)
