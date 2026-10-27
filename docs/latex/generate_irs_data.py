r"""Generate the data files used by the vanilla IRS LaTeX document (figures, tables, macros).

Every number of ``pricing_irs.tex`` that comes from the pricer is produced here by calling the
library, so the PDF never drifts from the code. Run it from anywhere:

    uv run python docs/latex/generate_irs_data.py

Outputs (next to this script): ``data/irs_*.dat`` for pgfplots and ``generated/irs_*.tex`` for
tables and the ``\newcommand`` macros of the numerical examples.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

from generate_data import tabular, tex_num, tex_sci, write  # noqa: E402

from rate_derivatives_pricer.core.conventions.day_count import Act360  # noqa: E402
from rate_derivatives_pricer.core.conventions.tenor import Tenor, TimeUnit  # noqa: E402
from rate_derivatives_pricer.core.engines.base import engine_for  # noqa: E402
from rate_derivatives_pricer.core.instruments.swaps import Instrument, Swap  # noqa: E402
from rate_derivatives_pricer.core.market.bootstrap import CurveBuilder  # noqa: E402
from rate_derivatives_pricer.core.market.providers import FileMarketDataProvider  # noqa: E402
from rate_derivatives_pricer.core.market.snapshot import MarketSnapshot  # noqa: E402
from rate_derivatives_pricer.core.registry import INSTRUMENTS  # noqa: E402
from rate_derivatives_pricer.core.risk import RiskEngine  # noqa: E402

SNAPSHOT_NAME = "EUR_2026-10-19"
PERCENT = 100.0
BASIS_POINT = 1e-4
CURVE_GRID_MONTHS = 360
EXAMPLE = {
    "notional": 10_000_000.0,
    "fixed_rate": 0.025,
    "direction": "pay_fixed",
    "maturity": "5Y",
    "index": "EURIBOR6M",
}
INDEX_LABEL = {"ESTR": "OIS", "EURIBOR6M": "Euribor 6M", "EURIBOR3M": "Euribor 3M"}


def main() -> None:
    """Produce every data file."""
    snapshot = FileMarketDataProvider(ROOT / "data" / "market").load_snapshot(SNAPSHOT_NAME)
    env = CurveBuilder().build(snapshot)
    valuation = snapshot.valuation_date
    macros: dict[str, str] = {}

    # --- Figure: zero rates of the three curves and the 6M forward of Euribor vs OIS ----------
    ois, e6, e3 = (env.projection_curve(n) for n in ("ESTR", "EURIBOR6M", "EURIBOR3M"))
    rows = ["t ois e6 e3 basis fwd6 fwdois"]
    # The Euribor curves start at their first pillar: sample from 1Y to 30Y on all three.
    for months in range(12, CURVE_GRID_MONTHS + 1):
        d = Tenor(months, TimeUnit.MONTHS).add_to(valuation)
        d6 = Tenor(6, TimeUnit.MONTHS).add_to(d)
        fwd6 = e6.forward_rate(d, d6, Act360())
        fwd_ois = ois.forward_rate(d, d6, Act360())
        rows.append(
            f"{ois.time(d):.6f} {ois.zero_rate(d) * PERCENT:.6f} {e6.zero_rate(d) * PERCENT:.6f} "
            f"{e3.zero_rate(d) * PERCENT:.6f} {(fwd6 - fwd_ois) / BASIS_POINT:.4f} "
            f"{fwd6 * PERCENT:.6f} {fwd_ois * PERCENT:.6f}"
        )
    write(HERE / "data" / "irs_curves.dat", "\n".join(rows) + "\n")

    # --- Table: calibration of the Euribor 6M curve -------------------------------------------
    report = next(r for r in env.calibration if r.index_name == "EURIBOR6M")
    curve6 = env.projection_curve("EURIBOR6M")
    kind = {"ibor_deposit": "Dépôt", "ibor_swap": "IRS"}
    table_rows = [
        f"{p.label} & {kind[p.instrument_type]} & {p.pillar_date.isoformat()} & "
        f"{tex_num(p.market_quote * PERCENT, 3)} & {tex_num(p.discount_factor, 8)} & "
        f"{tex_num(curve6.zero_rate(p.pillar_date) * PERCENT, 3)} & {p.iterations} & "
        f"{tex_sci(p.residual)} \\\\"
        for p in report.pillars
    ]
    write(
        HERE / "generated" / "irs_pillars.tex",
        tabular(
            r"@{}llcrrrcr@{}",
            r"Pilier & Type & Date & Cotation (\%) & DF & Zéro (\%) & Itér. & Résidu \\",
            table_rows,
        ),
    )
    macros["IrsPillarCount"] = str(len(report.pillars))
    macros["IrsMaxIterations"] = str(report.max_iterations)
    macros["IrsMaxResidual"] = tex_sci(report.max_abs_residual)

    # --- The example swap: priced on the full curves ------------------------------------------
    swap = INSTRUMENTS.get("vanilla_irs")(EXAMPLE, valuation)
    assert isinstance(swap, Swap)
    result = engine_for("vanilla_irs").price(swap, env)
    discount = env.discount_curve
    legs = {leg.name: leg.present_value for leg in result.legs}
    macros["IrsNotional"] = tex_num(float(EXAMPLE["notional"]), 0)
    macros["IrsFixedRate"] = tex_num(float(EXAMPLE["fixed_rate"]) * PERCENT, 2)
    macros["IrsStart"] = swap.start_date.isoformat()
    macros["IrsEnd"] = swap.end_date.isoformat()
    macros["IrsPV"] = tex_num(result.present_value, 2)
    macros["IrsFixedLegPV"] = tex_num(legs["fixed"], 2)
    macros["IrsFloatingLegPV"] = tex_num(legs["floating"], 2)
    macros["IrsParRate"] = tex_num(result.par_rate * PERCENT, 4)
    macros["IrsAnnuity"] = tex_num(result.annuity, 2)
    macros["IrsPVBP"] = tex_num(result.pvbp, 2)
    macros["IrsSpread"] = tex_num((result.par_rate - float(EXAMPLE["fixed_rate"])) / BASIS_POINT, 1)

    # --- Table: the cashflows of the example (first floating coupon is a realised fixing) ----
    cash_rows = []
    for row in result.cashflows:
        leg = "Fixe" if row.leg == "fixed" else "Flottante"
        cash_rows.append(
            f"{leg} & {row.accrual_start.isoformat()} & {row.accrual_end.isoformat()} & "
            f"{row.payment_date.isoformat()} & {tex_num(row.rate * PERCENT, 4)} & "
            f"{tex_num(row.amount, 2)} & {tex_num(row.discount_factor, 6)} & "
            f"{tex_num(row.present_value, 2)} \\\\"
        )
    write(
        HERE / "generated" / "irs_cashflows.tex",
        tabular(
            r"@{}lcccrrrr@{}",
            r"Jambe & Début & Fin & Paiement & Taux (\%) & Montant & DF OIS & PV \\",
            cash_rows,
        ),
    )
    first_floating = swap.floating_leg.cashflows[0]
    macros["IrsFirstFixingDate"] = first_floating.fixing_date.isoformat()  # type: ignore[attr-defined]
    macros["IrsFirstFixing"] = tex_num(
        (env.fixing("EURIBOR6M", first_floating.fixing_date) or 0.0) * PERCENT,
        3,  # type: ignore[attr-defined]
    )
    macros["IrsFloatingCount"] = str(len(swap.floating_leg.cashflows))
    macros["IrsFixedCount"] = str(len(swap.fixed_leg.cashflows))

    # --- Single-curve vs multi-curve: what happens if Euribor were projected on OIS -----------
    single = env.__class__(
        valuation_date=env.valuation_date,
        currency=env.currency,
        discount_curve=discount,
        projection_curves={"ESTR": discount, "EURIBOR6M": discount},
        fixings=env.fixings,
    )
    single_pv = engine_for("vanilla_irs").price(swap, single).present_value
    macros["IrsSingleCurvePV"] = tex_num(single_pv, 2)
    macros["IrsSingleCurveGap"] = tex_num(single_pv - result.present_value, 2)

    # --- Risk: parallel, per curve, convexity, key rates --------------------------------------
    builder = CurveBuilder()
    engine = engine_for("vanilla_irs")

    def pricer(instrument: Instrument, bumped: MarketSnapshot) -> float:
        horizon = instrument.last_cashflow_date if isinstance(instrument, Swap) else None
        return engine.present_value(instrument, builder.build(bumped, horizon=horizon))

    report_risk = RiskEngine(pricer).compute(
        swap, snapshot, ["parallel_dv01", "key_rate_dv01", "curve_dv01", "convexity"]
    )
    parallel = report_risk.result("parallel_dv01").value
    by_curve = {b.curve: b.value for b in report_risk.result("curve_dv01").buckets}
    macros["IrsParallelDV"] = tex_num(parallel, 2)
    macros["IrsConvexity"] = tex_num(report_risk.result("convexity").value, 3)
    macros["IrsDVOis"] = tex_num(by_curve["OIS"], 2)
    macros["IrsDVSix"] = tex_num(by_curve["EURIBOR6M"], 2)
    macros["IrsDVThree"] = tex_num(by_curve["EURIBOR3M"], 2)
    macros["IrsKeyRateSum"] = tex_num(report_risk.result("key_rate_dv01").value, 2)
    macros["IrsKeyRateGap"] = tex_sci(abs(report_risk.result("key_rate_dv01").value - parallel))
    macros["IrsRiskBumps"] = str(sum(r.bump_count for r in report_risk.results))
    macros["IrsTaylorShift"] = "10"
    shifted_pv = _shifted_pv(builder, swap, snapshot, 10.0)
    taylor = parallel * 10.0 + 0.5 * report_risk.result("convexity").value * 10.0**2
    macros["IrsTaylorActual"] = tex_num(shifted_pv - result.present_value, 2)
    macros["IrsTaylorEstimate"] = tex_num(taylor, 2)

    buckets = report_risk.result("key_rate_dv01").buckets
    labels = list(dict.fromkeys(b.label for b in buckets if b.curve == "OIS"))
    lookup = {(b.curve, b.label): b.value for b in buckets}
    rows = ["i label ois e6"]
    for i, label in enumerate(labels):
        rows.append(
            f"{i} {label} {lookup.get(('OIS', label), 0.0):.4f} "
            f"{lookup.get(('EURIBOR6M', label), 0.0):.4f}"
        )
    write(HERE / "data" / "irs_keyrate.dat", "\n".join(rows) + "\n")

    lines = ["% Generated by docs/latex/generate_irs_data.py: do not edit by hand."]
    lines += [f"\\newcommand{{\\{name}}}{{{value}}}" for name, value in macros.items()]
    write(HERE / "generated" / "irs_numbers.tex", "\n".join(lines) + "\n")


def _shifted_pv(
    builder: CurveBuilder, swap: Swap, snapshot: MarketSnapshot, shift_bp: float
) -> float:
    """PV after a parallel shift of every quote (for the second-order Taylor check)."""
    from rate_derivatives_pricer.core.risk import Bumper, BumpTarget

    bumped = Bumper().apply(snapshot, BumpTarget(), shift_bp)
    env = builder.build(bumped, horizon=swap.last_cashflow_date)
    return engine_for("vanilla_irs").present_value(swap, env)


if __name__ == "__main__":
    main()
