"""Risk by bump-and-reprice on calibration instruments (ADR-010, ADR-029), scenarios and carry.

``Bumper`` shocks the quotes of a ``MarketSnapshot``; ``RiskMeasure`` classes (registered in
``RISK_MEASURES``) turn bumped valuations into sensitivities; ``RiskEngine`` runs measures
through an abstract pricer. ``ScenarioPnL`` revalues a trade under named curve shocks (ADR-033)
and ``CarryRolldown`` ages it along an unchanged curve (ADR-034). Nothing here imports a pricing
engine.
"""

from rate_derivatives_pricer.core.risk.bumps import Bumper, BumpMode, BumpSpec, BumpTarget
from rate_derivatives_pricer.core.risk.carry import (
    DEFAULT_HORIZON,
    CarryRolldown,
    CarryRolldownResult,
    HorizonValuer,
    unchanged_curve_snapshot,
)
from rate_derivatives_pricer.core.risk.engine import RiskEngine, RiskReport
from rate_derivatives_pricer.core.risk.measures import (
    Convexity,
    CurveSpecificDV01,
    KeyRateDV01,
    ParallelDV01,
    Pricer,
    RiskBucket,
    RiskMeasure,
    RiskResult,
    RiskSettings,
)
from rate_derivatives_pricer.core.risk.scenarios import (
    QuoteShift,
    Scenario,
    ScenarioPnL,
    ScenarioPoint,
    ScenarioReport,
    ScenarioResult,
    resolve_scenarios,
    scenario_from_dict,
)

__all__ = [
    "DEFAULT_HORIZON",
    "BumpMode",
    "BumpSpec",
    "BumpTarget",
    "Bumper",
    "CarryRolldown",
    "CarryRolldownResult",
    "Convexity",
    "CurveSpecificDV01",
    "HorizonValuer",
    "KeyRateDV01",
    "ParallelDV01",
    "Pricer",
    "QuoteShift",
    "RiskBucket",
    "RiskEngine",
    "RiskMeasure",
    "RiskReport",
    "RiskResult",
    "RiskSettings",
    "Scenario",
    "ScenarioPnL",
    "ScenarioPoint",
    "ScenarioReport",
    "ScenarioResult",
    "resolve_scenarios",
    "scenario_from_dict",
    "unchanged_curve_snapshot",
]
