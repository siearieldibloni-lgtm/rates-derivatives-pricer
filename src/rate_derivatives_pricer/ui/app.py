"""Streamlit dashboard (phase 1): choose a snapshot, inspect the €STR curve, price an OIS.

Run with ``uv run streamlit run src/rate_derivatives_pricer/ui/app.py`` while the API is up
(``PRICER_API_URL`` overrides the default ``http://localhost:8000``). Every number shown comes
from the API through the SDK.
"""

from __future__ import annotations

from typing import Literal, cast

import streamlit as st

from rate_derivatives_pricer.sdk.client import PricerApiError, ois_swap
from rate_derivatives_pricer.ui import views

DEFAULT_NOTIONAL = 10_000_000.0
DEFAULT_FIXED_RATE_PERCENT = 2.25
MATURITIES = ["1M", "3M", "6M", "1Y", "18M", "2Y", "3Y", "5Y", "7Y", "10Y", "15Y", "20Y", "30Y"]
DEFAULT_MATURITY = "5Y"
PERCENT = 100.0


def show_error(error: PricerApiError) -> None:
    """Display an API error with the request_id to quote to support."""
    st.error(f"{error.code}: {error.message}")
    st.caption(f"request_id: {error.request_id} | details: {error.details}")


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
    interpolation = st.selectbox("Interpolation", catalog.interpolations, key="interpolation")
    options = {"interpolation": interpolation}

curve_tab, trade_tab = st.tabs(["Market", "OIS swap"])

with curve_tab:
    try:
        curves = client.build_curves(snapshot, options)
    except PricerApiError as error:
        show_error(error)
    else:
        curve = curves.result.curves[0]
        st.subheader(f"{curve.curve_name} curve as of {curves.result.snapshot.valuation_date}")
        st.plotly_chart(views.curve_figure(curve), width="stretch")
        st.caption(
            f"{len(curve.nodes)} pillars, max |residual| {curve.max_abs_residual:.1e}, "
            f"max iterations {curve.max_iterations}, cache {curves.diagnostics.curve_cache}, "
            f"request_id {curves.request_id}"
        )
        st.dataframe(views.calibration_rows(curve), width="stretch", hide_index=True)

with trade_tab:
    with st.form("ois_swap"):
        left, middle, right = st.columns(3)
        notional = left.number_input(
            "Notional (EUR)", min_value=1.0, value=DEFAULT_NOTIONAL, step=1e6
        )
        fixed_rate = middle.number_input(
            "Fixed rate (%)", value=DEFAULT_FIXED_RATE_PERCENT, step=0.01, format="%.4f"
        )
        direction = cast(
            Literal["pay_fixed", "receive_fixed"],
            right.selectbox("Direction", ["pay_fixed", "receive_fixed"]),
        )
        maturity = left.selectbox("Maturity", MATURITIES, index=MATURITIES.index(DEFAULT_MATURITY))
        forward_start = middle.checkbox("Explicit start date (default: spot)")
        start_date = right.date_input("Start date") if forward_start else None
        submitted = st.form_submit_button("Price", key="price")

    if submitted:
        trade = ois_swap(notional, fixed_rate / PERCENT, direction, maturity, start_date=start_date)
        try:
            priced = client.price(snapshot, trade, options)
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
