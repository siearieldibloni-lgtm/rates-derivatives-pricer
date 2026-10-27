"""Presentation helpers of the dashboard: tables and Plotly figures built from SDK results.

Kept free of Streamlit calls so that they can be unit-tested; the UI never computes prices,
it only formats what the API returns (docs/API.md section 1).
"""

from __future__ import annotations

import os
from typing import Any

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from rate_derivatives_pricer.sdk.client import PricerClient
from rate_derivatives_pricer.sdk.models import (
    CurveResult,
    PriceResult,
    RiskMeasureResult,
    RiskResult,
)

API_URL_ENVIRONMENT_VARIABLE = "PRICER_API_URL"
DEFAULT_API_URL = "http://localhost:8000"
PERCENT = 100.0
BASIS_POINTS = 10_000.0


def default_api_url() -> str:
    """API URL from ``PRICER_API_URL``, or the local default."""
    return os.environ.get(API_URL_ENVIRONMENT_VARIABLE, DEFAULT_API_URL)


def make_client(base_url: str) -> PricerClient:
    """SDK client used by the dashboard (single construction point, patched in tests)."""
    return PricerClient(base_url=base_url)


def curve_figure(curve: CurveResult) -> go.Figure:
    """Zero and 1M forward rates (in %) above discount factors, with the calibration pillars."""
    figure = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.08,
        subplot_titles=("Zero and forward rates (%)", "Discount factors"),
    )
    dates = [point.date for point in curve.grid]
    figure.add_trace(
        go.Scatter(
            x=dates, y=[p.zero_rate * PERCENT for p in curve.grid], name="Zero (cont., ACT/365F)"
        ),
        row=1,
        col=1,
    )
    figure.add_trace(
        go.Scatter(
            x=dates,
            y=[p.forward_rate * PERCENT for p in curve.grid],
            name="1M forward (ACT/360)",
            line={"dash": "dot"},
        ),
        row=1,
        col=1,
    )
    figure.add_trace(
        go.Scatter(
            x=[node.pillar_date for node in curve.nodes],
            y=[node.zero_rate * PERCENT for node in curve.nodes],
            mode="markers",
            name="Pillars",
            text=[node.label for node in curve.nodes],
        ),
        row=1,
        col=1,
    )
    figure.add_trace(
        go.Scatter(x=dates, y=[p.discount_factor for p in curve.grid], name="Discount factor"),
        row=2,
        col=1,
    )
    figure.update_layout(height=600, margin={"t": 40, "b": 20}, legend={"orientation": "h"})
    return figure


def calibration_rows(curve: CurveResult) -> list[dict[str, Any]]:
    """Calibration table: one row per pillar (rates in %, residual in bp)."""
    return [
        {
            "pillar": node.label,
            "instrument": node.instrument_type,
            "pillar date": node.pillar_date.isoformat(),
            "market quote (%)": node.market_quote * PERCENT,
            "residual (bp)": node.residual * BASIS_POINTS,
            "iterations": node.iterations,
            "discount factor": node.discount_factor,
            "zero rate (%)": node.zero_rate * PERCENT,
        }
        for node in curve.nodes
    ]


def cashflow_rows(result: PriceResult) -> list[dict[str, Any]]:
    """Cashflow table (signed amounts: positive = received)."""
    return [
        {
            "leg": row.leg,
            "accrual start": row.accrual_start.isoformat(),
            "accrual end": row.accrual_end.isoformat(),
            "payment date": row.payment_date.isoformat(),
            "rate (%)": row.rate * PERCENT,
            "amount": row.amount,
            "discount factor": row.discount_factor,
            "present value": row.present_value,
        }
        for row in result.cashflows
    ]


def pricing_summary(result: PriceResult) -> dict[str, str]:
    """Headline figures of a pricing, formatted for display."""
    par_rate = "n/a" if result.par_rate is None else f"{result.par_rate * PERCENT:.5f} %"
    summary = {
        "PV": f"{result.present_value:,.2f} {result.currency}",
        "Par rate": par_rate,
        "Annuity": f"{result.annuity:,.2f}",
        "PVBP": f"{result.pvbp:,.2f} {result.currency}",
    }
    for leg in result.legs:
        summary[f"PV {leg.name} ({leg.direction})"] = f"{leg.present_value:,.2f} {result.currency}"
    return summary


def curve_rows(curves: list[CurveResult]) -> list[dict[str, Any]]:
    """One row per curve: name, role and calibration quality."""
    return [
        {
            "curve": curve.curve_name,
            "role": curve.role,
            "pillars": len(curve.nodes),
            "max |residual| (bp)": curve.max_abs_residual * BASIS_POINTS,
            "max iterations": curve.max_iterations,
        }
        for curve in curves
    ]


def risk_figure(measure: RiskMeasureResult) -> go.Figure:
    """Bar chart of a bucketed measure (DV01 per calibration instrument), one series per curve."""
    figure = go.Figure()
    # Union of the labels in order of first appearance (the overnight curve lists every tenor).
    labels = list(dict.fromkeys(bucket.label for bucket in measure.buckets))
    for curve in dict.fromkeys(bucket.curve for bucket in measure.buckets):
        buckets = [bucket for bucket in measure.buckets if bucket.curve == curve]
        figure.add_trace(
            go.Bar(x=[b.label for b in buckets], y=[b.value for b in buckets], name=curve)
        )
    figure.update_layout(
        barmode="group",
        height=420,
        margin={"t": 30, "b": 20},
        legend={"orientation": "h"},
        xaxis={
            "title": "Calibration instrument",
            "categoryorder": "array",
            "categoryarray": labels,
        },
        yaxis={"title": measure.unit},
    )
    return figure


def risk_bucket_rows(measure: RiskMeasureResult) -> list[dict[str, Any]]:
    """Bucket table of a measure."""
    return [
        {
            "curve": bucket.curve,
            "instrument": bucket.instrument_type,
            "bucket": bucket.label,
            measure.unit: bucket.value,
        }
        for bucket in measure.buckets
    ]


def risk_summary(result: RiskResult) -> dict[str, str]:
    """Headline figures of a risk run, formatted for display."""
    summary = {"PV": f"{result.present_value:,.2f} {result.currency}"}
    for measure in result.measures:
        summary[measure.measure] = f"{measure.value:,.4f} {measure.unit}"
    return summary
