r"""Generate the data files used by the LaTeX document (figures, tables, numeric macros).

Every number of the document that comes from the pricer is produced here by calling the
library, so the PDF never drifts from the code. Run it from anywhere:

    uv run python docs/latex/generate_data.py

Outputs (next to this script): ``data/*.dat`` for pgfplots and ``generated/*.tex`` for tables
and the ``\newcommand`` macros of the numerical examples.
"""

from __future__ import annotations

import itertools
import sys
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
# The QuantLib oracle lives in the test suite (repository root on the path).
sys.path.insert(0, str(ROOT))

from tests.validation.quantlib_oracle import (  # noqa: E402
    build_estr_curve,
    evaluation_date,
    to_ql,
)

from rate_derivatives_pricer.core.conventions.calendar import TargetCalendar  # noqa: E402
from rate_derivatives_pricer.core.conventions.day_count import Act360  # noqa: E402
from rate_derivatives_pricer.core.conventions.tenor import Tenor, TimeUnit  # noqa: E402
from rate_derivatives_pricer.core.engines.base import engine_for  # noqa: E402
from rate_derivatives_pricer.core.market.bootstrap import (  # noqa: E402
    CurveBuilder,
    CurveBuildOptions,
)
from rate_derivatives_pricer.core.market.providers import FileMarketDataProvider  # noqa: E402
from rate_derivatives_pricer.core.registry import INSTRUMENTS  # noqa: E402

SNAPSHOT_NAME = "EUR_2026-10-19"
PERCENT = 100.0
CURVE_GRID_MONTHS = 360
QUANTLIB_SAMPLE_STEP_DAYS = 33
QUANTLIB_HORIZON_DAYS = 40 * 365
# Differences below this are plotted at the floor (a log axis cannot show exact zeros).
LOG_FLOOR = 1e-17
COMPARISON_RATE = 0.0225
COMPARISON_NOTIONAL = 10_000_000.0
EXAMPLE_NOTIONAL = 10_000_000.0
EXAMPLE_FIXED_RATE = 0.02
EXAMPLE_TENOR = "2Y"


def write(path: Path, text: str) -> None:
    """Write ``text`` as UTF-8 with Unix line endings, creating the folder if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def tex_num(value: float, digits: int = 2) -> str:
    """Format a number for LaTeX with a thin-space thousands separator and a decimal comma."""
    text = f"{value:,.{digits}f}".replace(",", r"\,").replace(".", "{,}")
    return text.replace("-", "$-$")


def tabular(column_spec: str, header: str, rows: list[str]) -> str:
    r"""Build a complete booktabs ``tabular`` (so it can be ``\input`` outside any alignment)."""
    body = "\n".join(rows)
    return (
        f"\\begin{{tabular}}{{{column_spec}}}\n\\toprule\n{header}\n\\midrule\n{body}\n"
        "\\bottomrule\n\\end{tabular}\n"
    )


def tex_sci(value: float, digits: int = 1) -> str:
    r"""Format a number in scientific notation for LaTeX (``1{,}2\times10^{-15}``)."""
    if value == 0.0:
        return "$0$"
    mantissa, exponent = f"{value:.{digits}e}".split("e")
    sign = "-" if mantissa.startswith("-") else ""
    mantissa = mantissa.lstrip("+-").replace(".", "{,}")
    return rf"${sign}{mantissa}\times10^{{{int(exponent)}}}$"


def main() -> None:
    """Produce every data file."""
    snapshot = FileMarketDataProvider(ROOT / "data" / "market").load_snapshot(SNAPSHOT_NAME)
    env = CurveBuilder().build(snapshot)
    curve = env.discount_curve
    pillars = env.calibration[0].pillars
    valuation = snapshot.valuation_date
    macros: dict[str, str] = {}

    # --- Figure 1: the bootstrapped curve (zero, 1M forward, discount factor) -----------------
    rows = ["t zero forward df"]
    for months in range(1, CURVE_GRID_MONTHS + 1):
        d = Tenor(months, TimeUnit.MONTHS).add_to(valuation)
        forward = curve.forward_rate(d, Tenor(1, TimeUnit.MONTHS).add_to(d), Act360())
        rows.append(
            f"{curve.time(d):.6f} {curve.zero_rate(d) * PERCENT:.6f} "
            f"{forward * PERCENT:.6f} {curve.discount_factor(d):.8f}"
        )
    write(HERE / "data" / "curve.dat", "\n".join(rows) + "\n")
    rows = ["t zero label"]
    for pillar in pillars:
        rows.append(
            f"{curve.time(pillar.pillar_date):.6f} "
            f"{curve.zero_rate(pillar.pillar_date) * PERCENT:.6f} {pillar.label}"
        )
    write(HERE / "data" / "pillars.dat", "\n".join(rows) + "\n")

    # --- Figure 2: gap with QuantLib along the curve ------------------------------------------
    gaps: dict[str, list[tuple[float, float]]] = {}
    sample_dates = [
        valuation + timedelta(days=k)
        for k in range(1, QUANTLIB_HORIZON_DAYS, QUANTLIB_SAMPLE_STEP_DAYS)
    ]
    with evaluation_date(valuation):
        for interpolation in ("log_linear_df", "linear_zero"):
            ours = CurveBuilder(CurveBuildOptions(interpolation=interpolation)).build(snapshot)
            reference = build_estr_curve(snapshot, interpolation)
            values = ours.discount_curve.discount_factors(sample_dates)
            gaps[interpolation] = [
                (
                    ours.discount_curve.time(d),
                    max(abs(float(v) - reference.discount(to_ql(d))), LOG_FLOOR),
                )
                for d, v in zip(sample_dates, values, strict=True)
            ]
    rows = ["t loglinear linearzero"]
    for (t, a), (_, b) in zip(gaps["log_linear_df"], gaps["linear_zero"], strict=True):
        rows.append(f"{t:.6f} {a:.3e} {b:.3e}")
    write(HERE / "data" / "quantlib_gap.dat", "\n".join(rows) + "\n")
    macros["MaxGapLogLinear"] = tex_sci(max(g for _, g in gaps["log_linear_df"]))
    macros["MaxGapLinearZero"] = tex_sci(max(g for _, g in gaps["linear_zero"]))

    # --- Table: calibration of every pillar ----------------------------------------------------
    lines = []
    for pillar in pillars:
        kind = "Dépôt" if pillar.instrument_type == "deposit" else "OIS"
        lines.append(
            f"{pillar.label} & {kind} & {pillar.pillar_date.isoformat()} & "
            f"{tex_num(pillar.market_quote * PERCENT, 3)} & {pillar.discount_factor:.8f}".replace(
                ".", "{,}"
            )
            + f" & {tex_num(curve.zero_rate(pillar.pillar_date) * PERCENT, 3)} & "
            f"{pillar.iterations} & {tex_sci(pillar.residual)} \\\\"
        )
    header = (
        "Pilier & Type & Date & Cotation (\\%) & DF & Z\u00e9ro (\\%) & It\u00e9r. "
        "& R\u00e9sidu \\\\"
    )
    write(HERE / "generated" / "table_pillars.tex", tabular("@{}llcrrrcr@{}", header, lines))
    macros["PillarCount"] = str(len(pillars))
    macros["MaxIterations"] = str(env.calibration[0].max_iterations)
    macros["MaxResidual"] = tex_sci(env.calibration[0].max_abs_residual)

    # --- Example 1: the 5Y payer swap, decomposition of the PV ---------------------------------
    engine = engine_for("ois_swap")
    five_year = INSTRUMENTS.get("ois_swap")(
        {
            "notional": COMPARISON_NOTIONAL,
            "fixed_rate": COMPARISON_RATE,
            "direction": "pay_fixed",
            "maturity": "5Y",
        },
        valuation,
    )
    priced = engine.price(five_year, env)
    fixed_leg, overnight_leg = priced.legs
    macros["FiveYPV"] = tex_num(priced.present_value, 2)
    macros["FiveYFixedLeg"] = tex_num(fixed_leg.present_value, 2)
    macros["FiveYOvernightLeg"] = tex_num(overnight_leg.present_value, 2)
    macros["FiveYParRate"] = tex_num(priced.par_rate * PERCENT, 4)
    macros["FiveYAnnuity"] = tex_num(priced.annuity, 2)
    macros["FiveYPVBP"] = tex_num(priced.pvbp, 2)
    macros["FiveYStart"] = five_year.start_date.isoformat()
    macros["FiveYEnd"] = five_year.end_date.isoformat()
    macros["FiveYCashflowCount"] = str(len(priced.cashflows))
    macros["FiveYPVCheck"] = tex_num((priced.par_rate - COMPARISON_RATE) * priced.annuity, 2)

    # --- Example 2: the 2Y payer swap, cashflow table ------------------------------------------
    two_year = INSTRUMENTS.get("ois_swap")(
        {
            "notional": EXAMPLE_NOTIONAL,
            "fixed_rate": EXAMPLE_FIXED_RATE,
            "direction": "pay_fixed",
            "maturity": EXAMPLE_TENOR,
        },
        valuation,
    )
    priced2 = engine.price(two_year, env)
    lines = []
    for row in priced2.cashflows:
        leg = "Fixe (pay)" if row.leg == "fixed" else "Overnight (receive)"
        lines.append(
            f"{leg} & {row.accrual_start.isoformat()} & {row.accrual_end.isoformat()} & "
            f"{row.payment_date.isoformat()} & {tex_num(row.rate * PERCENT, 4)} & "
            f"{tex_num(row.amount, 2)} & {row.discount_factor:.6f}".replace(".", "{,}", 1)
            + f" & {tex_num(row.present_value, 2)} \\\\"
        )
    header = (
        "Jambe & D\u00e9but & Fin & Paiement & Taux (\\%) & Montant (\\texteuro) & DF "
        "& PV (\\texteuro) \\\\"
    )
    write(HERE / "generated" / "table_cashflows.tex", tabular("@{}lccccrrr@{}", header, lines))
    macros["TwoYPV"] = tex_num(priced2.present_value, 2)
    macros["TwoYParRate"] = tex_num(priced2.par_rate * PERCENT, 4)
    macros["TwoYAnnuity"] = tex_num(priced2.annuity, 2)

    # --- Example 3: numerical check of the telescoping identity --------------------------------
    first = five_year.overnight_coupons[0]
    calendar = TargetCalendar()
    days = calendar.business_days(first.accrual_start, first.accrual_end)
    day_count = Act360()
    product = 1.0
    for start, end in itertools.pairwise(days):
        forward = curve.forward_rate(start, end, day_count)
        product *= 1.0 + forward * day_count.year_fraction(start, end)
    ratio = curve.discount_factor(first.accrual_start) / curve.discount_factor(first.accrual_end)
    macros["TelStart"] = first.accrual_start.isoformat()
    macros["TelEnd"] = first.accrual_end.isoformat()
    macros["TelDays"] = str(len(days) - 1)
    macros["TelProduct"] = f"{product:.14f}".replace(".", "{,}")
    macros["TelRatio"] = f"{ratio:.14f}".replace(".", "{,}")
    macros["TelDiff"] = tex_sci(abs(product - ratio))

    # --- Numeric macros ------------------------------------------------------------------------
    macros["ValuationDate"] = valuation.isoformat()
    macros["FixingCount"] = str(len(snapshot.fixings))
    macros["QuoteCount"] = str(len(snapshot.quotes))
    text = "% Generated by docs/latex/generate_data.py: do not edit by hand.\n" + "".join(
        f"\\newcommand{{\\{name}}}{{{value}}}\n" for name, value in macros.items()
    )
    write(HERE / "generated" / "numbers.tex", text)


if __name__ == "__main__":
    main()
