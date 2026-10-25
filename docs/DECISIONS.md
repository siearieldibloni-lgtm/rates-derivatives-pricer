# DECISIONS

Journal des décisions, style ADR. On ajoute de nouvelles entrées ; on ne réécrit
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

### ADR-019 : Conventions EUR €STR (OIS et dépôt overnight)
- **Statut :** Acceptée (24/10/2026, validées par le porteur du projet avant implémentation, règle 7 de CLAUDE.md)
- **Contexte :** Le bootstrap et les coupons OIS dépendent de conventions qu'on ne doit pas deviner ; elles doivent
  aussi être alignées sur QuantLib pour atteindre les tolérances de validation.
- **Décision :** Conventions décrites dans `core/conventions/data/EUR.json` (aucune n'est codée en dur) :
  - **Calendrier** TARGET ; **spot lag** 2 jours ouvrés (date de valorisation ajustée *following*, puis +2 jours).
  - **OIS** : fréquence annuelle sur les deux jambes (une seule période si maturité <= 1 an, stub en tête sinon),
    Act/360 sur les deux jambes, Modified Following (dates intermédiaires et date de fin), génération *backward*,
    **payment lag 1 jour ouvré TARGET**, composition quotidienne in arrears **sans lookback, lockout ni observation
    shift**.
  - **Règle de fin de mois** : appliquée si la date de départ est le dernier jour ouvré du mois ; la maturité est
    alors le dernier jour ouvré du mois cible, sinon `départ + tenor` ajusté par l'échéancier (comportement de
    `MakeOIS` de QuantLib).
  - **Dépôt overnight €STR** : départ T+0, fin à J+1 ouvré, Act/360, taux simple.
  - **Instruments de calibration** : dépôt ON puis OIS 1W, 2W, 1M, 2M, 3M, 6M, 9M, 1Y, 18M, 2Y-10Y, 12Y, 15Y, 20Y,
    25Y, 30Y.
  - **Day counts 30/360** : deux variantes explicites, `30/360` (Bond Basis, ISDA 4.16(f)) et `30E/360` (Eurobond,
    ISDA 4.16(g)), plutôt qu'un `Thirty360` ambigu.
- **Conséquences :** Les échéanciers sont identiques à ceux de QuantLib (`Schedule`, `MakeOIS`) date par date. Changer
  une convention revient à modifier le JSON et à rejouer la validation.

### ADR-020 : Fixings €STR et composition des coupons overnight
- **Statut :** Acceptée (24/10/2026, validée par le porteur du projet)
- **Contexte :** Un swap déjà commencé dépend des fixings passés ; le fixing du jour n'est publié qu'à J+1.
- **Décision :**
  - Fixings **strictement antérieurs** à la date de valorisation : obligatoires (`MarketDataError` sinon, code HTTP
    `MARKET_DATA_INVALID`, avec l'indice et la date manquante).
  - Fixing **du jour de valorisation** : utilisé s'il est fourni, projeté sinon (comportement QuantLib).
  - Partie projetée : le produit des forwards overnight se **télescope** en `P(d_k) / P(fin)` sur la courbe de
    projection. C'est exact (pas une approximation) et évite une boucle quotidienne ; la boucle jour par jour n'est
    faite que sur la partie réalisée.
  - Un flux payé **à la date de valorisation ou avant** est considéré comme échu et exclu du PV (équivalent de
    `includeReferenceDateEvents = false` dans QuantLib).
  - Un snapshot refuse tout fixing daté après sa date de valorisation.
- **Conséquences :** Le snapshot de référence contient les fixings €STR depuis le 02/01/2026 ; un swap commencé avant
  cette date est refusé avec une erreur explicite.

### ADR-021 : Frontières entre couches (DTO, schémas partagés, contrats import-linter)
- **Statut :** Acceptée (24/10/2026, complète ADR-006 et ADR-018)
- **Contexte :** `services` ne doit pas dépendre de Pydantic (réservé à `api`/`sdk`), et le SDK doit réutiliser les
  schémas de l'API (docs/API.md section 8). Le contrat import-linter de la phase 0 interdisait à `ui` d'importer
  `api` *même indirectement*, ce qui rendait impossible la réutilisation des schémas par le SDK.
- **Décision :**
  - `api/schemas.py` ne dépend que de Pydantic (ni FastAPI ni `core`) ; le SDK le réexporte dans `sdk/models.py`.
  - `services/requests.py` définit les entrées du service en dataclasses figées ; les routes convertissent les
    modèles Pydantic en ces dataclasses, `services/mappers.py` les convertit en objets du domaine et transforme les
    résultats en dictionnaires JSON, validés ensuite par les schémas de réponse.
  - Le contrat `ui` est scindé : (1) `ui` n'atteint jamais `services` ni `core`, même indirectement ; (2) `ui`
    n'importe jamais `api` directement (import indirect via le SDK autorisé).
  - Les paramètres d'un trade sont validés par un schéma propre à son type (`TRADE_PARAMS_SCHEMAS`), publié dans
    `/v1/catalog` (JSON Schema) pour que l'UI puisse construire ses formulaires.
- **Conséquences :** Une rupture du contrat HTTP casse immédiatement le SDK et le dashboard (tests `tests/api`).

### ADR-022 : Choix numériques du bootstrap
- **Statut :** Acceptée (24/10/2026)
- **Décision :**
  - **Pilier** d'un OIS = sa dernière date de paiement (fin + payment lag), comme `Pillar::LastRelevantDate` de
    QuantLib ; pilier d'un dépôt = sa maturité. Deux instruments sur le même pilier sont refusés.
  - **Bootstrap séquentiel** en une passe (exact avec une interpolation locale) ; le solveur cherche `P_i` dans
    `[P_{i-1} e^{-1,0 dt}, P_{i-1} e^{+0,5 dt}]` (forwards entre -50 % et +100 %).
  - **Solveur par défaut** : Brent, tolérance absolue 1e-15 sur le DF ; Newton (dérivée par différences finies,
    garde-fou par bissection) disponible en option. Un pilier dont le résidu final dépasse 1e-12 (en taux décimal)
    lève `CalibrationError` avec le pilier et le résidu.
  - **Extrapolation** au-delà du dernier pilier : forward instantané constant pour les deux interpolations
    (`log_linear_df` et `linear_zero`), comme QuantLib. Pour `linear_zero`, le zéro en `t = 0` est pris égal à
    celui du premier pilier.
  - **Temps des courbes** : Act/365F depuis la date de valorisation (ADR-008).
- **Conséquences :** Résidus de calibration mesurés < 1e-14 ; 24 piliers bootstrapés en ~0,1 s.

### ADR-023 : Données de marché synthétiques
- **Statut :** Acceptée (24/10/2026, validée par le porteur du projet)
- **Décision :** Le snapshot `data/market/EUR_2026-10-19.json` est **synthétique** (niveaux plausibles, €STR ~1,92 %,
  courbe OIS 1,87 %-2,73 %) et l'indique dans son champ `description`. Les fixings €STR (02/01/2026-16/10/2026) sont
  générés sur les jours ouvrés TARGET avec 3 décimales, comme la publication BCE.
- **Conséquences :** Aucun chiffre de ce dépôt ne doit être lu comme un niveau de marché réel. Des cotations réelles
  pourront remplacer ce fichier sans changement de code.

### ADR-024 : Validation contre QuantLib, bilan de la phase 1
- **Statut :** Acceptée (24/10/2026, applique ADR-014)
- **Contexte :** Les tolérances de départ (CONVENTIONS.md section 5) doivent être confrontées aux écarts réels.
- **Décision :**
  - Tolérances **inchangées** (module unique `tests/validation/tolerances.py`). Écarts maximaux mesurés sur 9 marchés
    (3 dates de valorisation, dont une fin de mois et une veille de Pâques, x 3 chocs parallèles -250/0/+150 bp) :
    DF 2,5e-13 (`log_linear_df`) et 2,3e-11 (`linear_zero`, l'écart vient de la précision du bootstrap QuantLib),
    PV/notionnel 2,3e-13, par rate 9,9e-11 relatif, annuité 5,6e-14 relatif, sur 1 404 swaps spot.
  - Le PV est comparé **relativement au notionnel** : un swap proche du par a un PV proche de zéro, pour lequel un écart
    relatif au PV n'a pas de sens.
  - QuantLib reçoit la même date d'évaluation que nous via un gestionnaire de contexte (`evaluation_date`) qui restaure
    la valeur globale précédente (ADR-009).
- **Conséquences :** La validation tourne en ~20 s (`uv run pytest -m validation`).

### ADR-025 : Détails du contrat HTTP v1
- **Statut :** Acceptée (24/10/2026, précise ADR-011)
- **Décision :**
  - Tous les endpoints `/v1/*` répondent avec l'enveloppe `request_id` + `result` + `diagnostics` ; `/health` et
    `/ready` répondent `{"status", "checks"}` (`/ready` en 503 si un registry est vide).
  - `diagnostics.input_hashes` expose les empreintes du marché et du trade (mêmes valeurs que dans les logs d'audit).
  - Snapshot introuvable : `MarketDataNotFoundError` (sous-classe de `MarketDataError`) traduite en 404 `NOT_FOUND`.
  - Trade dont la devise diffère de celle du marché : `INVALID_TRADE_DEFINITION` (pas de FX en v1).
  - `X-Request-ID` client accepté s'il fait 1 à 128 caractères `[A-Za-z0-9._-]`, sinon remplacé par un id généré.
  - Les exceptions inattendues sont interceptées par le middleware ASGI (qui connaît encore le `request_id`) :
    réponse 500 `INTERNAL_ERROR` générique, trace complète dans les logs.
  - Variables d'environnement : `LOG_LEVEL`, `LOG_FORMAT`, `MARKET_DATA_DIR`, `CURVE_CACHE_SIZE`,
    `CURVE_CACHE_TTL_SECONDS`, `GIT_COMMIT` ; `PRICER_API_URL` pour le dashboard.
  - Les routes de calcul sont des fonctions synchrones : FastAPI les exécute dans un thread, le bootstrap (CPU) ne
    bloque pas la boucle d'événements, et le `request_id` (ContextVar) est propagé au thread.

### ADR-026 : Clé du cache de courbes
- **Statut :** Acceptée (24/10/2026, précise docs/API.md section 6)
- **Décision :** Clé = SHA-256 de la sérialisation JSON canonique (clés triées) de : données du snapshot (date,
  devise, cotations avec leurs unités, fixings), interpolation, **solveur**, version du cœur. Le **nom** et la
  description du snapshot sont exclus : les mêmes données passées par référence ou par valeur partagent la même
  entrée de cache.
- **Conséquences :** Un PV différent avec une même empreinte d'entrée et une même version révèle un bug ; le cache
  reste par process (ADR-005).
