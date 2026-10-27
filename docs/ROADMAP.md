# ROADMAP

Une phase = une branche = une PR. La CI doit passer avant la fusion ;
`DECISIONS.md` est mis à jour dans la même PR.

## Principes

- **Tranche verticale d'abord** : dès la phase 1, un swap est pricé de bout en bout (cotations -> courbe -> PV -> API -> UI).
- Chaque phase se termine par un système fonctionnel ; pas de phase « plomberie seule » après la phase 0.
- Les critères de réussite sont mesurables (tests verts, écart vs QuantLib sous tolérance).

## Phase 0 : fondations du dépôt — TERMINÉE

> Fusionnée dans `main` (PR #1, branche `phase-0-foundations`). CI verte sur la PR et sur `main`.
> Choix d'outillage consignés dans ADR-018. Prochaine phase : la phase 1.

**Livrables**
- Repo Git, arborescence d'ARCHITECTURE.md, `pyproject.toml` avec groupes de dépendances.
- `uv`, `ruff`, `mypy`, `pytest`, `hypothesis`, `import-linter`, `pre-commit` configurés.
- CI GitHub Actions minimale (lint + types + import-linter + tests).
- Squelette de logging JSON avec `request_id` (via `contextvars`), utilisable par le cœur via `logging` standard.
- `CLAUDE.md` et `docs/` initiaux.

**Critères de réussite**
- `uv sync && uv run pytest` passe sur une machine vierge.
- Un import interdit (ex. `core` important `api`) **fait échouer la CI**.
- Un commit qui viole `ruff` ou `mypy` est bloqué par `pre-commit`.

**Bilan de vérification**
- `uv sync && uv run pytest` : 21 tests verts, en local et sur la CI GitHub (machine Linux vierge).
- Import interdit : `lint-imports` échoue (code de sortie 1) sur un `import QuantLib` ou un import `core -> api`.
  Vérifié en local ; la CI exécute la même commande.
- Commit fautif : un commit contenant une erreur `mypy` a été refusé par le hook `pre-commit`.
- **Protection de branche** GitHub activée sur `main` (CI obligatoire avant fusion). C'est un réglage du dépôt,
  pas du code : il n'apparaît pas dans l'historique git (*Settings -> Rules*).

## Phase 1 : tranche verticale, valorisation d'un swap OIS — TERMINÉE

> Fusionnée dans `main` (PR #3, branche `phase-1-ois-vertical-slice`). CI verte sur la PR. Conventions validées avant
> implémentation (ADR-019, ADR-020) ; décisions de la phase : ADR-019 à ADR-026. Prochaine phase : la phase 2.

**Cœur**
- `TargetCalendar`, `Act360`, `Act365Fixed`, `Thirty360`, business day conventions, `Tenor`, `CurrencyConventions` (EUR).
- `ScheduleGenerator` / `Schedule`.
- `DiscountCurve`, `LogLinearOnDiscountFactor`, `LinearOnZeroRate`, `NewtonSolver`, `BrentSolver`.
- `DepositQuote`, `OisSwapQuote` et leurs `CalibrationInstrument`.
- `CurveBuilder` : **courbe OIS €STR uniquement** (mono-courbe).
- `FixedCoupon`, `OvernightCompoundedCoupon`, `Leg`, `OisSwap`.
- `DiscountingSwapEngine`, `PricingResult` (PV, par rate, annuité, flux).
- Registries (instruments, moteurs, interpolateurs, conventions).
- `FileMarketDataProvider` + premier snapshot dans `data/market/`.

**Service**
- `/health`, `/ready`, `/v1/version`, `/v1/catalog`, `/v1/curves/build`, `/v1/price` (swap OIS).
- Enveloppe de réponse avec `diagnostics`, erreurs structurées, `request_id`, cache de courbes LRU.
- SDK minimal (`price`, `build_curves`) ; Streamlit minimal (choix du snapshot, saisie d'un swap OIS, PV et courbe).

**Critères de réussite**
- Calendriers, day counts, schedules : **égalité exacte** avec QuantLib sur la grille de tests.
- Courbe OIS bootstrapée : écart sur les DF < 1e-10 vs QuantLib à chaque pilier.
- PV, par rate, annuité d'un swap OIS : écart relatif < 1e-8 vs QuantLib sur une grille (maturités x sens x notionnels).
- Un swap au par rate a un PV nul (test de propriété, tolérance 1e-10).
- Couverture de `core/` >= 90 %.

**Bilan de vérification** (en local sous Windows et sur la CI GitHub sous Linux)
- Boucle `ruff check`, `ruff format --check`, `mypy` (strict sur `core/` et `services/`), `lint-imports` (5 contrats)
  et `pytest` : **265 tests verts**, dont 75 de validation QuantLib.
- Calendrier TARGET (1995-2060), `adjust`/`advance`/fin de mois, day counts, échéanciers (fréquences x règles x fin
  de mois x 2 ans de dates de départ) : **égalité exacte** avec QuantLib.
- Courbe €STR sur 9 marchés (3 dates de valorisation x 3 chocs) : écart DF max **2,5e-13** (`log_linear_df`) et
  2,3e-11 (`linear_zero`) aux piliers, entre piliers et en extrapolation (critère : 1e-10).
- 1 404 swaps OIS spot (13 maturités x 2 sens x 2 notionnels x 3 taux fixes x 9 marchés) plus des swaps déjà
  commencés et forward : PV/notionnel **2,3e-13**, par rate 9,9e-11, annuité 5,6e-14 (critère : 1e-8).
- PV nul au par rate (hypothesis) : |PV| <= 1e-10 x notionnel.
- Couverture de `core/` : **99 %** (96 % sur l'ensemble du paquet).
- Tranche verticale : `uvicorn` sert les endpoints de la phase 1, le SDK et le dashboard Streamlit les consomment ;
  un test d'intégration vérifie que SDK -> HTTP -> API -> services -> cœur donne exactement les mêmes nombres que le
  cœur appelé directement, et un *golden file* fige les résultats de référence.

## Phase 2 : IRS vanille, multi-courbes, risque de base — IMPLÉMENTÉE

> Branche `phase-2-irs-multicurve-risk`, en attente de fusion (puis clôture des docs comme en phase 1).
> Conventions validées avant implémentation (ADR-027) ; décisions de la phase : ADR-027 à ADR-031.

**Livrables**
- `IborIndex` (Euribor 3M/6M), `IborSwapQuote`, `IborCoupon`, `VanillaIrs`.
- **Bootstrap multi-courbes** : actualisation OIS + projection Euribor.
- `Bumper`, `BumpSpec`, `RiskEngine`, `RiskReport`.
- Mesures : `ParallelDV01`, `KeyRateDV01` (bump des instruments de calibration + re-bootstrap), `Convexity`, `CurveSpecificDV01`.
- `/v1/risk`.
- Streamlit : onglet Marché (courbes zéro/forward/DF), onglet Trade (échéancier, PV par jambe), premier onglet Risque
  (histogramme des DV01 par bucket).

**Critères de réussite**
- Courbe de projection Euribor : écart sur les DF < 1e-10 vs QuantLib.
- PV et par rate d'un IRS vanille : écart relatif < 1e-8 vs QuantLib.
- DV01 par bucket : écart relatif < 1e-6 vs différences finies QuantLib sur les mêmes cotations.
- **Cohérence interne** : somme des DV01 par bucket ≈ DV01 parallèle (test de propriété, tolérance documentée).
- Ajout d'une mesure de risque de test **sans modifier** `risk/` ni les moteurs (preuve d'extensibilité).

**Bilan de vérification** (en local sous Windows ; la CI Linux le rejouera sur la PR)
- Boucle `ruff check`, `ruff format --check`, `mypy` (strict sur `core/` et `services/`), `lint-imports` (5 contrats) et
  `pytest` : **424 tests verts** (dont 152 de validation QuantLib), couverture totale **96 %**, `core/` >= 94 % par fichier.
- Courbes Euribor 3M et 6M sur 9 marchés x 2 interpolations : écart DF max **3,5e-13** (`log_linear_df`) et 3,0e-12
  (`linear_zero`) (critère : 1e-10), mêmes piliers que QuantLib.
- IRS vanille : 972 swaps spot plus swaps déjà commencés et forward : PV/notionnel **2,2e-13**, par rate 4,0e-10,
  annuité 5,6e-14 (critère : 1e-8) ; échéanciers (dont stubs) à égalité exacte avec QuantLib.
- DV01 par bucket vs différences finies QuantLib : **9,8e-12** du DV01 parallèle (critère : 1e-6) ; somme des buckets =
  DV01 parallèle à 2,1e-7 près (tolérance 1e-6).
- Extensibilité : un test enregistre une mesure de risque depuis `tests/` sans modifier `risk/` ni les moteurs.
- Performance (ADR-028) : key-rate d'un IRS 5 ans en ~2,5 s (43 s avant optimisation exacte).
- Tranche verticale : `/v1/risk`, SDK (`vanilla_irs`, `risk`) et dashboard (onglets Marché, Trade, Risque) ; test
  d'intégration SDK -> HTTP -> cœur identique au bit près, et *golden file* de la phase 2.

## Phase 3 : risque avancé

**Livrables**
- `Scenario`, `ScenarioPnL` (parallèle ±, steepener, flattener, twist), `/v1/risk/scenarios`.
- `CarryRolldown` (hypothèse consignée dans DECISIONS.md).
- Conventions USD (SOFR, calendrier US Government) ajoutées **uniquement par configuration**.
- Streamlit : matrice de scénarios, carry/roll-down, comparaison de deux snapshots.

**Critères de réussite**
- L'USD est ajouté **sans modifier la logique de calcul** (configuration + tests uniquement).
- Un swap USD SOFR validé vs QuantLib avec les mêmes tolérances que l'EUR.
- PV scénario − PV base cohérent avec DV01 et convexité (vérification de Taylor sur petits chocs).
- Hypothèse carry/roll-down documentée et testée sur un cas simple calculable à la main.

## Phase 4 : industrialisation et dashboard complet

**Livrables**
- Dockerfiles (API, UI), `docker-compose.yml`, smoke test du conteneur en CI.
- Logs de calcul et d'audit complets, **ring buffer** de logs, `GET /v1/logs/{request_id}`.
- `/metrics` (Prometheus), `GET /v1/cache/stats`, `DELETE /v1/cache`.
- `/v1/batch/price` avec résultats partiels par trade.
- SDK complet (retries, timeouts, exceptions typées, vérification de version).
- Dashboard final : onglets Marché, Trade, Risque, Scénarios, **Diagnostic** (timings, cache, warnings, écarts vs
  QuantLib, timeline des logs par `request_id`).
- `README` complet, diagramme d'architecture, guide de démarrage.

**Critères de réussite**
- `docker compose up` en une commande ; smoke test CI vert.
- À partir d'un `request_id`, on reconstruit la chronologie complète d'un calcul via logs et endpoint.
- Un lot de 100 trades est pricé avec gestion d'erreur par trade (un trade invalide n'empêche pas les autres).
- Un tiers peut cloner le repo et lancer la plateforme en suivant le README seul.

## Phase 5 (hors v1) : extensions candidates

Caps/floors et swaptions (Black/Bachelier, nappe de vol), modèles short-rate (Hull-White), exercice anticipé,
Numba/C++, persistance (`TradeRepository`), authentification, cache Redis (derrière une interface `CurveCache`).
Chacune devient une entrée de roadmap à part entière le moment venu.

## Vue synthétique

```
Phase 0  Fondations         outillage, CI, logging, docs
Phase 1  Tranche verticale  swap OIS de bout en bout (API + UI)
Phase 2  IRS + multi-courbes  DV01 / key-rate / convexité
Phase 3  Risque avancé      scénarios, carry/roll, USD par configuration
Phase 4  Industrialisation  Docker, observabilité, batch, dashboard complet
Phase 5  Extensions         options, modèles, performance
```
