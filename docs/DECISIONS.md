# DECISIONS

À placer dans `docs/DECISIONS.md`. Journal des décisions, style ADR. On ajoute de nouvelles entrées ; on ne réécrit
jamais l'historique (une décision est « remplacée » par une nouvelle entrée).
Format d'une entrée : **Contexte / Décision / Conséquences**.

Statuts : Acceptée, Remplacée, Reportée.

---

### ADR-001 : Périmètre de la v1, swaps uniquement (OIS + IRS vanille)
- **Statut :** Acceptée
- **Contexte :** L'objectif est d'apprendre le pricing de taux et l'ingénierie de production ; un périmètre produit
  étroit garde le focus sur le pipeline cotations -> courbe -> PV -> risque.
- **Décision :** La v1 couvre les swaps OIS et les IRS vanilles uniquement. Les autres produits viendront via le registry.
- **Conséquences :** La construction de courbes est le vrai cœur de la v1. Pas de modèle d'optionalité en v1.

### ADR-002 : Devise, EUR d'abord, USD par configuration
- **Statut :** Acceptée
- **Décision :** Démarrer en EUR (€STR, Euribor 3M/6M). Les conventions vivent dans `CurrencyConventions`, chargées
  depuis la configuration ; l'USD (SOFR) est ajouté en phase 3 **sans modifier la logique de calcul**.
- **Conséquences :** Aucune convention codée en dur dans le code de calcul.

### ADR-003 : QuantLib uniquement comme oracle de test
- **Statut :** Acceptée
- **Contexte :** Réécrire force à comprendre les conventions, le bootstrap et la composition ; QuantLib fournit un
  critère de réussite objectif.
- **Décision :** `core/` est écrit de zéro. QuantLib est dans le groupe `dev` uniquement et jamais importée par `core/`.
- **Conséquences :** Des exceptions au cas par cas sont possibles mais doivent être consignées ici.

### ADR-004 : Python + NumPy pur ; solveurs internes
- **Statut :** Acceptée
- **Décision :** Pas d'optimisation prématurée ; pas de Numba/C++ en v1. La recherche de racines est implémentée
  en interne (`NewtonSolver`, `BrentSolver`) derrière une interface `Solver` ; SciPy pourra être branché plus tard.
- **Conséquences :** L'architecture ne doit pas fermer la porte à Numba/C++.

### ADR-005 : API sans état, cache de courbes LRU en mémoire, pas de base de données en v1
- **Statut :** Acceptée
- **Contexte :** Un calcul sans état est reproductible, scalable et facile à déboguer.
- **Décision :** Chaque requête porte ou référence ses données. Les courbes sont mises en cache en mémoire avec pour
  clé un hash de snapshot + options + version du cœur. Snapshots et trades d'exemple sont des fichiers dans `data/`.
- **Conséquences :** Pas d'historique, pas de portefeuille persistant, pas de piste d'audit au-delà des logs. Le cache
  est par process (plusieurs workers = plusieurs caches). La persistance pourra être ajoutée derrière
  `TradeRepository` / `MarketDataProvider` ; Redis derrière `CurveCache`.

### ADR-006 : Architecture en couches avec règle de dépendance imposée
- **Statut :** Acceptée
- **Décision :** `ui -> sdk -> api -> services -> core`, à sens unique, dans un seul package racine
  `rate_derivatives_pricer` avec les sous-packages `core/ services/ api/ sdk/ ui/`. Vérifiée par `import-linter` en CI.
- **Conséquences :** L'UI est un vrai client de l'API ; `core` reste libre de toute dépendance web.

### ADR-007 : Cashflows polymorphes ; moteurs sans état choisis par registry
- **Statut :** Acceptée
- **Décision :** Un `Cashflow` calcule son montant à partir de l'interface `MarketEnvironment`. Les instruments ne
  portent pas leur moteur de pricing (contrairement à QuantLib) ; les moteurs sont sélectionnés via `EngineRegistry`.
- **Conséquences :** Plus simple à lire, tester et étendre ; aucun état partagé.

### ADR-008 : Représentation du temps, dates + day count interne
- **Statut :** Acceptée
- **Décision :** Les courbes travaillent en dates avec une `DayCountConvention` interne (Act/365F par défaut) pour
  l'interpolation, plutôt qu'en flottants bruts.
- **Conséquences :** Plus lourd mais plus proche de la production et plus facile à valider contre QuantLib.

### ADR-009 : Pas de date d'évaluation globale
- **Statut :** Acceptée
- **Décision :** La date de valorisation est une partie explicite de `MarketSnapshot`. Le
  `Settings.evaluationDate` global de QuantLib n'est volontairement pas reproduit.
- **Conséquences :** Les tests de validation doivent aligner la date d'évaluation de QuantLib sur la nôtre.

### ADR-010 : Risque par bump-and-reprice sur les instruments de calibration
- **Statut :** Acceptée
- **Décision :** Choquer les cotations de marché (instruments de calibration), re-bootstrap, re-pricer. Catalogue
  complet phasé : DV01 parallèle, DV01 par bucket, convexité, DV01 par courbe (phase 2) ; scénarios, carry/roll-down
  (phase 3). Le risque est un composant séparé, dépendant d'un pricer abstrait.
- **Conséquences :** La différentiation automatique est hors v1 mais non interdite. L'hypothèse carry/roll-down
  (courbe inchangée en espace de tenors vs forwards réalisés) **sera tranchée en phase 3**.

### ADR-011 : Conventions de l'API
- **Statut :** Acceptée
- **Décision :** Versioning par URL (`/v1`), snapshot par référence et par valeur, dates ISO 8601, taux en décimal,
  unités explicites sur les cotations, erreurs structurées, enveloppe de réponse avec `diagnostics` permanents,
  `request_id` partout. Pricing batch et métriques Prometheus en phase 4.
- **Conséquences :** Les unités explicites évitent les bugs classiques pourcent/décimal/bp en production.

### ADR-012 : Stratégie de logging
- **Statut :** Acceptée
- **Décision :** Trois niveaux (technique, calcul, audit), JSON structuré, `request_id` via `contextvars`, hashes
  d'entrée pour la rejouabilité, `core` n'utilise que `logging` standard. Ring buffer et `/v1/logs/{request_id}`
  en phase 4 ; squelette de logging JSON dès la phase 0/1.

### ADR-013 : Outillage et qualité
- **Statut :** Acceptée
- **Décision :** `uv`, `ruff`, `mypy` (strict sur `core/` et `services/`), `pytest`, `hypothesis`, `import-linter`,
  `pre-commit`, CI GitHub Actions, Python 3.12. Couverture >= 90 % sur `core/`.
- **Conséquences :** Les garde-fous qualité sont obligatoires dès la phase 0.

### ADR-014 : Tolérances de validation
- **Statut :** Acceptée
- **Décision :** Tolérances de départ dans CONVENTIONS.md (section 5) ; ajustées selon les écarts réels constatés,
  chaque ajustement documenté ici par une nouvelle entrée. Les tolérances sont des constantes nommées dans un seul
  module de test.

### ADR-015 : Langue et documentation (première version)
- **Statut :** Remplacée par ADR-017
- **Décision initiale :** Tout en anglais (code, docstrings, commits, docs).

### ADR-016 : Éléments reportés
- **Statut :** Reportée
- **Éléments :** Authentification, limitation de débit, calcul asynchrone avec file de tâches, WebSocket,
  persistance des résultats, cache Redis, Numba/C++, optionalité (caps/floors, swaptions), modèles short-rate,
  exercice anticipé.
- **Raison :** Hors périmètre v1 ; chacun aura sa propre entrée de roadmap et son ADR lorsqu'il sera planifié.

### ADR-017 : Langue, documentation en français, code en anglais
- **Statut :** Acceptée (22/10/2026, remplace ADR-015)
- **Contexte :** La documentation (README, `docs/`, `CLAUDE.md`) est un support de travail et d'apprentissage que le
  porteur du projet doit pouvoir relire et amender facilement. Le code, lui, doit suivre le standard de l'industrie
  et servir d'exercice d'anglais professionnel.
- **Décision :** Documentation en **français**. Code, identifiants, docstrings, commentaires de code, messages de
  commit et messages de log/erreur en **anglais**. Le code reste commenté pour la lisibilité.
- **Conséquences :** `CLAUDE.md` rappelle cette règle en tête de fichier. Les extraits de code et commandes dans la
  documentation restent en anglais.

### ADR-018 : Choix d'outillage de la phase 0
- **Statut :** Acceptée (22/10/2026)
- **Contexte :** La phase 0 met en place les garde-fous (ADR-013) ; plusieurs détails d'implémentation n'étaient pas
  fixés par les documents initiaux.
- **Décision :**
  - **Dépendances** : NumPy est la seule dépendance de base (`core`). `api`, `sdk` et `ui` sont des *extras*
    (`[project.optional-dependencies]`) ; l'outillage de dev est un *dependency group* `dev`, installé par défaut par
    `uv sync`. Un extra `sdk` (httpx, Pydantic) est ajouté car `ui` dépend du SDK et non de l'API. La CI installe tout
    avec `uv sync --locked --all-extras`.
  - **Python 3.12** épinglé (`requires-python = ">=3.12,<3.13"` et `.python-version`).
  - **mypy** : mode standard global (avec `check_untyped_defs`) et options strictes explicites sur `core.*` et
    `services.*` (l'option `strict` n'est pas autorisée par module).
  - **import-linter** : un contrat `layers` (ui > sdk > api > services > core), deux contrats `forbidden` (ui ne voit
    que le SDK ; le SDK ne descend pas sous l'API) et un contrat `forbidden` sur les paquets externes interdits dans
    `core` (FastAPI, Pydantic, Starlette, httpx, Streamlit, Plotly, SciPy, QuantLib).
  - **Logging** : le module `api/logging_config.py` porte la `ContextVar` du `request_id`, un `logging.Filter` qui
    l'injecte dans chaque enregistrement (y compris ceux de `core`), un formateur JSON (une ligne par événement, ASCII
    échappé) et un formateur texte pour le local. Le message du log est le nom stable de l'événement ; les champs
    métier passent par `extra`. Le préfixe `rate_derivatives_pricer.` est retiré du champ `logger`.
  - **pre-commit** : hooks locaux qui appellent `uv run` (ruff check, ruff format --check, mypy, lint-imports, tests
    unitaires), pour utiliser exactement les versions de `uv.lock`.
  - **Fins de ligne** : `.gitattributes` force LF dans le dépôt (développement sous Windows, CI sous Linux).
- **Conséquences :** `uv` doit être dans le `PATH` pour que le hook pre-commit fonctionne. Le `request_id` sera
  positionné par un middleware FastAPI en phase 1 via `request_context()`.
