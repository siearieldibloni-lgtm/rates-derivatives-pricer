# API

Contrat HTTP, correspondance des erreurs, observabilité et spécification des logs.

## 1. Principes

- **Sans état** : chaque requête embarque (ou référence) ses données de marché et son trade ; pas de session.
- **DTO séparés du domaine** : les schémas Pydantic (api) sont distincts des dataclasses de `core` ; les mappers
  vivent dans `services/`.
- **Versioning dans l'URL** : `/v1/...`. Un changement incompatible crée `/v2/`, jamais une modification silencieuse de `/v1/`.
- **Erreurs structurées** : un seul format partout, avec un code machine stable.
- **Traçabilité** : chaque requête reçoit un `request_id` (généré, ou repris de `X-Request-ID`), renvoyé dans la
  réponse et présent dans tous les logs.
- **L'UI ne calcule rien** : Streamlit appelle l'API via le SDK, jamais `core`.

## 2. Endpoints

| Méthode | Route | Rôle | Phase |
|---|---|---|---|
| GET | `/health` | Liveness | 1 |
| GET | `/ready` | Readiness : conventions et registries chargés | 1 |
| GET | `/v1/version` | Version API/cœur, commit, versions NumPy/Python | 1 |
| GET | `/v1/catalog` | Types d'instruments, interpolateurs, mesures de risque, devises, **générés depuis les registries** | 1 |
| GET | `/v1/market-snapshots` | Liste des snapshots (fichiers de `data/market/`) | 1 |
| GET | `/v1/market-snapshots/{name}` | Contenu d'un snapshot | 1 |
| POST | `/v1/curves/build` | Bootstrap : snapshot -> courbes (nœuds, DF, zéro, forwards, diagnostics de convergence) | 1 |
| POST | `/v1/price` | Pricing d'un trade | 1 |
| POST | `/v1/risk` | Mesures de risque sur un trade | 2 |
| POST | `/v1/risk/scenarios` | Revalorisation sous scénarios nommés | 3 |
| POST | `/v1/batch/price` | Pricing de plusieurs trades, résultat partiel par trade | 4 |
| GET | `/v1/cache/stats` | Statistiques du cache de courbes | 4 |
| DELETE | `/v1/cache` | Vidage du cache de courbes | 4 |
| GET | `/v1/logs/{request_id}` | Chronologie d'événements issue du ring buffer en mémoire | 4 |
| GET | `/metrics` | Métriques Prometheus | 4 |

## 3. Forme des requêtes

```
{
  "market":  { "snapshot": "EUR_2026-10-19" }                       # par référence
          or { "inline": { "valuation_date", "quotes": [...], "fixings": [...] } },   # par valeur
  "trade":   { "type": "ois_swap", "params": { ... } },
  "options": { "interpolation": "log_linear_df", "currency": "EUR" }
}
```

- Référence **et** valeur dès la v1 : la référence pour le confort du dashboard, la valeur pour la reproductibilité et le débogage.
- `type` est un discriminant (union discriminée Pydantic) ; `/v1/catalog` permet à l'UI de construire les formulaires dynamiquement.
- `/v1/risk` ajoute `"measures": ["parallel_dv01", "key_rate_dv01", ...]` et des options de bump (taille en bp, mode).
- Formats : dates ISO 8601 ; taux en **décimal** ; cotations avec `"unit": "percent" | "decimal" | "bp"` ; PV en devise du trade.

## 4. Enveloppe de réponse

```
{
  "request_id": "...",
  "result": { ... },
  "diagnostics": {
    "timings_ms": { "curve_build": 0.0, "pricing": 0.0 },
    "curve_cache": "hit" | "miss",
    "versions": { "api": "...", "core": "..." },
    "warnings": []
  }
}
```

Le bloc `diagnostics` est permanent : il alimente l'onglet Diagnostic et les investigations de support.

## 5. Erreurs

```
{ "request_id": "...", "error": { "code": "CALIBRATION_FAILED", "message": "...", "details": { "pillar": "5Y", "residual": 1.2e-4 } } }
```

| Source | HTTP | Code |
|---|---|---|
| Validation Pydantic (JSON mal formé, champ manquant) | 422 | `VALIDATION_ERROR` |
| `MarketDataError` | 422 | `MARKET_DATA_INVALID` |
| `ConventionError`, `ScheduleError` | 422 | `INVALID_TRADE_DEFINITION` |
| `UnsupportedInstrumentError` | 400 | `UNSUPPORTED_INSTRUMENT` |
| `CalibrationError` | 422 | `CALIBRATION_FAILED` (pilier et résidu) |
| Snapshot ou ressource introuvable | 404 | `NOT_FOUND` |
| `PricingError`, `RiskComputationError` | 500 | `COMPUTATION_FAILED` |
| Exception inattendue | 500 | `INTERNAL_ERROR` (message générique au client, trace complète dans les logs) |

**Ne jamais exposer une stack trace au client** ; la journaliser avec le `request_id`.

## 6. Cache de courbes

- Clé : hash stable (SHA-256 d'une sérialisation canonique) de `snapshot + options d'interpolation + version du cœur`.
- LRU en mémoire avec taille maximale configurable, TTL optionnel ; interface `CurveCache` pour pouvoir remplacer
  par Redis plus tard.
- Observable via `diagnostics.curve_cache`, `/v1/cache/stats`, `DELETE /v1/cache`.
- Piège à consigner dans DECISIONS.md : le cache est **par process** ; avec plusieurs workers Uvicorn, chacun a le sien.

## 7. Spécification des logs

Objectif : répondre, à partir des seuls logs, à « pourquoi ce PV est-il différent de celui d'hier ? ».
Trois niveaux de logs.

### 7.1 Logs techniques

JSON, un événement par ligne, champs stables (filtrables avec `jq`, chargeables dans ELK/Loki plus tard).

| Champ | Contenu |
|---|---|
| `timestamp` | UTC, ISO 8601 |
| `level` | DEBUG / INFO / WARNING / ERROR |
| `logger` | Module émetteur (`core.market.bootstrap`, `api.routes.price`) |
| `event` | Nom court et stable (`curve_build_started`, `pricing_completed`) |
| `request_id` | Identifiant de corrélation de bout en bout |
| `duration_ms` | Sur les événements de fin d'étape |
| champs métier | `trade_type`, `currency`, `snapshot_name`, `cache`... |

Le `request_id` est injecté via `contextvars` dans tous les logs d'une requête, y compris ceux émis depuis `core`,
qui n'utilise que le module standard `logging` (le cœur émet, l'API configure).

Implémentation (phase 0) : `api/logging_config.py` fournit `configure_logging(level, log_format)` à appeler au
démarrage, et `request_context(request_id)` à utiliser dans un middleware (id repris de `X-Request-ID` ou généré).
Les champs métier se passent avec `extra={...}` ; le message du log est le nom stable de l'événement.

### 7.2 Logs de calcul

- **Bootstrap** : par pilier, instrument, valeur cible, itérations du solveur, résidu final, DF trouvé (DEBUG) ;
  résumé en INFO (« 18 piliers, 4 itérations max, résidu max 1e-13 »).
- **Pricing** : PV par jambe, nombre de flux, par rate, annuité.
- **Risque** : pour chaque bump, cible, taille, PV avant/après.
- **Empreinte d'entrée** : hash du snapshot (le même que la clé du cache) et du trade. Mêmes hashes d'entrée et même
  version du cœur doivent donner exactement le même résultat. Un PV différent avec un hash identique révèle un bug ou
  un changement de version ; un hash différent révèle un changement de données.

### 7.3 Logs d'audit

Un événement `calculation_audit` (INFO) par requête de calcul : `request_id`, hashes des entrées, versions de l'API et
du cœur, options, PV et métriques clés, durée totale. Cet enregistrement permet de **rejouer** le calcul.

### 7.4 Règles

| Règle | Raison |
|---|---|
| Niveaux disciplinés : DEBUG détail numérique, INFO jalons, WARNING situation dégradée mais traitée (convergence lente), ERROR échec de requête | Du signal, pas du bruit |
| Pas de données sensibles ni de payload complet en INFO ; détail complet seulement en DEBUG | Les trades réels peuvent être confidentiels |
| Jamais de `print` | Tout passe par `logging` |
| `request_id` renvoyé dans chaque réponse | L'utilisateur le communique au support, qui retrouve la chaîne |
| Les erreurs loguent la stack trace complète avec le `request_id` | Le client ne reçoit qu'un message générique |
| Configuration par environnement : `LOG_LEVEL`, `LOG_FORMAT` | JSON en conteneur, texte en local |
| Écriture sur stdout en Docker | L'infrastructure collecte |

### 7.5 Visibilité (phase 4)

- L'onglet Diagnostic affiche les `diagnostics` de chaque réponse.
- `GET /v1/logs/{request_id}` renvoie la chronologie d'événements depuis un **ring buffer** borné en mémoire.
- La structure de logs JSON existe dès la **phase 1** ; le ring buffer arrive en phase 4.

## 8. SDK

Client typé basé sur `httpx` :

```
client = PricerClient(base_url="http://localhost:8000")
result = client.price(market="EUR_2026-10-19", trade=my_swap)
risk   = client.risk(market=..., trade=..., measures=["key_rate_dv01"])
```

- Modèles de réponse typés (réutilisent les schémas Pydantic de l'API).
- Exceptions client mappées sur les codes d'erreur de l'API (`CalibrationFailedError`, ...).
- Timeouts et retries configurables ; version du SDK vérifiée contre `/v1/version`.
- Le SDK sert aussi de test d'intégration vivant : Streamlit l'utilise, donc toute rupture de contrat se voit immédiatement.

## 9. Hors v1 (consigné, pas oublié)

Authentification, limitation de débit, calculs asynchrones avec file de tâches (futur Monte Carlo), WebSocket,
persistance des résultats. Chacun devient une entrée de DECISIONS.md avec la raison du report.
