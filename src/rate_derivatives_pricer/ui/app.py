"""Streamlit dashboard: market curves, trade pricing and risk (phase 2).

Run with ``uv run streamlit run src/rate_derivatives_pricer/ui/app.py`` while the API is up
(``PRICER_API_URL`` overrides the default ``http://localhost:8000``). Every number shown comes
from the API through the SDK.

Tabs: *Market* (zero / forward / discount curves of the OIS and Ibor curves), *Trade* (OIS swap
or vanilla IRS: schedule, PV per leg) and *Risk* (DV01 per calibration instrument, per curve,
convexity).
"""

from __future__ import annotations

from typing import Literal, cast

import streamlit as st

from rate_derivatives_pricer.sdk.client import PricerApiError, ois_swap, vanilla_irs
from rate_derivatives_pricer.sdk.models import TradeModel
from rate_derivatives_pricer.ui import views

DEFAULT_NOTIONAL = 10_000_000.0
DEFAULT_FIXED_RATE_PERCENT = 2.25
MATURITIES = ["1M", "3M", "6M", "1Y", "18M", "2Y", "3Y", "5Y", "7Y", "10Y", "15Y", "20Y", "30Y"]
DEFAULT_MATURITY = "5Y"
TRADE_TYPES = ["ois_swap", "vanilla_irs"]
DEFAULT_RISK_MEASURES = ["parallel_dv01", "key_rate_dv01", "convexity"]
DEFAULT_BUMP_SIZE_BP = 1.0
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

market_tab, trade_tab, risk_tab = st.tabs(["Market", "Trade", "Risk"])

with market_tab:
    try:
        curves = client.build_curves(snapshot, options)
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

ibor_indices = next((c.ibor_indices for c in catalog.currencies if c.currency == "EUR"), [])

with trade_tab:
    with st.form("trade"):
        left, middle, right = st.columns(3)
        trade_type = left.selectbox("Product", TRADE_TYPES, key="trade_type")
        index = middle.selectbox("Ibor index (IRS)", ibor_indices or [""], key="index")
        direction = cast(
            Literal["pay_fixed", "receive_fixed"],
            right.selectbox("Direction", ["pay_fixed", "receive_fixed"]),
        )
        notional = left.number_input(
            "Notional (EUR)", min_value=1.0, value=DEFAULT_NOTIONAL, step=1e6
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
        price_clicked = st.form_submit_button("Price", key="price")
        risk_clicked = st.form_submit_button("Compute risk", key="risk")

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
            )
        return ois_swap(notional, fixed_rate / PERCENT, direction, maturity, start_date=start_date)

    if price_clicked:
        try:
            priced = client.price(snapshot, current_trade(), options)
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
            risked = client.risk(
                snapshot, current_trade(), measures, {"size_bp": bump_size_bp}, options
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
