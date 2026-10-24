# Rate Derivatives Pricer

Un **pricer de dérivés de taux** extensible, de type production, en Python : bibliothèque quant en POO,
service FastAPI, SDK Python et dashboard Streamlit.

> Statut : **Phase 0 terminée** (outillage, CI, logging). **Phase 1 (swap OIS de bout en bout) à venir.**
> Voir `docs/ROADMAP.md`.

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

L'API et le dashboard arrivent en phase 1 :

```
uv run uvicorn rate_derivatives_pricer.api.main:app --reload
uv run streamlit run src/rate_derivatives_pricer/ui/app.py
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
