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
    CarryResult,
    CurveResult,
    PriceResult,
    RiskMeasureResult,
    RiskResult,
    ScenariosResult,
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


# --------------------------------------------------------------------------- phase 3
def scenario_rows(result: ScenariosResult) -> list[dict[str, Any]]:
    """Scenario table: PV and P&L under each scenario."""
    return [
        {
            "scenario": entry.name,
            "description": entry.description,
            "curves": ", ".join(entry.curves) if entry.curves else "all",
            "present value": entry.present_value,
            "P&L": entry.pnl,
        }
        for entry in result.scenarios
    ]


def scenario_pnl_figure(result: ScenariosResult) -> go.Figure:
    """Bar chart of the P&L of each scenario (trade currency)."""
    figure = go.Figure(
        go.Bar(
            x=[entry.name for entry in result.scenarios],
            y=[entry.pnl for entry in result.scenarios],
            marker_color=[
                "seagreen" if entry.pnl >= 0.0 else "indianred" for entry in result.scenarios
            ],
        )
    )
    figure.update_layout(
        height=380,
        margin={"t": 30, "b": 20},
        yaxis={"title": f"P&L ({result.currency})"},
    )
    return figure


def scenario_shift_matrix(result: ScenariosResult) -> go.Figure:
    """Heatmap of the par-rate shifts (bp): one row per scenario, one column per tenor.

    Columns are the tenors of the overnight (OIS) curve quotes, short to long; a scenario that
    skips the OIS curve shows its shifts on the first curve it shocks.
    """
    columns: list[str] = []
    rows: list[list[float]] = []
    for entry in result.scenarios:
        curves = list(dict.fromkeys(shift.curve for shift in entry.shifts))
        shocked = [c for c in curves if any(s.shift_bp for s in entry.shifts if s.curve == c)]
        curve = "OIS" if "OIS" in shocked or not shocked else shocked[0]
        by_label = {s.label: s.shift_bp for s in entry.shifts if s.curve == curve}
        if not columns:
            columns = [s.label for s in entry.shifts if s.curve == "OIS"] or list(by_label)
        rows.append([by_label.get(label, 0.0) for label in columns])
    figure = go.Figure(
        go.Heatmap(
            z=rows,
            x=columns,
            y=[entry.name for entry in result.scenarios],
            colorscale="RdBu",
            reversescale=True,
            zmid=0.0,
            colorbar={"title": "bp"},
        )
    )
    figure.update_layout(height=320, margin={"t": 30, "b": 20}, xaxis={"title": "Tenor"})
    return figure


def carry_summary(result: CarryResult) -> dict[str, str]:
    """Headline figures of a carry/roll-down, formatted for display."""
    unit = result.currency
    return {
        "Horizon": f"{result.horizon} ({result.horizon_date.isoformat()})",
        "Carry": f"{result.carry:,.2f} {unit}",
        "Roll-down": f"{result.rolldown:,.2f} {unit}",
        "Total": f"{result.total:,.2f} {unit}",
        "PV today": f"{result.present_value:,.2f} {unit}",
        "PV at horizon": f"{result.horizon_present_value:,.2f} {unit}",
        "Cashflows paid": f"{result.cashflows:,.2f} {unit}",
    }


def carry_figure(result: CarryResult) -> go.Figure:
    """Waterfall: carry plus roll-down give the total P&L over the horizon."""
    figure = go.Figure(
        go.Waterfall(
            x=["Carry", "Roll-down", "Total"],
            y=[result.carry, result.rolldown, result.total],
            measure=["relative", "relative", "total"],
        )
    )
    figure.update_layout(
        height=380, margin={"t": 30, "b": 20}, yaxis={"title": f"P&L ({result.currency})"}
    )
    return figure


def snapshot_comparison_rows(
    base: PriceResult, other: PriceResult, base_name: str, other_name: str
) -> list[dict[str, Any]]:
    """Same trade on two markets: PV, par rate and PV per leg side by side, with the change."""

    def figures(result: PriceResult) -> dict[str, float]:
        values = {"PV": result.present_value, "PVBP": result.pvbp}
        if result.par_rate is not None:
            values["par rate (%)"] = result.par_rate * PERCENT
        values.update({f"PV {leg.name}": leg.present_value for leg in result.legs})
        return values

    base_values, other_values = figures(base), figures(other)
    return [
        {
            "figure": name,
            base_name: base_values[name],
            other_name: other_values[name],
            "change": other_values[name] - base_values[name],
        }
        for name in base_values
        if name in other_values
    ]


def curve_change_figure(
    base: list[CurveResult], other: list[CurveResult], base_name: str, other_name: str
) -> go.Figure:
    """Change of the calibrated par quotes (bp) between two snapshots, one series per curve."""
    figure = go.Figure()
    other_by_name = {curve.curve_name: curve for curve in other}
    for curve in base:
        counterpart = other_by_name.get(curve.curve_name)
        if counterpart is None:
            continue
        quotes = {node.label: node.market_quote for node in counterpart.nodes}
        labels = [node.label for node in curve.nodes if node.label in quotes]
        figure.add_trace(
            go.Scatter(
                x=labels,
                y=[
                    (quotes[node.label] - node.market_quote) * BASIS_POINTS
                    for node in curve.nodes
                    if node.label in quotes
                ],
                mode="lines+markers",
                name=curve.curve_name,
            )
        )
    figure.update_layout(
        height=380,
        margin={"t": 30, "b": 20},
        legend={"orientation": "h"},
        xaxis={"title": "Calibration instrument"},
        yaxis={"title": f"{other_name} - {base_name} (bp)"},
    )
    return figure
