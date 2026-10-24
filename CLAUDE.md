# CLAUDE.md — Rate Derivatives Pricer

Briefing pour Claude Code. Ce fichier doit rester **COURT et STABLE**. Les détails sont dans `docs/`.

## Langue (règle importante)

- **Documentation** (`README.md`, `docs/*.md`, ce fichier) : en **français**.
- **Code, docstrings, commentaires de code, noms d'identifiants, messages de commit, messages de log** : en **anglais**.
- Les commandes et extraits de code dans les documents restent en anglais.

## Projet

Un **pricer de dérivés de taux** de type production, écrit en Python (POO, extensible), exposé comme un service
(FastAPI), avec un SDK Python et un dashboard Streamlit + Plotly.

Deux objectifs : (1) apprendre le pricing de taux, (2) apprendre les pratiques de développement de production
(API, SDK, Docker, CI, logging, observabilité) en vue d'un poste de
*Production Engineer – Quantitative and Applicative Support*.

Package : `rate_derivatives_pricer` (repo : `rate-derivatives-pricer`).

## État actuel

- **Phase 0 (fondations du dépôt) : TERMINÉE et fusionnée** (PR #1). Outillage, CI, hooks pre-commit, règle de
  couches (import-linter) et squelette de logging en place ; 21 tests verts.
- **Phase courante : 1 (tranche verticale, swap OIS)**. Branche à créer : `phase-1-ois-vertical-slice`.
- Logging : `rate_derivatives_pricer.api.logging_config` (`configure_logging`, `request_context`). Le cœur se
  contente de `logging.getLogger(__name__)`.
- Mettre à jour cette section à la fin de chaque phase. Ne jamais travailler en dehors de la phase courante.

## Périmètre de la v1

- Produits : swaps OIS et IRS vanilles uniquement. Devise : EUR d'abord (€STR, Euribor 3M/6M) ;
  l'USD (SOFR) s'ajoute plus tard **uniquement par configuration**.
- Python + NumPy pur. Solveurs de racines écrits en interne (Newton, Brent) derrière une interface `Solver`.
  Pas de SciPy dans `core/`.
- QuantLib est une dépendance **dev/test uniquement** (oracle de référence). Elle ne doit **jamais** être
  importée par `core/`.
- API sans état, cache de courbes LRU en mémoire, pas de base de données, pas d'authentification en v1.
- Risque par *bump-and-reprice* sur les **instruments de calibration** (chocs sur les par rates + re-bootstrap).

## Architecture en une image

```
ui -> sdk -> (HTTP) -> api -> services -> core
```

Les dépendances vont dans **un seul sens**. `core/` n'importe rien de `services/`, `api/`, `sdk/`, `ui/`.
Règle vérifiée par `import-linter` en CI. Voir @docs/ARCHITECTURE.md.

Structure : `src/rate_derivatives_pricer/{core,services,api,sdk,ui}/`.

## Documents détaillés (à lire avant de travailler sur le sujet concerné)

- @docs/ARCHITECTURE.md : couches, contrats, patterns
- @docs/ROADMAP.md : phases et critères de réussite vérifiables
- @docs/CONVENTIONS.md : style de code, tests, tolérances, git
- @docs/API.md : contrat HTTP, erreurs, spécification des logs
- @docs/DECISIONS.md : journal des décisions (style ADR)

## Commandes

```
uv sync --all-extras                      # install from uv.lock (core + api/sdk/ui extras + dev tools)
uv run pre-commit install                 # once per clone: enable the git hooks
uv run ruff check . && uv run ruff format --check .
uv run mypy                               # strict on core/ and services/
uv run lint-imports                       # layer dependency rule
uv run pytest                             # all tests
uv run pytest -m validation               # QuantLib validation only
uv run uvicorn rate_derivatives_pricer.api.main:app --reload   # available from phase 1
uv run streamlit run src/rate_derivatives_pricer/ui/app.py   # available from phase 1
docker compose up
```

## Règles de travail (obligatoires)

1. **Une phase à la fois.** N'implémenter que la phase courante de `docs/ROADMAP.md`.
2. **Tests d'abord** pour toute logique numérique, y compris la comparaison avec QuantLib.
3. **Ne jamais casser l'API publique.** Un changement incompatible passe par `/v2/` ou par une décision explicite
   dans DECISIONS.md.
4. **Respecter la règle de dépendance** entre couches (voir ci-dessus).
5. **Boucle de vérification avant de déclarer une tâche terminée :**
   `uv run ruff check . && uv run mypy && uv run lint-imports && uv run pytest`
6. **Consigner chaque décision non triviale** (convention, tolérance, hypothèse financière) dans
   `docs/DECISIONS.md`, dans la même PR.
7. **Ne jamais deviner une convention de marché** (lookback OIS, spot lag, payment delay...). En cas de doute,
   S'ARRÊTER et poser la question.
8. **Pas d'optimisation prématurée.** NumPy simple d'abord, mesurer avant d'optimiser.
9. **Commenter le code** (l'intention et les formules financières, pas seulement ce que fait la ligne),
   **en anglais**. Docstrings style Google, avec unités et conventions.
10. **Pas de `print`.** Uniquement le module standard `logging` (JSON structuré, corrélation par `request_id`).
11. **Immutabilité et pas d'état global.** Dataclasses figées dans `core/` ; la date de valorisation est toujours
    un paramètre explicite (pas de date d'évaluation globale comme dans QuantLib).
12. **Commits :** Conventional Commits en anglais, petits et atomiques. Une phase = une branche = une PR.

## Unités et formats (source classique de bugs en production)

- Dates : ISO 8601 (`2026-10-19`). Taux : **décimal** (0.025 = 2,5 %).
- Les cotations de marché portent une unité explicite : `"percent" | "decimal" | "bp"`.
- PV dans la devise du trade.
