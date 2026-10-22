# CONVENTIONS

À placer dans `docs/CONVENTIONS.md`.

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
| Calendriers | Jours ouvrés TARGET / US sur 30 ans, date par date | exacte |
| Day counts | `year_fraction` sur une grille de paires de dates | ~1e-14 |
| Schedules | Dates de début, fin, paiement, fixing | exacte |
| Courbe bootstrapée | DF, zéros et forwards aux piliers et entre piliers | 1e-10 à 1e-12 |
| Cashflows | Montants flottants projetés, coupons OIS composés | 1e-9 relatif |
| PV, par rate, annuité | Swaps de plusieurs maturités, sens, notionnels | 1e-8 relatif |
| DV01 par bucket | Par rapport aux différences finies QuantLib sur les mêmes cotations | 1e-6 relatif |

- Les tolérances sont des **constantes nommées dans un seul module de test**, jamais dispersées.
- Elles sont ajustées selon les écarts réels constatés, et **chaque ajustement est documenté** dans DECISIONS.md.
- Les cas de validation sont **générés à partir de grilles** (maturités x fréquences x devises), pas écrits à la main.
- Sources d'écart connues à documenter : `evaluationDate` global de QuantLib (nous passons la date explicitement ;
  aligner les deux dans les tests), conventions d'interpolation et de temps, fixings et spot lag, composition OIS /
  lookback / payment lag.

## 6. Pipeline CI (GitHub Actions), dans l'ordre

1. `uv sync`
2. `ruff check` + `ruff format --check`
3. `mypy`
4. `lint-imports`
5. `pytest` avec rapport de couverture
6. Build Docker + smoke test (à partir de la phase 4)
7. Publication de la couverture en artefact

Un job qui échoue bloque la fusion. `pre-commit` exécute localement la partie rapide (ruff, mypy, tests unitaires rapides).

## 7. Configuration

Variables d'environnement via `pydantic-settings` : `LOG_LEVEL`, `LOG_FORMAT` (`json` en conteneur, texte lisible
en local), taille du cache, dossier de données.
