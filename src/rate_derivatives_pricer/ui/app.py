"""Streamlit dashboard: market curves, trade pricing, risk, scenarios, carry and diagnostics.

Run with ``uv run streamlit run src/rate_derivatives_pricer/ui/app.py`` while the API is up
(``PRICER_API_URL`` overrides the default ``http://localhost:8000``). Every number shown comes
from the API through the SDK.

Tabs: *Market* (zero / forward / discount curves of the OIS and Ibor curves), *Trade* (OIS swap
or vanilla IRS: schedule, PV per leg), *Risk* (DV01 per calibration instrument, per curve,
convexity), *Scenarios* (P&L and shift matrix of the preset scenarios), *Carry* (carry and
roll-down over a horizon), *Compare* (the trade and the curves on two snapshots) and
*Diagnostic* (timings and cache status of the recent calls, curve cache, log timeline of a
``request_id``, gaps against QuantLib). The trade currency is the currency of the selected
snapshot (EUR or USD).
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, cast

import streamlit as st

from rate_derivatives_pricer.sdk.client import (
    NotFoundError,
    PricerApiError,
    ois_swap,
    vanilla_irs,
)
from rate_derivatives_pricer.sdk.models import Diagnostics, TradeModel
from rate_derivatives_pricer.ui import views

DEFAULT_NOTIONAL = 10_000_000.0
DEFAULT_FIXED_RATE_PERCENT = 2.25
MATURITIES = ["1M", "3M", "6M", "1Y", "18M", "2Y", "3Y", "5Y", "7Y", "10Y", "15Y", "20Y", "30Y"]
DEFAULT_MATURITY = "5Y"
TRADE_TYPES = ["ois_swap", "vanilla_irs"]
DEFAULT_RISK_MEASURES = ["parallel_dv01", "key_rate_dv01", "convexity"]
DEFAULT_BUMP_SIZE_BP = 1.0
CARRY_HORIZONS = ["1M", "3M", "6M", "1Y"]
DEFAULT_CARRY_HORIZON = "3M"
PERCENT = 100.0
# Recent API calls kept for the Diagnostic tab (per browser session).
HISTORY_LENGTH = 20

history: list[dict[str, Any]] = st.session_state.setdefault("history", [])


class ApiResponse(Protocol):
    """Response envelope of the API: what the Diagnostic tab keeps of each call."""

    request_id: str
    diagnostics: Diagnostics


def remember[ResponseT: ApiResponse](endpoint: str, envelope: ResponseT) -> ResponseT:
    """Keep the diagnostics of a response for the Diagnostic tab, and return the response."""
    history.append(views.history_entry(endpoint, envelope.request_id, envelope.diagnostics))
    del history[:-HISTORY_LENGTH]
    return envelope


def show_error(error: PricerApiError) -> None:
    """Display an API error with the request_id to quote to support."""
    st.error(f"{error.code}: {error.message}")
    st.caption(f"request_id: {error.request_id} | details: {error.details}")
    if error.request_id:
        history.append(views.history_entry(f"error {error.code}", error.request_id, Diagnostics()))
        del history[:-HISTORY_LENGTH]


st.set_page_config(page_title="Rate Derivatives Pricer", layout="wide")
st.title("Rate Derivatives Pricer")

with st.sidebar:
    st.header("Connection")
    api_url = st.text_input("API URL", value=views.default_api_url())
    client = views.make_client(api_url)
    try:
        catalog = client.catalog().result
        snapshots = client.list_snapshots()
    except PricerApiError as error:
        st.error(f"API unreachable at {api_url}: {error}")
        st.stop()
    st.header("Market")
    snapshot = st.selectbox("Snapshot", snapshots, key="snapshot")
    try:
        currencies = {name: client.get_snapshot(name).currency for name in snapshots}
    except PricerApiError as error:
        st.error(f"{error.code}: {error.message}")
        st.stop()
    currency = currencies[snapshot]
    interpolation = st.selectbox("Interpolation", catalog.interpolations, key="interpolation")
    options = {"interpolation": interpolation}
    st.caption(f"Trade currency: {currency}")

market_tab, trade_tab, risk_tab, scenario_tab, carry_tab, compare_tab, diagnostic_tab = st.tabs(
    ["Market", "Trade", "Risk", "Scenarios", "Carry", "Compare", "Diagnostic"]
)

with market_tab:
    try:
        curves = remember("curves/build", client.build_curves(snapshot, options))
    except PricerApiError as error:
        show_error(error)
    else:
        by_name = {c.curve_name: c for c in curves.result.curves}
        st.dataframe(views.curve_rows(curves.result.curves), width="stretch", hide_index=True)
        curve_name = st.selectbox("Curve", list(by_name), key="curve")
        curve = by_name[curve_name]
        st.subheader(f"{curve.curve_name} curve as of {curves.result.snapshot.valuation_date}")
        st.plotly_chart(views.curve_figure(curve), width="stretch")
        st.caption(
            f"{len(curve.nodes)} pillars, max |residual| {curve.max_abs_residual:.1e}, "
            f"max iterations {curve.max_iterations}, cache {curves.diagnostics.curve_cache}, "
            f"request_id {curves.request_id}"
        )
        st.dataframe(views.calibration_rows(curve), width="stretch", hide_index=True)

ibor_indices = next((c.ibor_indices for c in catalog.currencies if c.currency == currency), [])
# A currency without Ibor index (USD: SOFR only) trades OIS only.
trade_types = TRADE_TYPES if ibor_indices else ["ois_swap"]
# Other snapshots of the same currency, for the comparison (no FX in v1).
same_currency = [name for name in snapshots if name != snapshot and currencies[name] == currency]

with trade_tab:
    with st.form("trade"):
        left, middle, right = st.columns(3)
        trade_type = left.selectbox("Product", trade_types, key="trade_type")
        index = middle.selectbox("Ibor index (IRS)", ibor_indices or [""], key="index")
        direction = cast(
            Literal["pay_fixed", "receive_fixed"],
            right.selectbox("Direction", ["pay_fixed", "receive_fixed"]),
        )
        notional = left.number_input(
            f"Notional ({currency})", min_value=1.0, value=DEFAULT_NOTIONAL, step=1e6
        )
        fixed_rate = middle.number_input(
            "Fixed rate (%)", value=DEFAULT_FIXED_RATE_PERCENT, step=0.01, format="%.4f"
        )
        maturity = right.selectbox("Maturity", MATURITIES, index=MATURITIES.index(DEFAULT_MATURITY))
        forward_start = left.checkbox("Explicit start date (default: spot)")
        start_date = middle.date_input("Start date") if forward_start else None
        measures = st.multiselect(
            "Risk measures",
            catalog.risk_measures,
            default=[m for m in DEFAULT_RISK_MEASURES if m in catalog.risk_measures],
            key="measures",
        )
        bump_size_bp = st.number_input(
            "Bump size (bp)", min_value=0.01, value=DEFAULT_BUMP_SIZE_BP, key="bump_size_bp"
        )
        left, middle, right = st.columns(3)
        scenario_names = left.multiselect(
            "Scenarios",
            [s.name for s in catalog.scenarios],
            default=[s.name for s in catalog.scenarios],
            key="scenario_names",
        )
        horizon = middle.selectbox(
            "Carry horizon",
            CARRY_HORIZONS,
            index=CARRY_HORIZONS.index(DEFAULT_CARRY_HORIZON),
            key="horizon",
        )
        other_snapshot = right.selectbox(
            "Compare with snapshot", same_currency or [""], key="other_snapshot"
        )
        buttons = st.columns(5)
        price_clicked = buttons[0].form_submit_button("Price", key="price")
        risk_clicked = buttons[1].form_submit_button("Compute risk", key="risk")
        scenarios_clicked = buttons[2].form_submit_button("Run scenarios", key="scenarios")
        carry_clicked = buttons[3].form_submit_button("Carry / roll-down", key="carry")
        compare_clicked = buttons[4].form_submit_button("Compare snapshots", key="compare")

    def current_trade() -> TradeModel:
        """Trade described by the form (rates entered in percent, sent as decimals)."""
        if trade_type == "vanilla_irs":
            return vanilla_irs(
                notional,
                fixed_rate / PERCENT,
                direction,
                maturity,
                index=index or None,
                start_date=start_date,
                currency=currency,
            )
        return ois_swap(
            notional,
            fixed_rate / PERCENT,
            direction,
            maturity,
            start_date=start_date,
            currency=currency,
        )

    if price_clicked:
        try:
            priced = remember("price", client.price(snapshot, current_trade(), options))
        except PricerApiError as error:
            show_error(error)
        else:
            columns = st.columns(4)
            for column, (label, value) in zip(
                columns * 2, views.pricing_summary(priced.result).items(), strict=False
            ):
                column.metric(label, value)
            st.dataframe(views.cashflow_rows(priced.result), width="stretch", hide_index=True)
            st.caption(
                f"request_id {priced.request_id} | timings (ms) {priced.diagnostics.timings_ms} "
                f"| cache {priced.diagnostics.curve_cache}"
            )

with risk_tab:
    if not risk_clicked:
        st.info("Define a trade in the Trade tab and press 'Compute risk'.")
    elif not measures:
        st.warning("Select at least one risk measure.")
    else:
        try:
            risked = remember(
                "risk",
                client.risk(
                    snapshot, current_trade(), measures, {"size_bp": bump_size_bp}, options
                ),
            )
        except PricerApiError as error:
            show_error(error)
        else:
            columns = st.columns(4)
            for column, (label, value) in zip(
                columns * 2, views.risk_summary(risked.result).items(), strict=False
            ):
                column.metric(label, value)
            for measure in risked.result.measures:
                if measure.buckets:
                    st.subheader(measure.measure)
                    st.plotly_chart(views.risk_figure(measure), width="stretch")
                    st.dataframe(views.risk_bucket_rows(measure), width="stretch", hide_index=True)
            st.caption(
                f"request_id {risked.request_id} | timings (ms) {risked.diagnostics.timings_ms}"
            )

with scenario_tab:
    if not scenarios_clicked:
        st.info("Define a trade in the Trade tab and press 'Run scenarios'.")
    elif not scenario_names:
        st.warning("Select at least one scenario.")
    else:
        try:
            shocked = remember(
                "risk/scenarios",
                client.scenarios(snapshot, current_trade(), scenario_names, options=options),
            )
        except PricerApiError as error:
            show_error(error)
        else:
            st.metric("Base PV", f"{shocked.result.present_value:,.2f} {shocked.result.currency}")
            st.plotly_chart(views.scenario_pnl_figure(shocked.result), width="stretch")
            st.dataframe(views.scenario_rows(shocked.result), width="stretch", hide_index=True)
            st.subheader("Scenario matrix: par-rate shifts by tenor (bp)")
            st.plotly_chart(views.scenario_shift_matrix(shocked.result), width="stretch")
            st.caption(
                f"request_id {shocked.request_id} | timings (ms) {shocked.diagnostics.timings_ms}"
            )

with carry_tab:
    if not carry_clicked:
        st.info("Define a trade in the Trade tab and press 'Carry / roll-down'.")
    else:
        try:
            carried = remember(
                "risk/carry", client.carry_rolldown(snapshot, current_trade(), horizon, options)
            )
        except PricerApiError as error:
            show_error(error)
        else:
            columns = st.columns(4)
            for column, (label, value) in zip(
                columns * 2, views.carry_summary(carried.result).items(), strict=False
            ):
                column.metric(label, value)
            st.plotly_chart(views.carry_figure(carried.result), width="stretch")
            assumed = ", ".join(
                f"{name} {rate * PERCENT:.3f} %"
                for name, rate in carried.result.assumed_fixings.items()
            )
            st.caption(
                "Hypothesis: unchanged curve (par rates by tenor kept, fixings over the horizon "
                f"at today's quotes: {assumed}). Carry = net accrual, roll-down = total - carry. "
                f"request_id {carried.request_id}"
            )

with compare_tab:
    if not compare_clicked:
        st.info("Choose a second snapshot in the Trade tab and press 'Compare snapshots'.")
    elif not other_snapshot:
        st.warning(f"No other {currency} snapshot to compare with.")
    else:
        try:
            base_price = remember("price", client.price(snapshot, current_trade(), options))
            other_price = remember("price", client.price(other_snapshot, current_trade(), options))
            base_curves = client.build_curves(snapshot, options)
            other_curves = client.build_curves(other_snapshot, options)
        except PricerApiError as error:
            show_error(error)
        else:
            rows = views.snapshot_comparison_rows(
                base_price.result, other_price.result, snapshot, other_snapshot
            )
            st.dataframe(rows, width="stretch", hide_index=True)
            st.subheader("Change of the calibration quotes")
            st.plotly_chart(
                views.curve_change_figure(
                    base_curves.result.curves,
                    other_curves.result.curves,
                    snapshot,
                    other_snapshot,
                ),
                width="stretch",
            )
            st.caption(
                f"request_ids {base_price.request_id}, {other_price.request_id} | the trade keeps "
                "its dates: valued on a later snapshot it is one day older"
            )

with diagnostic_tab:
    st.subheader("Recent API calls")
    if history:
        st.dataframe(views.history_rows(history), width="stretch", hide_index=True)
        request_ids = [entry["request_id"] for entry in reversed(history)]
        selected_id = st.selectbox("Request", request_ids, key="diagnostic_request")
        selected = next(entry for entry in history if entry["request_id"] == selected_id)
        if selected["timings_ms"]:
            st.plotly_chart(views.timings_figure(selected), width="stretch")
    else:
        selected_id = ""
        st.info("No API call yet in this session.")

    st.subheader("Curve cache")
    try:
        cache = client.cache_stats()
    except PricerApiError as error:
        show_error(error)
    else:
        columns = st.columns(4)
        for column, (label, value) in zip(columns, views.cache_summary(cache).items(), strict=True):
            column.metric(label, value)
        st.caption(
            f"TTL: {cache.ttl_seconds or 'none'} | per process: each API worker has its own cache"
        )
        if st.button("Clear curve cache", key="clear_cache"):
            try:
                cleared = client.clear_cache()
            except PricerApiError as error:
                show_error(error)
            else:
                st.success(f"{cleared.entries_dropped} cached market(s) dropped")

    st.subheader("Log timeline")
    lookup_id = st.text_input(
        "request_id (from a response, an error or the X-Request-ID header)",
        value=selected_id,
        key="lookup_request_id",
    ).strip()
    if lookup_id:
        try:
            timeline = client.logs(lookup_id)
        except NotFoundError:
            st.info(f"No buffered log event for {lookup_id} (unknown, evicted or other worker).")
        except PricerApiError as error:
            show_error(error)
        else:
            st.caption(
                f"{timeline.event_count} events over {timeline.duration_ms:,.1f} ms | buffer "
                f"{timeline.buffer.size}/{timeline.buffer.capacity} events"
            )
            st.plotly_chart(views.log_timeline_figure(timeline), width="stretch")
            st.dataframe(views.log_event_rows(timeline), width="stretch", hide_index=True)

    st.subheader("Validation against QuantLib")
    st.dataframe(views.validation_rows(), width="stretch", hide_index=True)
    st.caption(
        "Largest gaps measured by the validation suite (uv run pytest -m validation); QuantLib "
        "is a test oracle only and is not installed with the dashboard."
    )
