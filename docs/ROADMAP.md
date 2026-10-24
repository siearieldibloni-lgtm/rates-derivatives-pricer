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

## Phase 1 : tranche verticale, valorisation d'un swap OIS

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

## Phase 2 : IRS vanille, multi-courbes, risque de base

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
