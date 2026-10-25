# Guide de la phase 1 : un swap OIS €STR valorisé de bout en bout

Ce guide explique ce que la phase 1 a construit, comment les pièces s'emboîtent, le rôle de chaque fichier et les
raisons des choix de conception. Il complète `ARCHITECTURE.md` (le plan), `ROADMAP.md` (les critères de réussite) et
`DECISIONS.md` (les ADR-019 à ADR-026, qui détaillent chaque décision citée ici).

État décrit : `main` après la fusion de la PR #3 (phase 1) et de la PR #4 (clôture).

---

## 1. L'objectif en une phrase

À partir de **cotations de marché** (taux de swaps €STR), on **construit une courbe d'actualisation**, puis on
**valorise un swap OIS** : PV, taux de par, annuité et tableau des flux. Le tout est exposé par une **API HTTP**,
consommé par un **SDK Python** et affiché dans un **dashboard**, et chaque étape est **validée contre QuantLib**.

```
cotations (JSON) -> courbe €STR (bootstrap) -> swap OIS -> PV, par rate, annuité
                                                             |
                     ui  ->  sdk  ->  HTTP  ->  api  ->  services  ->  core
```

Bilan chiffré : 78 fichiers, environ 9 500 lignes, **265 tests**. Écart maximal avec QuantLib : 2,5e-13 sur les
discount factors et 2,3e-13 sur le PV rapporté au notionnel. Couverture de `core/` : 99 %. CI verte.

---

## 2. Le minimum de finance pour suivre

| Notion | Définition | Où elle vit dans le code |
|---|---|---|
| **€STR** | Taux overnight de la BCE : le taux d'un prêt d'un jour ouvré à l'autre. Il est publié à J+1. | `OvernightIndex` |
| **Swap OIS** | On échange un **taux fixe** K contre l'**€STR composé jour après jour** sur chaque période. | `OisSwap` |
| **Discount factor** P(t) | Valeur aujourd'hui d'1 € reçu à la date t. P(aujourd'hui) = 1, et P décroît quand les taux sont positifs. | `DiscountCurve` |
| **Bootstrap** | On retrouve les P(t) un à un, pilier après pilier, pour que chaque swap coté soit exactement au par. | `CurveBuilder` |
| **Taux de par** | Le taux fixe qui annule le PV du swap. C'est ce que le marché cote. | `par_rate` |
| **Annuité** | Σ N·τᵢ·P(paiementᵢ) : la valeur d'un taux fixe de 1, payé sur la jambe fixe. | `annuity` |
| **PVBP** | Annuité × 1 bp : ce que rapporte 1 bp de plus sur le taux fixe. | `pvbp` |
| **Fixing** | La valeur publiée d'un indice pour un jour passé, nécessaire pour un swap déjà commencé. | `Fixing` |
| **Spot lag / payment lag** | Le swap démarre 2 jours ouvrés après la date de valorisation (spot lag) ; chaque flux est payé 1 jour ouvré après la fin de sa période (payment lag). | `EUR.json` |

Les trois formules au cœur de la phase :

- **Coupon overnight** : C = Π(1 + rᵢ·δᵢ), montant = N·(C − 1). Pour la partie future, ce produit se « télescope »
  exactement en P(début)/P(fin).
- **PV** = Σ_jambes signe × Σ montantᵢ × P(paiementᵢ).
- **Taux de par** = PV(jambe overnight) / annuité.

---

## 3. Le fil rouge : la vie d'une requête `POST /v1/price`

1. **UI ou client** : `PricerClient.price("EUR_2026-10-19", ois_swap(1e7, 0.0225, "pay_fixed", "5Y"))` envoie le
   JSON. Code : `sdk/client.py`.
2. **Middleware** : il lit l'en-tête `X-Request-ID` (ou en génère un) et l'attache au contexte. À partir de là,
   **chaque log**, y compris ceux du cœur, porte cet identifiant. Code : `api/middleware.py`.
3. **Validation Pydantic** du corps de la requête (`api/schemas.py`), puis validation des paramètres selon le type de
   trade (`api/routes.py`).
4. **Service** (`services/pricer_service.py`) :
   - il charge le snapshot (`FileMarketDataProvider`) ;
   - il calcule l'**empreinte** des données (SHA-256), puis consulte le **cache** ;
   - en cas d'absence, il **bootstrape** la courbe (`CurveBuilder`) et la met en cache ;
   - il construit l'instrument avec la factory (`INSTRUMENTS.get("ois_swap")`) ;
   - il le valorise avec le moteur (`DiscountingSwapEngine`) ;
   - il écrit le log d'audit `calculation_audit`.
5. **Mapper** (`services/mappers.py`) : il transforme le `PricingResult` en dictionnaire JSON.
6. **Route** : elle emballe le résultat dans l'enveloppe `{request_id, result, diagnostics}`.
7. **SDK** : il reconstruit des objets typés (`Envelope[PriceResult]`), ou lève une exception typée si l'API renvoie
   une erreur.

---

## 4. Le cœur quantitatif `core/` (NumPy + bibliothèque standard uniquement)

### 4.1 Les fondations transversales

**`core/errors.py`** — la hiérarchie d'exceptions, avec une classe par mode d'échec : `ConventionError`,
`ScheduleError`, `MarketDataError`, `MarketDataNotFoundError` (pour pouvoir renvoyer un 404), `CalibrationError`
(qui porte le pilier fautif et le résidu), `UnsupportedInstrumentError`, `PricingError`, `RiskComputationError`.
Chaque erreur a un `message` et des `details` structurés. L'API traduit chaque classe en un code HTTP stable.

**`core/registry/base.py`** — la classe générique `Registry[K, V]` : un dictionnaire « clé → composant » qu'on
remplit avec un décorateur (`@INTERPOLATORS.register("log_linear_df")`).
- C'est le principe **Open/Closed** : pour ajouter un composant, on ajoute un fichier sans toucher au code existant.
- Une clé inconnue lève l'erreur métier propre au registry, accompagnée de la liste des clés disponibles.
- Un doublon lève une erreur au lieu d'écraser silencieusement l'entrée existante.

**`core/registry/__init__.py`** — les six registries concrets : `CALENDARS`, `DAY_COUNTS`, `INTERPOLATORS`,
`INSTRUMENTS`, `ENGINES`, `CONVENTIONS`. On y trouve aussi `load_builtin_components()`, qui importe les modules
intégrés au premier accès. Ce chargement paresseux évite les **imports circulaires** : un module de calendrier
importe le registry pour s'y enregistrer, donc le registry ne peut pas importer les calendriers au chargement. Le
chargeur est commun à tous les registries et sait se rappeler lui-même sans effet (réentrant).

### 4.2 Les conventions de marché `core/conventions/`

C'est la couche « grammaire des dates », la source classique d'écarts entre systèmes.

**`business_day.py`** — l'énumération `BusinessDayConvention` : FOLLOWING, MODIFIED_FOLLOWING, PRECEDING,
UNADJUSTED. Elle dit comment déplacer une date qui tombe un jour férié. *Modified Following* avance au jour ouvré
suivant, sauf si l'on change de mois : on recule alors au jour ouvré précédent.

**`tenor.py`** — les objets valeur `Tenor` (« 3M », « 10Y », « ON » = 1 jour) et `Frequency` (ANNUAL,
QUARTERLY…), plus l'arithmétique de dates sans calendrier. Quand on ajoute des mois, le jour est ramené à la longueur
du mois cible : 31 janvier + 1 mois = 28 ou 29 février.

**`calendar.py`** — les calendriers :
- La classe abstraite `Calendar` sait dire si un jour est ouvré, ajuster une date, avancer de *n* jours ouvrés ou
  d'un tenor, et trouver la fin de mois.
- `TargetCalendar` implémente le calendrier TARGET : 1er janvier, Vendredi saint, lundi de Pâques, 1er mai, 25 et
  26 décembre, avec les règles historiques d'avant 2000. Pâques est calculé par l'algorithme de Meeus. Les jours
  fériés sont mis en cache, car on interroge le calendrier jour par jour.
- `NullCalendar` (tous les jours sont ouvrés) sert à l'arithmétique « non ajustée » des échéanciers.
- `JointCalendar` combine les jours fériés de plusieurs calendriers ; `calendar_from_name("TARGET+…")` le construit.
- La **règle de fin de mois** est appliquée ici : si l'on part du dernier jour ouvré d'un mois, on arrive au dernier
  jour ouvré du mois cible.

**`day_count.py`** — les conventions de décompte : `Act360` (jours réels / 360, celle de l'€STR), `Act365Fixed`
(qui sert aussi d'horloge interne aux courbes), `Thirty360` (*Bond Basis*) et `Thirty360European` (30E/360). Deux
variantes 30/360 explicites plutôt qu'une seule ambiguë (ADR-019).

**`currency.py` + `data/EUR.json`** — `CurrencyConventions` regroupe tout ce qui caractérise une devise :
calendrier, spot lag, horloge des courbes, indice overnight, conventions du dépôt et conventions des OIS. Elles sont
**lues dans le JSON** et ne sont jamais codées en dur.
- Ajouter l'USD en phase 3 reviendra à ajouter `USD.json`, sans modifier le code de calcul : c'est l'objectif de
  l'ADR-002.
- Les conventions EUR ont été validées par le porteur du projet avant implémentation (ADR-019) : spot lag 2, flux
  annuels, Act/360, Modified Following, règle de fin de mois, génération *backward*, payment lag 1, sans lookback.

### 4.3 Les échéanciers `core/schedule/generator.py`

`ScheduleGenerator` transforme « départ, fin, fréquence » en une liste de périodes `SchedulePeriod` (début et fin
d'accrual, date de paiement, indicateur de stub). L'algorithme suit le marché et QuantLib :

1. **Dates brutes** : on recule depuis la date de fin par pas de 1 an (*backward*). Le reliquat devient un stub
   (période courte) en tête.
2. **Règle de fin de mois**, si elle s'applique ; sinon, ajustement de chaque date en Modified Following.
3. Suppression des dates que l'ajustement a fait se chevaucher.
4. **Date de paiement** = fin de période + payment lag (en jours ouvrés).

C'est le fichier où la validation contre QuantLib a été la plus instructive. En génération *backward* avec la règle de
fin de mois, QuantLib ne déplace **pas** la date de départ en fin de mois. Le test de validation l'a détecté et
l'implémentation a été corrigée.

### 4.4 Les données de marché `core/market/` (première moitié)

**`indices.py`** — `OvernightIndex` : nom (« ESTR »), devise, calendrier de fixing, day count. C'est une simple
description, sans comportement.

**`quotes.py`** — les cotations `Quote`, `DepositQuote` et `OisSwapQuote`, toujours accompagnées d'une **unité
explicite** (`percent`, `decimal` ou `bp`). La propriété `.rate` convertit toujours en décimal. Confondre 2,5 et 0,025
est un bug de production classique ; l'unité obligatoire l'empêche.

**`snapshot.py`** — `MarketSnapshot` : date de valorisation, cotations et fixings, le tout figé (immuable). C'est
**l'unité de choc** du futur calcul de risque : un choc produira un nouveau snapshot. La validation refuse les
cotations en double et les fixings datés après la date de valorisation.

**`providers.py`** — `MarketDataProvider`, une interface, et `FileMarketDataProvider`, qui lit
`data/market/<nom>.json`.
- Les noms de snapshot sont filtrés par une expression régulière, contre les tentatives du type `../secrets`
  (*path traversal*).
- `snapshot_from_dict` et `snapshot_to_dict` définissent le **format canonique**, réutilisé pour les données passées
  par valeur et pour l'empreinte du cache.

### 4.5 La machinerie numérique `core/market/` (seconde moitié)

**`interpolation.py`** — deux stratégies interchangeables (pattern Strategy) :
- `LogLinearOnDiscountFactor` (par défaut) : ln P est linéaire entre les piliers, donc les forwards sont constants
  par morceaux. C'est le standard des courbes OIS.
- `LinearOnZeroRate` : interpolation linéaire du taux zéro.

Au-delà du dernier pilier, les deux prolongent la courbe à **forward instantané constant**, comme QuantLib. Cet
alignement a été fait après un écart détecté au-delà de 30 ans (ADR-022).

**`solvers.py`** — la recherche de racine, écrite en interne (pas de SciPy dans `core`, ADR-004) :
- `BrentSolver` : la méthode de Brent (1973). Elle combine interpolation quadratique inverse et bissection, et
  converge à coup sûr si la racine est encadrée.
- `NewtonSolver` : Newton avec une dérivée par différences finies, protégé par une bissection si un pas sort de
  l'intervalle.

Les deux renvoient un `SolverResult` (racine, résidu, nombre d'itérations, convergence) plutôt que de lever une
exception. C'est le bootstrap qui décide quoi faire d'un échec.

**`curves.py`** — `Curve` (interface) et `DiscountCurve`, construite à partir de nœuds (date, DF) et d'un
interpolateur. Elle fournit `discount_factor`, `zero_rate` (continu, Act/365F) et `forward_rate(d1, d2, day_count)`.
Les dates sont converties en temps avec l'horloge interne (ADR-008). Demander un DF avant la date de référence lève
une erreur.

**`environment.py`** — `MarketEnvironment`, ce que consomment les instruments et les moteurs : courbe
d'actualisation, courbes de projection par indice, fixings, rapports de calibration. Les dictionnaires sont en
**lecture seule** (`MappingProxyType`), car un environnement en cache est partagé entre plusieurs requêtes.
`PillarReport` et `CalibrationReport` contiennent les diagnostics de chaque pilier : cotation, cotation implicite,
résidu, itérations, DF.

**`calibration.py`** — l'équivalent du `RateHelper` de QuantLib. Un `CalibrationInstrument` sait calculer la
**cotation implicite** d'une courbe candidate et indique quel nœud il détermine (son pilier).
- `DepositCalibration` : taux simple (P(début)/P(fin) − 1)/τ.
- `OisCalibration` : taux de par d'un swap de notionnel 1. Son pilier est la **dernière date de paiement**, car le
  payment lag fait dépasser l'échéance.

**`bootstrap.py`** — `CurveBuilder.build(snapshot)`, le cœur de la phase :
1. Il convertit chaque cotation en instrument de calibration et les trie par pilier. Deux piliers identiques sont
   refusés.
2. Pour chaque pilier, avec les nœuds précédents déjà connus, il cherche le DF tel que *cotation implicite − cotation
   de marché = 0*. La recherche se fait par Brent dans un intervalle correspondant à des forwards entre -50 % et
   +100 %.
3. Si le résidu final dépasse 1e-12, il lève `CalibrationError` avec le pilier et le résidu.
4. Il produit le `MarketEnvironment` mono-courbe : la courbe €STR sert à la fois à actualiser et à projeter.

Il émet un log DEBUG par pilier et un log INFO de synthèse. Résultat : 24 piliers en ~0,1 s, résidus inférieurs à
1e-14.

### 4.6 Les instruments `core/instruments/`

**`cashflows.py`** — des **cashflows polymorphes** : chacun calcule son propre montant à partir de l'environnement
(ADR-007).
- `Coupon` est une dataclass de base (notionnel, dates d'accrual, date de paiement, day count) ; le montant vaut
  N × taux × τ.
- `FixedCoupon` : le taux est constant.
- `OvernightCompoundedCoupon` : le coupon €STR, la pièce la plus délicate (ADR-020).
  - **Partie réalisée** : boucle jour par jour sur les fixings. Ils sont obligatoires avant la date de valorisation ;
    celui du jour même est utilisé s'il est connu.
  - **Partie future** : télescopage exact en P(dₖ)/P(fin), sans boucle.

**`legs.py`** — `Leg` regroupe des cashflows, un notionnel et un sens (`PayReceive`, signe ±1). C'est de la
composition plutôt que de l'héritage.

**`swaps.py`** — `Instrument`, `Swap` et `OisSwap`, plus `SwapDirection` (pay_fixed ou receive_fixed). Un instrument
**décrit un contrat** et ne porte pas son moteur de pricing, contrairement à QuantLib (ADR-007).

**`factories.py`** — c'est ici qu'un contrat est construit à partir des conventions :
- `spot_date` calcule la date de départ spot.
- `ois_maturity_date` reproduit la logique de `MakeOIS`, règle de fin de mois comprise.
- `build_ois_swap` génère les deux échéanciers puis les deux jambes.
- `ois_swap_factory`, enregistrée sous `"ois_swap"`, valide les paramètres de l'API (exactement l'un de `maturity`
  ou `end_date`, `start_date` facultatif pour un swap déjà commencé ou à départ différé).

### 4.7 Les moteurs `core/engines/`

**`results.py`** — `PricingResult`, jamais un simple `float` : PV, PV par jambe (`LegResult`), tableau des flux
(`CashflowResult` : dates, τ, taux, montant signé, DF, PV), taux de par, annuité, PVBP et diagnostics (moteur,
durée, version, avertissements).

**`base.py`** — l'interface `PricingEngine` et `engine_for(type, méthode)`, qui choisit le moteur dans le registry.

**`discounting.py`** — `DiscountingSwapEngine` :
- Il exclut les flux payés à la date de valorisation ou avant (ADR-020).
- PV = somme signée des montants actualisés.
- Taux de par = PV de la jambe overnight / annuité.
- `par_rate()` est une version légère, sans tableau de flux, réutilisée par le bootstrap.
- S'il ne reste aucun coupon fixe, le taux de par vaut NaN et un avertissement est ajouté (l'API renvoie alors
  `null`).

---

## 5. La couche `services/` : l'orchestration

**`requests.py`** — les entrées du service sous forme de dataclasses figées : `SnapshotReference` (marché par
référence), `InlineMarket` (marché par valeur), `TradeInput`, `CalculationOptions`. Il n'y a **pas de Pydantic
ici**, car il est réservé à `api` et `sdk` (ADR-021).

**`mappers.py`** — trois rôles :
- traduire l'entrée en domaine (`snapshot_from_input`) ;
- calculer les **empreintes** SHA-256 sur une sérialisation canonique (clés triées). `market_fingerprint` exclut le
  nom du snapshot, si bien que les mêmes données passées par référence ou par valeur partagent le même cache
  (ADR-026) ;
- traduire le domaine en JSON (`curves_payload` avec une grille mensuelle DF / zéro / forward 1M, `pricing_payload`).

**`curve_cache.py`** — le protocole `CurveCache` et `LruCurveCache` : moins récemment utilisé évincé en premier, TTL
optionnel, thread-safe, compteurs de hits, misses et évictions. L'horloge est injectable, ce qui permet de tester
l'expiration sans attendre. Ce cache existe **par process**, et l'interface permet de le remplacer plus tard par
Redis.

**`pricer_service.py`** — `PricerService`, la façade appelée par l'API :
- calculs : `price` et `build_curves` ;
- métadonnées : `catalog` (généré depuis les registries), `readiness`, `version_info`, `list_snapshots`,
  `get_snapshot`.

Chaque calcul renvoie le résultat accompagné de `Diagnostics` (durées par étape, cache hit ou miss, avertissements,
empreintes) et émet un log `calculation_audit`, qui contient de quoi **rejouer** le calcul.

---

## 6. La couche `api/` : le service HTTP (FastAPI)

**`settings.py`** — `Settings` (pydantic-settings) lit les variables d'environnement : `LOG_LEVEL`, `LOG_FORMAT`,
`MARKET_DATA_DIR`, `CURVE_CACHE_SIZE`, `CURVE_CACHE_TTL_SECONDS`, `GIT_COMMIT`.

**`schemas.py`** — le **contrat HTTP** en modèles Pydantic : requêtes (`PriceRequest`, `CurvesBuildRequest`, marché
par référence ou par valeur, `OisSwapParams`), enveloppe générique `Envelope[T]`, `ErrorResponse` et tous les
résultats.
- Ce fichier ne dépend que de Pydantic, pour que le SDK puisse le réutiliser tel quel.
- Les champs inconnus sont refusés (`extra="forbid"`) : une faute de frappe ne passe jamais silencieusement.

**`errors.py`** — la correspondance entre exceptions et codes HTTP (404 `NOT_FOUND`, 422 `MARKET_DATA_INVALID`,
`INVALID_TRADE_DEFINITION`, `CALIBRATION_FAILED`, 400 `UNSUPPORTED_INSTRUMENT`, 500…). Le format d'erreur est
unique : `{request_id, error: {code, message, details}}`.

**`middleware.py`** — `RequestContextMiddleware`, un middleware ASGI « pur ». Il :
- lit ou génère le `request_id` et le renvoie dans l'en-tête de réponse ;
- émet le log `request_completed` (méthode, chemin, statut, durée) ;
- intercepte les exceptions imprévues : le client reçoit un 500 générique, et la trace complète part dans les logs
  avec le `request_id`.

**`routes.py`** — des routes fines : valider, convertir, appeler le service, emballer. Les paramètres d'un trade sont
validés par le schéma propre à son type (`TRADE_PARAMS_SCHEMAS`), schéma que `/v1/catalog` publie pour que l'UI
construise ses formulaires. Les routes de calcul sont **synchrones** : FastAPI les exécute dans un thread, donc le
bootstrap ne bloque pas la boucle d'événements.

**`main.py`** — `create_app()` : une factory qui configure les logs, crée le service (provider + cache), monte
routes, gestion d'erreurs et middleware. Les dépendances sont injectables, ce que les tests utilisent. La variable
`app` est ce que lance `uvicorn`.

---

## 7. Le SDK `sdk/` et le dashboard `ui/`

**`sdk/client.py`** — `PricerClient` (httpx) expose `price`, `build_curves`, `catalog`, `list_snapshots`,
`get_snapshot`, `health`, `ready` et `version`. Le constructeur `ois_swap(...)` décrit un trade. Chaque code d'erreur
de l'API devient une exception typée (`CalibrationFailedError`, `NotFoundError`…) qui porte le `request_id` à
transmettre au support. Une API injoignable lève `ApiUnavailableError`.

**`sdk/models.py`** — réexporte les schémas de l'API. L'UI importe ses modèles d'ici et jamais directement depuis
`api`.

**`ui/views.py`** — la logique d'affichage, testable car sans appel à Streamlit : figure Plotly (zéro et forward en
haut, DF en bas, piliers marqués), table de calibration, table des flux, résumé formaté.

**`ui/app.py`** — le script Streamlit :
- barre latérale : URL de l'API, choix du snapshot et de l'interpolation ;
- onglet *Market* : la courbe ;
- onglet *OIS swap* : le formulaire de pricing.

**L'UI ne calcule rien** : tout passe par le SDK.

---

## 8. Les données

- **`data/market/EUR_2026-10-19.json`** : le snapshot de référence, **synthétique** (ADR-023). Il contient un dépôt
  ON, 23 OIS de 1W à 30Y et environ 200 fixings €STR depuis le 02/01/2026, générés sur les jours ouvrés TARGET avec
  3 décimales comme la publication BCE.
- **`data/trades/ois_samples.json`** : 5 trades d'exemple (5Y payeur, 1Y receveur, 30Y, un swap déjà commencé, un
  forward 10Y).
- **`tests/fixtures/golden_EUR_2026-10-19.json`** : les résultats de référence figés (*golden file*), qui servent de
  test de non-régression. On le régénère volontairement avec `UPDATE_GOLDEN=1 uv run pytest tests/integration`.

---

## 9. La stratégie de tests (la pyramide)

| Niveau | Fichiers | Ce qu'il prouve |
|---|---|---|
| **Unitaires** | `test_conventions`, `test_schedule`, `test_market_data`, `test_curves_and_solvers`, `test_bootstrap`, `test_ois_swap`, `test_registry`, `test_services` | Chaque brique isolée, cas limites et erreurs compris |
| **Propriétés** (hypothesis) | `test_properties.py` | Sur des centaines d'entrées aléatoires : DF(0) = 1, DF décroissant, cohérence forward/DF, **PV nul au taux de par**, symétrie payeur/receveur, bootstrap correct sous chocs |
| **Validation QuantLib** | `tests/validation/` | Égalité avec un oracle indépendant |
| **API / SDK / UI** | `tests/api/` | Contrat HTTP, erreurs, `request_id` jusque dans les logs du cœur, cache, SDK typé, dashboard exécuté pour de vrai (`AppTest`) |
| **Intégration** | `test_pipeline.py` | SDK -> HTTP -> cœur donne **exactement** les mêmes nombres que le cœur appelé directement, plus le *golden file* |

Le dossier de validation :
- **`tolerances.py`** : le **seul** endroit où sont définies les tolérances (ADR-014).
- **`quantlib_oracle.py`** : la traduction de nos conventions en QuantLib. Le gestionnaire de contexte
  `evaluation_date` neutralise la date globale de QuantLib (ADR-009).
- **`market_grid.py`** : la grille de 9 marchés (3 dates de valorisation, dont une fin de mois et une veille de
  Pâques, × 3 chocs parallèles).
- **`test_conventions_vs_quantlib.py`** : calendrier sur 66 ans, day counts, échéanciers.
- **`test_pricing_vs_quantlib.py`** : DF entre les piliers et en extrapolation, 1 404 swaps spot, swaps déjà
  commencés et forward.

Les fichiers d'appui : `tests/unit/helpers.py` (courbes plates de test), les `conftest.py` (fixtures partagées) et
les `__init__.py` (pour que les modules de test s'importent entre eux).

Commandes utiles :

```
uv run pytest                    # tout
uv run pytest -m validation      # validation QuantLib uniquement
uv run pytest tests/unit         # ce que rejoue le hook pre-commit
```

---

## 10. L'outillage et la documentation

`pyproject.toml` :
- `pythonpath` pour les tests ;
- règles ruff D105/D107 ignorées (convention Google) ;
- override mypy pour plotly ;
- filtre d'un avertissement Starlette ;
- **contrat import-linter `ui` scindé** en deux : l'UI n'atteint jamais `services`/`core`, même indirectement, et
  n'importe jamais `api` directement (ADR-021).

Documentation :
- **`DECISIONS.md`** : huit ADR, de 019 à 026. Conventions €STR, politique des fixings, frontières entre couches,
  choix numériques du bootstrap, données synthétiques, bilan de validation, détails du contrat HTTP, clé du cache.
- **`API.md`** : paramètres OIS, exemple de requête, enveloppe, SDK.
- **`ROADMAP.md`**, **`CLAUDE.md`** et **`README.md`** : statut et démarrage rapide.

---

## 11. Les incidents, et ce qu'ils enseignent

1. **La règle de fin de mois des échéanciers** : la première version déplaçait la date de départ, pas QuantLib.
   *Leçon : les détails de convention ne se devinent pas, ils se vérifient date par date contre un oracle.*
2. **L'extrapolation `linear_zero`** : linéaire au départ, à forward constant chez QuantLib. L'écart n'apparaissait
   qu'au-delà de 30 ans. *Leçon : tester aussi hors des piliers.*
3. **Le contrat import-linter de la phase 0** contredisait l'architecture (SDK qui réutilise les schémas de l'API).
   Il a été scindé et consigné dans un ADR, sans l'affaiblir. *Leçon : une règle trop large finit par être
   contournée ; mieux vaut l'affiner en la documentant.*
4. **Le hook pre-commit** met de côté les fichiers modifiés non indexés : un commit peut donc être validé sur un état
   différent du répertoire de travail. Pour committer couche par couche, il faut isoler le reste du travail
   (`git stash push --keep-index --include-untracked`) afin que chaque commit soit vérifié sur son seul contenu.
5. **La CI Linux** : le *golden file* a échoué parce que le PV du 30Y (-1 034 €) est la différence de deux jambes de
   2,7 M€. Les arrondis d'`exp`/`log` diffèrent d'un système à l'autre (2e-9 €). *Leçon : un PV proche de zéro se
   compare au notionnel, jamais en relatif (ADR-024).*

---

## 12. Les patterns à savoir nommer en entretien

- **Strategy** : interpolateurs, solveurs, moteurs.
- **Factory + Registry** : principe Open/Closed.
- **Composition** : jambes et cashflows.
- **Interfaces** : `ABC` et `Protocol` (dont `CurveCache`).
- **Objets valeur immuables** : dataclasses figées.
- **DTO + Mapper** : Pydantic <-> dataclasses <-> domaine.
- **Inversion de dépendance** : les couches ne pointent que vers le bas.
- **Observabilité** : `request_id` de bout en bout, logs JSON, empreintes pour rejouer un calcul.
- **Oracle de test** : QuantLib.

## 13. Ce qui n'est pas encore fait

- Une seule courbe : la projection Euribor et le multi-courbes arrivent en phase 2.
- Pas encore de risque : DV01, key-rate et convexité arrivent en phase 2.
- Pas d'USD : il viendra en phase 3, par simple configuration.
- Ni Docker, ni ring buffer de logs, ni batch, ni métriques : c'est la phase 4.
- La couverture affiche 0 % pour `ui/app.py` alors que le test Streamlit l'exécute bien.
- La CI émet encore un avertissement sur les versions d'actions GitHub (Node.js 20 déprécié).
