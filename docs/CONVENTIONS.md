# CONVENTIONS

## 1. Langue

| Élément | Langue |
|---|---|
| Documentation (`README.md`, `docs/*.md`, `CLAUDE.md`) | Français |
| Code, noms d'identifiants, docstrings, commentaires de code | **Anglais** |
| Messages de commit (Conventional Commits) | Anglais |
| Messages de log et messages d'erreur de l'API | Anglais |

## 2. Code

| Sujet | Règle |
|---|---|
| Python | 3.12 |
| Style | `ruff` (lint + format), longueur de ligne 100 |
| Typage | `mypy --strict` sur `core/` et `services/` ; mode standard sur `api/`, `sdk/`, `ui/` |
| Docstrings | Style Google, en anglais, obligatoires sur toute classe/fonction publique ; elles indiquent les **unités et conventions** (« rate in decimal », « end date exclusive ») |
| Commentaires | Le code doit être commenté pour la lisibilité (en anglais) : expliquer l'intention et les formules financières, pas seulement ce que fait la ligne |
| Nommage | `snake_case` fonctions/variables, `PascalCase` classes, `UPPER_CASE` constantes ; noms financiers explicites (`discount_factor`, pas `df` hors d'un très petit scope local) |
| Immutabilité | `@dataclass(frozen=True, slots=True)` par défaut dans `core/` |
| Nombres magiques | Aucun : tolérances, jours par an, tailles de bump sont des constantes nommées |
| Affichage | Jamais de `print` ; uniquement `logging` |
| Couches | `core` n'importe jamais `services/api/sdk/ui` (vérifié par `import-linter`) |

## 3. Git

- Conventional Commits (`feat:`, `fix:`, `test:`, `docs:`, `refactor:`, `chore:`), petits et atomiques.
- Une branche par phase (`phase-1-ois-vertical-slice`), une PR même en solo (la CI s'y exécute).
- `DECISIONS.md` mis à jour dans la même PR que le changement qu'il documente.

## 4. Pyramide de tests

| Niveau | Dossier | Contenu | Quand |
|---|---|---|---|
| Unitaires | `tests/unit/` | Calendriers, day counts, schedules, interpolateurs, solveurs, cashflows isolés | à chaque commit (pre-commit + CI) |
| Propriétés | `tests/unit/` (`hypothesis`) | DF(0)=1, DF décroissant, forward x tau cohérent avec les DF, PV au par rate = 0, symétrie de signe pay/receive | CI |
| Intégration | `tests/integration/` | Pipeline complet cotations -> courbe -> PV -> risque | CI |
| Validation QuantLib | `tests/validation/` | Comparaison systématique avec QuantLib, `@pytest.mark.validation` | CI |
| API | `tests/api/` | Endpoints via `TestClient` FastAPI : forme des réponses, erreurs, `request_id`, cache | CI |
| Non-régression | `tests/fixtures/` | Résultats de référence figés (golden files) | CI |
| Smoke test conteneur | job CI | Build des images, démarrage, `GET /health`, puis `POST /v1/price` | CI |

Cibles de couverture : **>= 90 % sur `core/`**, >= 80 % ailleurs.

## 5. Validation contre QuantLib (oracle, dépendance dev uniquement)

On compare **couche par couche**, pour pouvoir localiser l'origine d'un écart de PV.

| Couche | Comparaison | Tolérance de départ |
|---|---|---|
| Calendriers | Jours ouvrés TARGET, US Government Bond et US SOFR de 1995 à 2060, date par date | exacte |
| Day counts | `year_fraction` sur une grille de paires de dates | ~1e-14 |
| Schedules | Dates de début, fin, paiement, fixing (OIS et IRS vanille, jambes fixe et flottante) | exacte |
| Courbe bootstrapée | DF, zéros et forwards aux piliers et entre piliers, courbes €STR et SOFR, courbes de projection Euribor | 1e-10 à 1e-12 |
| Cashflows | Montants flottants projetés, coupons OIS composés | 1e-9 relatif |
| PV, par rate, annuité | Swaps de plusieurs maturités, sens, notionnels | 1e-8 relatif |
| DV01 par bucket | Par rapport aux différences finies QuantLib sur les mêmes cotations (courbes reconstruites par QuantLib pour chaque choc) | 1e-6, rapporté au DV01 parallèle du trade (ADR-031) |
| Cohérence interne du risque | Somme des DV01 par bucket et par courbe = DV01 parallèle | 1e-6 relatif au DV01 parallèle |
| P&L de scénario (Taylor) | P&L d'un choc parallèle de 1 à 5 bp contre `DV01 dx + convexité dx² / 2` (ADR-033) | 1e-4 relatif au P&L |

- Les tolérances sont des **constantes nommées dans un seul module de test**, jamais dispersées.
- Elles sont ajustées selon les écarts réels constatés, et **chaque ajustement est documenté** dans DECISIONS.md.
- Les cas de validation sont **générés à partir de grilles** (maturités x fréquences x devises), pas écrits à la main.
- Sources d'écart connues à documenter : `evaluationDate` global de QuantLib (nous passons la date explicitement ;
  aligner les deux dans les tests), conventions d'interpolation et de temps, fixings et spot lag, composition OIS /
  lookback / payment lag.

## 6. Pipeline CI (GitHub Actions), dans l'ordre

1. `uv sync --locked --all-extras` (échoue si `uv.lock` n'est pas à jour avec `pyproject.toml`)
2. `ruff check` + `ruff format --check`
3. `mypy`
4. `lint-imports`
5. `pytest` avec rapport de couverture
6. Build Docker + smoke test (job `Docker smoke test`, en parallèle des étapes 1 à 5) : `docker compose build`,
   `docker compose up -d --wait` (attend les `HEALTHCHECK` des deux images), `scripts/smoke_test.py`, logs des
   conteneurs, `docker compose down`
7. Publication de la couverture en artefact

Un job qui échoue bloque la fusion : la **protection de branche** de `main` est activée dans GitHub
(*Settings -> Rules*, check obligatoire « Lint, types, layers, tests »). Ce réglage vit dans GitHub, pas dans le
dépôt : à refaire si le dépôt est recréé ou transféré, et à mettre à jour si le job est renommé dans `ci.yml`.

`pre-commit` exécute localement la partie rapide (ruff, format, mypy, import-linter, tests unitaires). Les hooks
s'activent **une fois par clone** avec `uv run pre-commit install` (le dossier `.git/hooks` n'est pas versionné), et
`uv` doit être dans le `PATH` du terminal qui lance `git commit`.

## 7. Configuration

Variables d'environnement via `pydantic-settings` (API) :

| Variable | Défaut | Rôle |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Niveau des logs sur stdout |
| `LOG_FORMAT` | `json` | `json` en conteneur, `text` lisible en local |
| `MARKET_DATA_DIR` | `data/market` | Dossier des snapshots (`/app/data/market` dans l'image) |
| `CURVE_CACHE_SIZE` | `32` | Nombre maximal d'environnements de marché en cache |
| `CURVE_CACHE_TTL_SECONDS` | aucun | Durée de vie d'une entrée du cache |
| `LOG_BUFFER_SIZE` | `10000` | Événements gardés en mémoire pour `/v1/logs/{request_id}` |
| `LOG_BUFFER_LEVEL` | `INFO` | Niveau minimal gardé en mémoire (indépendant de `LOG_LEVEL`) |
| `GIT_COMMIT` | `unknown` | Commit déployé (`/v1/version`, `pricer_build_info`) ; argument de build de l'image |

Le dashboard lit `PRICER_API_URL` (`http://localhost:8000` par défaut, `http://api:8000` dans Compose).
