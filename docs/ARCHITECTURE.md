# ARCHITECTURE

## 1. Couches et règle de dépendance

```
ui  ->  sdk  ->  (HTTP)  ->  api  ->  services  ->  core
```

| Couche | Rôle | Dépendances autorisées |
|---|---|---|
| `core/` | Bibliothèque quant : conventions, données de marché, courbes, instruments, moteurs, risque | NumPy + bibliothèque standard uniquement |
| `services/` | Orchestration, mappers DTO <-> domaine, cache de courbes | `core` |
| `api/` | Routes FastAPI, schémas Pydantic, traduction des erreurs en HTTP, configuration du logging | `services` (et types du `core` via services) |
| `sdk/` | Client Python typé (`httpx`) | Schémas de l'API uniquement |
| `ui/` | Dashboard Streamlit + Plotly | `sdk` uniquement |

`core/` est testable seul, utilisable dans un notebook, et ne connaît ni FastAPI ni Streamlit.
La règle est vérifiée par `import-linter` en CI.

## 2. Principes transversaux

- **Immutabilité** : `@dataclass(frozen=True, slots=True)` pour instruments, cotations, échéanciers, snapshots.
  Un bump renvoie de nouveaux objets, sans effet de bord.
- **Pas d'état global** : la date de valorisation vit dans `MarketSnapshot` (différence assumée avec le
  `Settings.evaluationDate` global de QuantLib).
- **Dépendre d'interfaces** (`ABC` / `Protocol`), jamais d'implémentations concrètes.
- **Données != comportement** : les instruments décrivent un contrat, les moteurs calculent, les schémas d'API
  transportent.
- **Erreurs typées** : une exception par mode d'échec (section 10).
- **Le cœur émet les logs, l'API les configure** : `core` n'utilise que le module standard `logging`.

## 3. Pipeline

```
MarketDataProvider -> MarketSnapshot (date de valorisation + cotations + fixings)
        -> CurveBuilder (bootstrap, Solver, Interpolator)
        -> MarketEnvironment (courbe d'actualisation + courbes de projection + fixings)
Trade/Instrument + MarketEnvironment -> PricingEngine -> PricingResult
RiskEngine : Bumper -> nouveau snapshot -> re-bootstrap -> re-pricing -> RiskResult
```

Le flux est fonctionnel : chaque étape prend des entrées et renvoie une sortie. Le risque revient à
« modifier le snapshot, puis rejouer le pipeline ».

## 4. Conventions et échéanciers (`core/conventions`, `core/schedule`)

| Contrat | Responsabilité | Implémentations v1 |
|---|---|---|
| `Calendar` | Dire si une date est ouvrée ; avancer de n jours ouvrés | `TargetCalendar`, `UsGovernmentCalendar`, `JointCalendar` |
| `DayCountConvention` | `year_fraction(d1, d2)` | `Act360`, `Act365Fixed`, `Thirty360` |
| `BusinessDayConvention` (enum) | Ajuster une date non ouvrée | Following, ModifiedFollowing, Preceding |
| `Tenor` / `Frequency` | « 3M », « 1Y », « 6M » avec arithmétique de dates | objet valeur |
| `CurrencyConventions` | Toutes les conventions d'une devise : calendrier, day counts, spot lag, fréquences, indices par défaut | `EUR`, `USD` (chargés depuis la configuration, pas codés en dur) |
| `ScheduleGenerator` -> `Schedule` | Périodes (début, fin, paiement, fixing) selon règle backward/forward, stubs, fin de mois | une classe paramétrée |

## 5. Données de marché (`core/market`)

| Contrat | Responsabilité |
|---|---|
| `Quote` | Une cotation : type d'instrument, tenor, valeur, unité. `DepositQuote`, `OisSwapQuote` (courbe €STR) ; `IborDepositQuote`, `IborSwapQuote` (courbe de projection d'un indice, qu'elles nomment : `IborQuote.index_name`) |
| `MarketSnapshot` | Date de valorisation + cotations + fixings historiques. **C'est l'unité de bump du risque** |
| `MarketDataProvider` | `load_snapshot(name_or_date)`. v1 : `FileMarketDataProvider` (CSV/JSON dans `data/market/`) |
| `Index` | Définition d'un indice : `OvernightIndex` (€STR, SOFR), `IborIndex` (Euribor 3M/6M : tenor, jours de fixing, calendrier, day count) |
| `Interpolator` (Strategy) | `LogLinearOnDiscountFactor` (défaut), `LinearOnZeroRate` |
| `Solver` (Strategy) | `NewtonSolver`, `BrentSolver` (implémentations internes) |
| `Curve` | `discount_factor(date)`, `zero_rate(date)`, `forward_rate(d1, d2)` |
| `DiscountCurve` | Courbe définie par des nœuds (date, DF) et un `Interpolator` ; temps mesuré avec un day count interne (Act/365F par défaut) |
| `CalibrationInstrument` | Pour une cotation et une courbe candidate, calcule la **cotation implicite** (équivalent du `RateHelper` de QuantLib) ; rend le bootstrap générique. `DepositCalibration`, `OisCalibration`, `IborDepositCalibration`, `IborSwapCalibration` |
| `CurveBuilder` | `build(snapshot, horizon=None) -> MarketEnvironment`. Bootstrap séquentiel en deux étapes : courbe €STR d'abord, puis une courbe de projection par indice Ibor actualisée sur l'€STR (ADR-028). Mémorise les piliers résolus ; `horizon` tronque les courbes pour les revalorisations de risque |
| `MarketEnvironment` | `discount_curve` (OIS), `projection_curve(index)` (une par indice, y compris l'indice overnight), `fixing(index, date)`. Interface consommée par instruments et moteurs |

## 6. Instruments (`core/instruments`)

```
Instrument (abstrait)
 └── Swap : legs = [Leg, Leg]
      ├── VanillaIrs   (jambe fixe + jambe flottante Ibor)
      └── OisSwap      (jambe fixe + jambe overnight composée)

Leg = liste de Cashflow + notionnel + sens (pay / receive)

Cashflow (abstrait) : payment_date, amount(env: MarketEnvironment)
 ├── FixedCoupon
 ├── IborCoupon                 (fixé 2 jours avant le début ; taux projeté sur la courbe de l'indice)
 └── OvernightCompoundedCoupon  (composition quotidienne, lookback, lag, payment delay)
```

- **Cashflows polymorphes** : un `Cashflow` calcule lui-même son montant à partir de l'interface `MarketEnvironment`.
- `Trade` (mince) = Instrument + métadonnées (identifiant, sens, date de trade, book). Séparé de `Instrument`.
- Les jambes sont construites par une factory à partir d'un `Schedule` et d'une description.

## 7. Moteurs de pricing (`core/engines`)

- `PricingEngine.price(instrument, env) -> PricingResult` ; déclare les types d'instruments supportés.
- `DiscountingSwapEngine` : somme des flux actualisés par jambe ; calcule aussi par rate et annuité (PVBP). Une seule classe pour `ois_swap` et `vanilla_irs` : les flux flottants se projettent sur la courbe de leur indice, tout s'actualise sur la courbe €STR. `present_value()` est un chemin léger (sans tableau de flux ni log) utilisé par le risque.
- **Moteurs sans état, choisis par registry** : les instruments ne portent PAS leur moteur (contrairement à QuantLib).
- `PricingResult` : PV total, PV par jambe, tableau des flux (date, montant, DF, PV), par rate, annuité,
  **diagnostics** (temps, versions, avertissements). Jamais un simple `float`.

## 8. Risque (`core/risk`)

| Contrat | Responsabilité | Fichier |
|---|---|---|
| `BumpTarget` / `BumpSpec` | Quelles cotations choquer (par courbe, type d'instrument ou identité), de combien (bp) et selon quel schéma (`up`, `down`, `central`) | `bumps.py` |
| `Bumper` | `apply(snapshot, target, shift_bp)` renvoie un **nouveau** snapshot ; erreur si la cible ne sélectionne rien | `bumps.py` |
| `RiskMeasure` | `compute(instrument, snapshot, pricer, settings) -> RiskResult` ; une classe par mesure, enregistrée dans `RISK_MEASURES` | `measures.py` |
| Mesures (phase 2) | `ParallelDV01`, `KeyRateDV01`, `CurveSpecificDV01` (actualisation vs projection), `Convexity` | `measures.py` |
| `RiskEngine` | Orchestre les mesures sur un trade, mémorise les valorisations partagées entre mesures, renvoie un `RiskReport` | `engine.py` |
| Phase 3 | `ScenarioPnL`, `Scenario` (parallèle, steepener, flattener, twist), `CarryRolldown` | à venir |

Le risque dépend d'une fonction `pricer` abstraite `(instrument, snapshot) -> PV`, pas des moteurs : remplacer le moteur
ou le produit ne demande **aucun changement** dans `risk/`. Ajouter une mesure = un fichier qui l'enregistre dans
`RISK_MEASURES` (un test le prouve en l'enregistrant depuis `tests/`). C'est l'appelant (`services/`) qui construit le
pricer : re-bootstrap du snapshot choqué, puis moteur de pricing.

Définitions : le **par-rate DV01** choque les cotations de marché puis re-bootstrap (ce qu'utilisent les desks) ;
le zero-rate DV01 choque directement les taux zéro de la courbe. La v1 utilise le bump-and-reprice
(différences finies) ; la différentiation automatique est hors v1 mais ne doit pas être interdite par le design.

Carry/roll-down : exige une hypothèse explicite (courbe inchangée en espace de tenors vs forwards réalisés), à
trancher et consigner dans DECISIONS.md en phase 3.

## 9. Registries et factories (`core/registry`)

| Registry | Clé -> Valeur |
|---|---|
| `InstrumentRegistry` | `"vanilla_irs"` -> factory |
| `EngineRegistry` | (type d'instrument, méthode) -> moteur |
| `InterpolatorRegistry` | `"log_linear_df"` -> classe |
| `RISK_MEASURES` | `"key_rate_dv01"` -> classe (nom inconnu : `UnsupportedRiskMeasureError`, hérite de `UnsupportedInstrumentError`) |
| `ConventionsRegistry` | `"EUR"`, `"USD"` -> `CurrencyConventions` |

Enregistrement par décorateur : ajouter un composant = ajouter un fichier, sans modifier de ligne existante
(principe Open/Closed).

## 10. Exceptions (`core/errors.py`)

```
RatesPricerError
 ├── ConventionError
 ├── ScheduleError
 ├── MarketDataError
 ├── CalibrationError          (porte le pilier fautif et le résidu)
 ├── UnsupportedInstrumentError
 │    └── UnsupportedRiskMeasureError
 ├── PricingError
 └── RiskComputationError
```

Traduites en codes HTTP dans `docs/API.md`.

## 11. Matrice d'extensibilité

| Pour ajouter... | Je touche... | Je ne touche pas... |
|---|---|---|
| Un produit (cap, FRA) | `instruments/` + entrée dans le registry | modèles, courbes, API |
| Un modèle de taux (Hull-White) | nouveau package `models/` + moteurs | instruments, conventions |
| Une méthode numérique | `engines/` | produits, courbes |
| Un schéma d'interpolation | `interpolation/` | bootstrap, courbes |
| Une mesure de risque | `risk/` | pricer, produits |
| Une devise (USD) | configuration dans `conventions/` + `indices/` | logique de calcul |

`core/models/` n'existe pas en v1 (pas d'optionalité) mais la structure lui réserve sa place.

## 12. Structure du dépôt

```
rate-derivatives-pricer/
├── CLAUDE.md  README.md  pyproject.toml  uv.lock  .pre-commit-config.yaml
├── .gitignore  .gitattributes  .python-version
├── Dockerfile.api  Dockerfile.ui  docker-compose.yml      (phase 4)
├── .github/workflows/ci.yml
├── docs/   (ARCHITECTURE, ROADMAP, CONVENTIONS, API, DECISIONS)
├── data/   market/ (snapshots CSV/JSON)   trades/ (swaps d'exemple JSON)
├── src/rate_derivatives_pricer/
│   ├── core/
│   │   ├── conventions/  schedule/
│   │   ├── market/ (quotes, indices, curves, interpolation, bootstrap, providers)
│   │   ├── instruments/ (cashflows, legs, swaps)
│   │   ├── engines/  registry/  errors.py
│   │   ├── risk/ (bumps, measures, engine)
│   ├── services/  sdk/  ui/
│   └── api/   (logging_config.py : configuration du logging et request_id, en place depuis la phase 0)
└── tests/  unit/  integration/  validation/  api/  fixtures/
```

Un seul `pyproject.toml`, un seul package racine. Les dépendances sont réparties ainsi :

- **Dépendances de base** : NumPy (le `core`).
- **Extras optionnels** (`pip install paquet[api]`) : `api` (FastAPI, Pydantic, pydantic-settings, Uvicorn),
  `sdk` (httpx, Pydantic), `ui` (Streamlit, Plotly, et le SDK).
- **Groupe de développement `dev`** (installé par défaut par `uv sync`) : pytest, pytest-cov, hypothesis, QuantLib,
  ruff, mypy, import-linter, pre-commit.

Dataclasses dans `core` ; Pydantic uniquement dans `api`/`sdk`. Voir ADR-018.

## 13. Patterns de conception à savoir nommer en entretien

Strategy (interpolateurs, solveurs, moteurs), Factory + Registry (Open/Closed), composition plutôt qu'héritage
(jambes, cashflows), interfaces (ABC/Protocol), DTO + Mapper (API vs domaine), objets valeur immuables,
inversion de dépendance (le risque dépend d'une abstraction de pricer).
