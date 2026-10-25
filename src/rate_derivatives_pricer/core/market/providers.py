"""Market data providers and the canonical (JSON-compatible) snapshot format.

Snapshot file layout (``data/market/<name>.json``)::

    {
      "name": "EUR_2026-10-19", "currency": "EUR", "valuation_date": "2026-10-19",
      "description": "...",
      "quotes": [{"type": "ois_swap", "tenor": "5Y", "value": 2.25, "unit": "percent"}, ...],
      "fixings": {"ESTR": [{"date": "2026-10-16", "value": 1.92, "unit": "percent"}, ...]}
    }

The same dictionary format is the canonical serialization hashed for curve cache keys.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

from rate_derivatives_pricer.core.errors import MarketDataError, MarketDataNotFoundError
from rate_derivatives_pricer.core.market.quotes import QUOTE_TYPES, QuoteUnit
from rate_derivatives_pricer.core.market.snapshot import Fixing, MarketSnapshot

# Snapshot names are file stems: restricting characters prevents path traversal.
SNAPSHOT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")
SNAPSHOT_FILE_SUFFIX = ".json"


def _field(mapping: Mapping[str, Any], key: str, context: str) -> Any:
    try:
        return mapping[key]
    except (KeyError, TypeError):
        raise MarketDataError(
            f"missing market data field {context}.{key}", {"field": f"{context}.{key}"}
        ) from None


def _unit(value: Any, context: str) -> QuoteUnit:
    try:
        return QuoteUnit(str(value).lower())
    except ValueError:
        raise MarketDataError(
            f"invalid unit in {context}: {value!r}",
            {"field": f"{context}.unit", "allowed": [u.value for u in QuoteUnit]},
        ) from None


def _date(value: Any, context: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise MarketDataError(f"invalid date in {context}: {value!r}", {"field": context}) from None


def _number(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise MarketDataError(f"invalid number in {context}: {value!r}", {"field": context})
    return float(value)


def snapshot_from_dict(data: Mapping[str, Any]) -> MarketSnapshot:
    """Parse the canonical snapshot dictionary.

    Raises:
        MarketDataError: If a field is missing or invalid.
    """
    name = str(data.get("name", "inline"))
    quotes = []
    for position, raw in enumerate(_field(data, "quotes", "snapshot")):
        context = f"quotes[{position}]"
        quote_type = str(_field(raw, "type", context))
        try:
            quote_class = QUOTE_TYPES[quote_type]
        except KeyError:
            raise MarketDataError(
                f"unknown quote type {quote_type!r}",
                {"field": f"{context}.type", "allowed": sorted(QUOTE_TYPES)},
            ) from None
        quotes.append(
            quote_class(
                tenor_label=str(_field(raw, "tenor", context)),
                value=_number(_field(raw, "value", context), f"{context}.value"),
                unit=_unit(raw.get("unit", QuoteUnit.DECIMAL.value), context),
            )
        )
    fixings = []
    for index_name, series in dict(data.get("fixings", {})).items():
        for position, raw in enumerate(series):
            context = f"fixings.{index_name}[{position}]"
            unit = _unit(raw.get("unit", QuoteUnit.DECIMAL.value), context)
            value = _number(_field(raw, "value", context), f"{context}.value")
            fixings.append(
                Fixing(
                    index=str(index_name).upper(),
                    fixing_date=_date(_field(raw, "date", context), f"{context}.date"),
                    rate=value * unit.to_decimal,
                )
            )
    return MarketSnapshot(
        name=name,
        currency=str(_field(data, "currency", "snapshot")).upper(),
        valuation_date=_date(_field(data, "valuation_date", "snapshot"), "valuation_date"),
        quotes=tuple(quotes),
        fixings=tuple(sorted(fixings, key=lambda f: (f.index, f.fixing_date))),
        description=str(data.get("description", "")),
    )


def snapshot_to_dict(snapshot: MarketSnapshot) -> dict[str, Any]:
    """Canonical dictionary of ``snapshot`` (quotes in their original units, fixings sorted)."""
    fixings: dict[str, list[dict[str, Any]]] = {}
    for fixing in sorted(snapshot.fixings, key=lambda f: (f.index, f.fixing_date)):
        fixings.setdefault(fixing.index, []).append(
            {"date": fixing.fixing_date.isoformat(), "value": fixing.rate, "unit": "decimal"}
        )
    return {
        "name": snapshot.name,
        "currency": snapshot.currency,
        "valuation_date": snapshot.valuation_date.isoformat(),
        "description": snapshot.description,
        "quotes": [
            {
                "type": quote.instrument_type,
                "tenor": quote.tenor_label,
                "value": quote.value,
                "unit": quote.unit.value,
            }
            for quote in snapshot.quotes
        ],
        "fixings": fixings,
    }


class MarketDataProvider(ABC):
    """Source of market snapshots."""

    @abstractmethod
    def list_snapshots(self) -> tuple[str, ...]:
        """Names of the available snapshots, sorted."""

    @abstractmethod
    def load_snapshot(self, name: str) -> MarketSnapshot:
        """Load the snapshot called ``name``.

        Raises:
            MarketDataNotFoundError: If no such snapshot exists.
            MarketDataError: If the snapshot is malformed.
        """


class FileMarketDataProvider(MarketDataProvider):
    """Reads ``<name>.json`` snapshot files from a directory (v1 provider, ADR-005).

    Args:
        directory: Folder holding the snapshot files (``data/market``).
    """

    def __init__(self, directory: Path) -> None:
        self._directory = Path(directory)

    @property
    def directory(self) -> Path:
        """Snapshot folder."""
        return self._directory

    def list_snapshots(self) -> tuple[str, ...]:
        """Stems of the ``*.json`` files of the folder."""
        if not self._directory.is_dir():
            return ()
        return tuple(sorted(path.stem for path in self._directory.glob(f"*{SNAPSHOT_FILE_SUFFIX}")))

    def load_snapshot(self, name: str) -> MarketSnapshot:
        """Parse ``<directory>/<name>.json``."""
        if not SNAPSHOT_NAME_PATTERN.match(name):
            raise MarketDataError("invalid snapshot name", {"snapshot": name})
        path = self._directory / f"{name}{SNAPSHOT_FILE_SUFFIX}"
        if not path.is_file():
            raise MarketDataNotFoundError(f"snapshot {name} not found", {"snapshot": name})
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise MarketDataError(
                "snapshot file is not valid JSON", {"snapshot": name, "error": str(error)}
            ) from None
        snapshot = snapshot_from_dict(data)
        if snapshot.name != name:
            raise MarketDataError(
                "snapshot name does not match its file name",
                {"snapshot": name, "declared_name": snapshot.name},
            )
        return snapshot
