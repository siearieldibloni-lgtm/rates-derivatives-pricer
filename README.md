# Rate Derivatives Pricer

Un **pricer de dérivés de taux** extensible, de type production, en Python : bibliothèque quant en POO,
service FastAPI, SDK Python et dashboard Streamlit.

> Statut : **Phase 0 terminée** (outillage, CI, logging). **Phase 1 implémentée** (swap OIS €STR de bout en bout :
> cotations -> courbe -> PV -> API -> SDK -> dashboard, validé contre QuantLib), en revue. Voir `docs/ROADMAP.md`.
>
> Les données de marché fournies (`data/market/EUR_2026-10-19.json`) sont **synthétiques** (ADR-023).

## Objectifs

1. Apprendre le pricing de taux : bootstrap de courbes (OIS, multi-courbes), valorisation de swaps, risque (DV01,
   key-rate, convexité, scénarios, carry/roll-down).
2. Apprendre les pratiques de développement de production : architecture en couches, conception d'API, SDK,
   logging structuré, observabilité, Docker, CI.

## Périmètre de la v1

- Produits : swaps OIS, IRS vanilles. Devise : EUR (USD par configuration en phase 3).
- Python + NumPy pur. QuantLib utilisée uniquement comme oracle de validation dans les tests.
- API sans état avec cache de courbes en mémoire.

## Architecture

```
ui -> sdk -> (HTTP) -> api -> services -> core
```

`core/` (bibliothèque quant) ne connaît rien des couches web. Détails : `docs/ARCHITECTURE.md`.

## Démarrage rapide

```
uv sync --all-extras
uv run pre-commit install    # une fois par clone : active les hooks git
uv run pytest
```

Lancer l'API (documentation interactive sur http://localhost:8000/docs), puis le dashboard dans un second terminal :

```
uv run uvicorn rate_derivatives_pricer.api.main:app --reload
uv run streamlit run src/rate_derivatives_pricer/ui/app.py
```

En local, `LOG_FORMAT=text` rend les logs lisibles ; `PRICER_API_URL` indique au dashboard où trouver l'API.

Pricer un swap OIS par HTTP :

```
curl -s -X POST http://localhost:8000/v1/price -H "Content-Type: application/json" \
  -d '{"market": {"snapshot": "EUR_2026-10-19"},
       "trade": {"type": "ois_swap", "params": {"notional": 10000000, "fixed_rate": 0.0225,
                                                 "direction": "pay_fixed", "maturity": "5Y"}}}'
```

Ou avec le SDK Python :

```python
from rate_derivatives_pricer.sdk import PricerClient, ois_swap

with PricerClient("http://localhost:8000") as client:
    priced = client.price("EUR_2026-10-19", ois_swap(10_000_000, 0.0225, "pay_fixed", "5Y"))
    print(priced.result.present_value, priced.result.par_rate, priced.request_id)
```

Le cœur s'utilise aussi seul, par exemple dans un notebook :

```python
from pathlib import Path
from rate_derivatives_pricer.core.engines.base import engine_for
from rate_derivatives_pricer.core.market.bootstrap import CurveBuilder
from rate_derivatives_pricer.core.market.providers import FileMarketDataProvider
from rate_derivatives_pricer.core.registry import INSTRUMENTS

snapshot = FileMarketDataProvider(Path("data/market")).load_snapshot("EUR_2026-10-19")
env = CurveBuilder().build(snapshot)
swap = INSTRUMENTS.get("ois_swap")(
    {"notional": 1e7, "fixed_rate": 0.0225, "direction": "pay_fixed", "maturity": "5Y"},
    snapshot.valuation_date,
)
result = engine_for("ois_swap").price(swap, env)
```

Avec Docker (phase 4) : `docker compose up`.

## Documentation

| Fichier | Contenu |
|---|---|
| `CLAUDE.md` | Briefing et règles de travail pour Claude Code |
| `docs/ARCHITECTURE.md` | Couches, contrats, patterns |
| `docs/ROADMAP.md` | Phases et critères de réussite |
| `docs/CONVENTIONS.md` | Code, tests, tolérances, CI |
| `docs/API.md` | Contrat HTTP, erreurs, logs |
| `docs/DECISIONS.md` | Journal des décisions |

## Langue

Documentation en français ; code, docstrings, commentaires de code et commits en anglais.

## Workflow de développement

Une phase = une branche = une PR. Avant de pousser :

```
uv run ruff check . && uv run mypy && uv run lint-imports && uv run pytest
```
