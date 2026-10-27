"""Per-currency market conventions, loaded from configuration files (never hard-coded).

Each ``core/conventions/data/<CCY>.json`` file describes one currency: calendar, spot lag, curve
time measure, overnight index and the conventions of each calibration instrument. Adding a
currency (USD in phase 3) means adding a file, not changing calculation code (ADR-002).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from typing import Any

from rate_derivatives_pricer.core.conventions.business_day import BusinessDayConvention
from rate_derivatives_pricer.core.conventions.calendar import Calendar, calendar_from_name
from rate_derivatives_pricer.core.conventions.day_count import (
    DayCountConvention,
    day_count_from_name,
)
from rate_derivatives_pricer.core.conventions.tenor import Frequency, Tenor
from rate_derivatives_pricer.core.errors import ConventionError
from rate_derivatives_pricer.core.market.indices import IborIndex, OvernightIndex
from rate_derivatives_pricer.core.registry import CONVENTIONS
from rate_derivatives_pricer.core.schedule.generator import DateGenerationRule

# Package-data directory holding one JSON file per currency.
CONVENTIONS_DATA_PACKAGE = "rate_derivatives_pricer.core.conventions.data"


@dataclass(frozen=True, slots=True)
class DepositConventions:
    """Conventions of a cash deposit used as a curve calibration instrument.

    Attributes:
        settlement_days: Business days from the valuation date to the deposit start (0 for ON).
        business_day_convention: Adjustment of the maturity date.
        end_of_month: End-of-month rule for month tenors.
        day_count: Accrual convention of the deposit rate.
    """

    settlement_days: int
    business_day_convention: BusinessDayConvention
    end_of_month: bool
    day_count: DayCountConvention


@dataclass(frozen=True, slots=True)
class OisConventions:
    """Conventions of an overnight indexed swap (fixed leg vs compounded overnight leg).

    Attributes:
        fixed_frequency: Payment frequency of the fixed leg.
        fixed_day_count: Accrual convention of the fixed leg.
        floating_frequency: Payment frequency of the overnight leg.
        floating_day_count: Accrual convention of the overnight leg.
        business_day_convention: Adjustment of accrual dates (also of the maturity date).
        end_of_month: End-of-month rule, applied when the start date is the last business day of
            its month.
        rule: Schedule generation direction.
        payment_lag_days: Business days between accrual end and payment.
    """

    fixed_frequency: Frequency
    fixed_day_count: DayCountConvention
    floating_frequency: Frequency
    floating_day_count: DayCountConvention
    business_day_convention: BusinessDayConvention
    end_of_month: bool
    rule: DateGenerationRule
    payment_lag_days: int


@dataclass(frozen=True, slots=True)
class IborSwapConventions:
    """Conventions of a vanilla interest rate swap (fixed leg vs an Ibor index).

    The floating leg takes its frequency, day count and calendar from the Ibor index; only the
    fixed leg and the schedule rules are described here.

    Attributes:
        fixed_frequency: Payment frequency of the fixed leg.
        fixed_day_count: Accrual convention of the fixed leg.
        business_day_convention: Adjustment of accrual dates (also of the maturity date).
        end_of_month: End-of-month rule, applied when the start date is the last business day of
            its month.
        rule: Schedule generation direction.
        payment_lag_days: Business days between accrual end and payment.
    """

    fixed_frequency: Frequency
    fixed_day_count: DayCountConvention
    business_day_convention: BusinessDayConvention
    end_of_month: bool
    rule: DateGenerationRule
    payment_lag_days: int


@dataclass(frozen=True, slots=True)
class CurrencyConventions:
    """All market conventions of one currency.

    Attributes:
        currency: ISO 4217 code (``"EUR"``).
        calendar: Settlement calendar of the currency's swaps.
        spot_lag_days: Business days from trade (valuation) date to spot date.
        curve_day_count: Time measure of the curves' interpolation (ADR-008).
        overnight_index: Default overnight index (discounting and OIS projection).
        deposit: Conventions of the overnight deposit calibration instrument.
        ois: Conventions of the OIS calibration instruments and traded OIS.
        ibor_indices: Ibor indices of the currency (empty for a currency without term rates).
        default_ibor_index: Name of the index used by a vanilla swap that names none.
        ibor_deposit: Conventions of the Ibor deposit calibration instrument.
        ibor_swap: Conventions of vanilla swaps against the Ibor indices.
    """

    currency: str
    calendar: Calendar
    spot_lag_days: int
    curve_day_count: DayCountConvention
    overnight_index: OvernightIndex
    deposit: DepositConventions
    ois: OisConventions
    ibor_indices: tuple[IborIndex, ...] = ()
    default_ibor_index: str | None = None
    ibor_deposit: DepositConventions | None = None
    ibor_swap: IborSwapConventions | None = None

    def ibor_index(self, name: str | None = None) -> IborIndex:
        """Ibor index called ``name`` (the default index when ``None``).

        Raises:
            ConventionError: If the currency has no such index.
        """
        wanted = (name or self.default_ibor_index or "").strip().upper()
        for index in self.ibor_indices:
            if index.name == wanted:
                return index
        raise ConventionError(
            f"unknown Ibor index for {self.currency}: {wanted!r}",
            {"index": wanted, "available": [index.name for index in self.ibor_indices]},
        )

    def require_ibor_swap(self) -> IborSwapConventions:
        """Vanilla swap conventions.

        Raises:
            ConventionError: If the currency defines none.
        """
        if self.ibor_swap is None:
            raise ConventionError(
                f"no vanilla swap conventions for {self.currency}", {"currency": self.currency}
            )
        return self.ibor_swap

    def require_ibor_deposit(self) -> DepositConventions:
        """Ibor deposit conventions.

        Raises:
            ConventionError: If the currency defines none.
        """
        if self.ibor_deposit is None:
            raise ConventionError(
                f"no Ibor deposit conventions for {self.currency}", {"currency": self.currency}
            )
        return self.ibor_deposit


def _require(config: Mapping[str, Any], key: str, context: str) -> Any:
    try:
        return config[key]
    except KeyError:
        raise ConventionError(
            f"missing convention field {context}.{key}", {"field": f"{context}.{key}"}
        ) from None


def _enum_value[E: (BusinessDayConvention, DateGenerationRule)](
    enum_type: type[E], value: str, field: str
) -> E:
    try:
        return enum_type(value.strip().upper())
    except ValueError:
        raise ConventionError(f"invalid {field}: {value!r}", {"field": field}) from None


def conventions_from_dict(config: Mapping[str, Any]) -> CurrencyConventions:
    """Build :class:`CurrencyConventions` from a parsed configuration mapping.

    Raises:
        ConventionError: If a field is missing or holds an unknown convention name.
    """
    currency = str(_require(config, "currency", "root")).upper()
    calendar = calendar_from_name(_require(config, "calendar", currency))

    index_config = _require(config, "overnight_index", currency)
    overnight_index = OvernightIndex(
        name=str(_require(index_config, "name", "overnight_index")).upper(),
        currency=currency,
        fixing_calendar=calendar_from_name(_require(index_config, "calendar", "overnight_index")),
        day_count=day_count_from_name(_require(index_config, "day_count", "overnight_index")),
    )

    deposit = _deposit_conventions(_require(config, "deposit", currency), "deposit")

    ois_config = _require(config, "ois", currency)
    ois = OisConventions(
        fixed_frequency=Frequency.parse(_require(ois_config, "fixed_frequency", "ois")),
        fixed_day_count=day_count_from_name(_require(ois_config, "fixed_day_count", "ois")),
        floating_frequency=Frequency.parse(_require(ois_config, "floating_frequency", "ois")),
        floating_day_count=day_count_from_name(_require(ois_config, "floating_day_count", "ois")),
        business_day_convention=_enum_value(
            BusinessDayConvention,
            _require(ois_config, "business_day_convention", "ois"),
            "ois.business_day_convention",
        ),
        end_of_month=bool(_require(ois_config, "end_of_month", "ois")),
        rule=_enum_value(DateGenerationRule, _require(ois_config, "rule", "ois"), "ois.rule"),
        payment_lag_days=int(_require(ois_config, "payment_lag_days", "ois")),
    )

    ibor_config = config.get("ibor")
    ibor_fields: dict[str, Any] = _ibor_conventions_from_dict(ibor_config, currency)

    return CurrencyConventions(
        currency=currency,
        calendar=calendar,
        spot_lag_days=int(_require(config, "spot_lag_days", currency)),
        curve_day_count=day_count_from_name(_require(config, "curve_day_count", currency)),
        overnight_index=overnight_index,
        deposit=deposit,
        ois=ois,
        **ibor_fields,
    )


def _deposit_conventions(config: Mapping[str, Any], context: str) -> DepositConventions:
    return DepositConventions(
        settlement_days=int(_require(config, "settlement_days", context)),
        business_day_convention=_enum_value(
            BusinessDayConvention,
            _require(config, "business_day_convention", context),
            f"{context}.business_day_convention",
        ),
        end_of_month=bool(_require(config, "end_of_month", context)),
        day_count=day_count_from_name(_require(config, "day_count", context)),
    )


def _ibor_conventions_from_dict(config: Mapping[str, Any] | None, currency: str) -> dict[str, Any]:
    """Optional ``ibor`` section: indices, deposit and vanilla swap conventions."""
    if config is None:
        return {}
    indices = []
    for position, raw in enumerate(_require(config, "indices", "ibor")):
        context = f"ibor.indices[{position}]"
        indices.append(
            IborIndex(
                name=str(_require(raw, "name", context)).upper(),
                currency=currency,
                tenor=Tenor.parse(str(_require(raw, "tenor", context))),
                fixing_days=int(_require(raw, "fixing_days", context)),
                fixing_calendar=calendar_from_name(_require(raw, "calendar", context)),
                day_count=day_count_from_name(_require(raw, "day_count", context)),
                business_day_convention=_enum_value(
                    BusinessDayConvention,
                    _require(raw, "business_day_convention", context),
                    f"{context}.business_day_convention",
                ),
                end_of_month=bool(_require(raw, "end_of_month", context)),
            )
        )
    swap_config = _require(config, "swap", "ibor")
    swap = IborSwapConventions(
        fixed_frequency=Frequency.parse(_require(swap_config, "fixed_frequency", "ibor.swap")),
        fixed_day_count=day_count_from_name(_require(swap_config, "fixed_day_count", "ibor.swap")),
        business_day_convention=_enum_value(
            BusinessDayConvention,
            _require(swap_config, "business_day_convention", "ibor.swap"),
            "ibor.swap.business_day_convention",
        ),
        end_of_month=bool(_require(swap_config, "end_of_month", "ibor.swap")),
        rule=_enum_value(
            DateGenerationRule, _require(swap_config, "rule", "ibor.swap"), "ibor.swap.rule"
        ),
        payment_lag_days=int(_require(swap_config, "payment_lag_days", "ibor.swap")),
    )
    return {
        "ibor_indices": tuple(indices),
        "default_ibor_index": str(_require(config, "default_index", "ibor")).upper(),
        "ibor_deposit": _deposit_conventions(_require(config, "deposit", "ibor"), "ibor.deposit"),
        "ibor_swap": swap,
    }


def register_conventions_from_package_data() -> None:
    """Load every ``data/*.json`` convention file into the ``CONVENTIONS`` registry."""
    for entry in sorted(resources.files(CONVENTIONS_DATA_PACKAGE).iterdir(), key=lambda e: e.name):
        if entry.name.endswith(".json"):
            conventions = conventions_from_dict(json.loads(entry.read_text(encoding="utf-8")))
            CONVENTIONS.add(conventions.currency, conventions)


def get_conventions(currency: str) -> CurrencyConventions:
    """Conventions of ``currency`` (``"EUR"``).

    Raises:
        ConventionError: If no configuration exists for the currency.
    """
    return CONVENTIONS.get(currency.strip().upper())
