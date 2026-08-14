# Constitution — Basic Memory

## Projet

**Nom** : basic-memory
**Version** : 0.17.4 (dynamic via git tags, `uv-dynamic-versioning`)
**Description** : Gestionnaire de connaissances local-first combinant Zettelkasten et knowledge graph, exposé via le protocole MCP pour les LLMs.
**Licence** : AGPL-3.0-or-later
**Auteur** : Basic Machines (hello@basic-machines.co)
**GitHub** : https://github.com/basicmachines-co/basic-memory

---

## Stack

| Composant | Version |
|-----------|---------|
| Python | >= 3.12 |
| Package manager | `uv` |
| Build backend | `hatchling` + `uv-dynamic-versioning` |
| Task runner | `just` (justfile) |

### Dépendances clés (runtime)

| Lib | Rôle |
|-----|------|
| `fastmcp==2.12.3` | Serveur MCP (version pinn\u00e9e — 2.14.x casse la visibilité des tools) |
| `fastapi[standard]` | API REST HTTP (mode cloud/API) |
| `sqlalchemy>=2.0` | ORM async |
| `aiosqlite` | Backend SQLite async |
| `asyncpg` / `psycopg` | Backend PostgreSQL |
| `alembic` | Migrations DB |
| `pydantic>=2.10` + `pydantic-settings` | Validation et configuration |
| `typer` | CLI |
| `loguru` | Logging |
| `markdown-it-py` | Parsing Markdown |
| `python-frontmatter` | Parsing frontmatter YAML |
| `watchfiles` | File watching (sync temps réel) |
| `pybars3` | Templates Handlebars |
| `mdformat` + plugins | Formatage Markdown |

### Dépendances dev

`pytest`, `pytest-asyncio`, `pytest-mock`, `pytest-cov`, `pytest-xdist`, `ruff`, `freezegun`, `testcontainers[postgres]`

---

## Architecture

### Structure des dossiers

```
basic-memory/
├── src/
│   └── basic_memory/          # Package principal
│       ├── __init__.py        # Version (__version__, __api_version__)
│       ├── config.py          # Configuration (BasicMemoryConfig, ProjectConfig)
│       ├── runtime.py         # RuntimeMode enum (LOCAL, CLOUD, TEST)
│       ├── db.py              # Engine SQLite/Postgres, sessions
│       ├── deps.py            # Dépendances FastAPI/MCP
│       ├── utils.py           # Utilitaires généraux
│       ├── file_utils.py      # Utilitaires fichiers
│       ├── ignore_utils.py    # Gestion .bmignore
│       ├── project_resolver.py
│       ├── telemetry.py       # OpenPanel (opt-out)
│       │
│       ├── models/            # SQLAlchemy ORM
│       │   ├── base.py
│       │   ├── knowledge.py   # Entity, Observation, Relation
│       │   ├── project.py
│       │   └── search.py
│       │
│       ├── repository/        # Data access layer
│       │   ├── entity_repository.py
│       │   ├── observation_repository.py
│       │   ├── relation_repository.py
│       │   ├── search_repository.py       # SQLite FTS
│       │   ├── sqlite_search_repository.py
│       │   └── postgres_search_repository.py
│       │
│       ├── services/          # Business logic
│       │   ├── entity_service.py
│       │   ├── search_service.py
│       │   ├── context_service.py
│       │   ├── file_service.py
│       │   ├── link_resolver.py
│       │   ├── project_service.py
│       │   ├── directory_service.py
│       │   └── initialization.py  # Startup orchestration (exclu coverage)
│       │
│       ├── mcp/               # Serveur MCP (FastMCP)
│       │   ├── server.py      # Entry point MCP (lifespan, McpContainer)
│       │   ├── container.py   # DI container MCP
│       │   ├── project_context.py
│       │   ├── async_client.py
│       │   ├── tools/         # MCP tools (1 fichier = 1 tool)
│       │   │   ├── write_note.py
│       │   │   ├── read_note.py
│       │   │   ├── edit_note.py
│       │   │   ├── delete_note.py
│       │   │   ├── search.py
│       │   │   ├── build_context.py
│       │   │   ├── list_directory.py
│       │   │   ├── recent_activity.py
│       │   │   ├── move_note.py
│       │   │   ├── read_content.py
│       │   │   ├── view_note.py
│       │   │   ├── canvas.py
│       │   │   ├── reindex.py
│       │   │   ├── chatgpt_tools.py
│       │   │   └── project_management.py
│       │   ├── resources/     # MCP resources
│       │   └── prompts/       # MCP prompts
│       │
│       ├── api/               # FastAPI (mode HTTP/cloud)
│       │   ├── app.py         # App FastAPI + lifespan
│       │   ├── container.py   # DI container API
│       │   ├── routers/       # v0 routes
│       │   └── v2/            # v2 routes
│       │
│       ├── cli/               # CLI Typer
│       │   ├── main.py        # Entry point CLI
│       │   ├── app.py
│       │   ├── auth.py
│       │   └── commands/      # Sous-commandes (mcp, project, cloud, db, import*, tool, status, telemetry)
│       │
│       ├── markdown/          # Parsing Markdown
│       │   ├── entity_parser.py
│       │   ├── markdown_processor.py
│       │   ├── schemas.py
│       │   └── plugins.py
│       │
│       ├── dataview/          # Moteur Dataview (Obsidian-compatible)
│       │   ├── ast.py
│       │   ├── lexer.py
│       │   ├── parser.py
│       │   ├── integration.py
│       │   ├── detector.py
│       │   └── executor/
│       │
│       ├── sync/              # File watching et synchronisation
│       │   ├── coordinator.py      # SyncCoordinator
│       │   ├── sync_service.py     # Logique sync (exclu coverage)
│       │   ├── watch_service.py    # watchfiles (exclu coverage)
│       │   ├── background_sync.py  # Cloud background sync (exclu coverage)
│       │   └── dataview_refresh_manager.py
│       │
│       ├── importers/         # Import depuis ChatGPT, Claude, Memory JSON
│       ├── schemas/           # Schémas Pydantic (API/MCP)
│       ├── templates/         # Templates Handlebars
│       └── alembic/           # Migrations DB
│
├── tests/                     # Tests unitaires (pytest)
├── test-int/                  # Tests d'intégration (pytest)
├── specs/                     # Spécifications techniques
├── docs/                      # Documentation
├── scripts/                   # Scripts utilitaires
├── justfile                   # Task runner
├── pyproject.toml             # Config projet Python
├── docker-compose.yml         # Stack Docker (production)
├── docker-compose-postgres.yml # Stack Docker (Postgres dev/test)
└── Dockerfile
```

### Patterns architecturaux

- **DI Container** : chaque entry point (MCP, API) crée son propre container (`McpContainer`, `ApiContainer`) — composition root unique.
- **RuntimeMode** : `LOCAL` (standalone), `CLOUD` (sync distant), `TEST` — résolu au démarrage.
- **Dual backend** : SQLite (défaut, FTS5) et PostgreSQL (cloud/entreprise). Backend piloté par `BASIC_MEMORY_DATABASE_BACKEND`.
- **Repository pattern** : services → repositories → SQLAlchemy async sessions.
- **File-first** : les notes Markdown sont source de vérité. La DB est un index dérivé, synchronisé via `SyncCoordinator`.
- **MCP tools** : 1 fichier Python = 1 tool MCP. Enregistrés via FastMCP.
- **API versionnée** : v0 (legacy) et v2 dans `api/routers/` et `api/v2/routers/`.

### Entry points

| Commande | Module |
|----------|--------|
| `basic-memory` / `bm` | `basic_memory.cli.main:app` |
| Serveur MCP (stdio) | `bm mcp` → `basic_memory.mcp.server` |
| Serveur API HTTP | `bm api` → `basic_memory.api.app` |

---

## Conventions

### Linting / Formatage

- **Ruff** : `line-length=100`, `target-version=py312`
  - `just lint` → `ruff check --fix --unsafe-fixes src tests test-int`
  - `just format` → `ruff format .`

### Type checking

- **Pyright** : `pythonVersion=3.12`, `include=["src/"]`, `reportMissingImports=error`
  - `just typecheck` → `uv run pyright`

### Type hints

- Type hints obligatoires sur les fonctions publiques (enforced par pyright).
- Modèles Pydantic pour les schémas d'entrée/sortie API et MCP.
- SQLAlchemy ORM avec `Mapped` et `mapped_column` (style 2.0).

### Tests

- **Framework** : `pytest` + `pytest-asyncio` (`asyncio_mode = strict`)
- **Couverture** : `pytest-cov` avec rapport `term-missing`
- **Tests unitaires** : `tests/` — isolation via mocks (`pytest-mock`, `freezegun`)
- **Tests d'intégration** : `test-int/` — contre SQLite ou Postgres (testcontainers)
- **Markers** : `benchmark`, `slow`, `postgres`, `windows`

### Configuration

- Via `BasicMemoryConfig` (Pydantic Settings) — variables d'environnement préfixées `BASIC_MEMORY_`.
- Fichier de config local : `~/.basic-memory/config.json` (ou `$BASIC_MEMORY_HOME/.basic-memory/config.json`).

### Git / Versioning

- **Workflow de branche** : toute tâche part d'une **branche feature** créée depuis `main` (format `type/description-courte` — `feat/...`, `fix/...`, `docs/...`). Une fois l'implémentation terminée **ET** le build validé (**tests verts** + **`ruff` clean**), merge `--no-ff` dans `main` local, puis **suppression de la branche locale**.
- **Commits signés DCO** : `git commit -s` sur **tous** les commits — le fork vise des **PR upstream** potentielles (cf. `CONTRIBUTING.md`).
- **Push fork systématique** : après le merge dans `main` local et le build validé, `git push fork main` est **systématique et sans confirmation** — c'est la **dernière étape normale** de tout chantier. Justification : éviter que du travail validé reste **local-only** (perte si poste défaillant, désynchronisation entre postes). Remote `fork` = `git@github.com:DonaldoDes/basic-memory.git`.
- **Exception upstream** : le push vers `upstream`/`origin` (**basicmachines-co**) et l'ouverture de **PR** restent **manuels et explicites** — **jamais automatiques**. Une contribution upstream se décide **au cas par cas** (DCO sign-off déjà en place).

---

## Commandes

```bash
# Installation
uv sync
just install         # uv pip install -e ".[dev]" + uv sync

# Tests
just test            # SQLite + Postgres (complet)
just test-sqlite     # Unit + intégration SQLite
just test-unit-sqlite
just test-int-sqlite
just test-postgres   # Unit + intégration Postgres (testcontainers)

# Qualité
just lint            # ruff check --fix
just format          # ruff format
just typecheck       # pyright
just check           # lint + format + typecheck + test

# Coverage
just coverage        # HTML report dans htmlcov/

# Dev
bm mcp               # Lancer le serveur MCP (stdio)
bm status            # Statut des projets
bm project list      # Lister les projets configurés

# DB
just migration "description"   # Générer une migration Alembic
just postgres-reset             # Réinitialiser la DB Postgres de test
just postgres-migrate           # Appliquer les migrations sur Postgres

# Release
just release v0.18.0   # Release stable (tag + push → CI publie sur PyPI)
just beta v0.18.0b1    # Beta release
```

---

## Points d'attention

1. **fastmcp pinn\u00e9 `==2.12.3`** : Les versions 2.14.x cassent la visibilité des MCP tools (issue #463). Ne pas upgrader sans validation.

2. **Dual backend SQLite/Postgres** : Les repositories de recherche ont deux implémentations distinctes (`sqlite_search_repository.py` / `postgres_search_repository.py`). Toute modification de la couche search doit couvrir les deux.

3. **asyncio_mode = strict** : Tous les tests async doivent être décorés `@pytest.mark.asyncio`. Le mode strict lève une erreur si ce décorateur est absent.

4. **Modules exclus de la coverage** : watch_service, background_sync, sync_service, cli, db, initialization, migration_service, telemetry, external auth providers — testés via intégration ou exclus par nature (I/O filesystem, startup, externe).

5. **Timeout Postgres test-int** : La combinaison FastMCP Client + asyncpg cause un hang au teardown (issue fastmcp #1311). `test-int-postgres` utilise `gtimeout`/`timeout` avec 600s et `SIGKILL`.

6. **File-first sync** : La DB est un index dérivé. En cas de désynchronisation, `bm reindex` reconstruit l'index depuis les fichiers. Ne jamais modifier la DB directement.

7. **Dataview** : Implémentation custom compatible Obsidian Dataview. `dataviewjs` non supporté (ignoré silencieusement). Certaines fonctions avancées (link(), regexreplace()) retournent des colonnes vides.

8. **Telemetry** : OpenPanel intégré avec opt-out (style Homebrew). Configurable via `bm telemetry disable`.

9. **API versionnée** : v0 maintenue pour compatibilité. v2 est la version cible. Les deux sont montées dans `app.py`.

10. **Multi-project** : Basic Memory supporte plusieurs projets (mapping nom → path dans `config.json`). Le `project_resolver.py` centralise la résolution du projet courant.

---

## Zones sensibles

Cette section désigne les fichiers et patterns qui déclenchent automatiquement le niveau **Strict** du workflow et le chargement de la skill `adversarial-testing`. Section générée par audit code le 2026-06-05 (HEAD `8ff1613d`) — à réviser quand le code évolue.

Contexte double : ce repo (1) fait tourner le serveur MCP du **vault PKM personnel** (données privées, sous git auto-sync + Obsidian Sync) et (2) alimente des **PRs upstream** (basicmachines-co). Toute régression sur ces zones touche des données réelles ET du code publié.

### Modèle d'identité et d'authentification (lecture de grille MCP-SEC)

Ce serveur est **mono-utilisateur local par défaut** (`RuntimeMode.LOCAL`) : le process MCP tourne en stdio sur le poste de l'utilisateur, sans notion de session HTTP multi-client ni d'utilisateurs distincts à isoler les uns des autres. Conséquence directe sur la grille d'invariants MCP-SEC de `.claude/skills/mcp-server/SKILL.md` (vault) :

- **Pas de paramètre `user_id`/`owner_id`/`tenant_id` en input MCP** — vérifié (`grep` sur `mcp/tools/`, `mcp/server.py`, `mcp/async_client.py`) : aucun tool n'accepte un identifiant d'utilisateur en paramètre. L'identité de l'appelant, en mode local, est simplement « le process qui a ouvert ce stdio » — il n'y a rien à dériver côté serveur au-delà de ça.
- **Isolation multi-tenant non applicable en mode LOCAL** — l'invariant « filtre d'identité obligatoire sur toute query de données utilisateur » se traduit ici par l'invariant **projet** existant (§ « Isolation projets / workspaces » ci-dessous), pas par un filtre utilisateur : un seul utilisateur humain, N projets/workspaces potentiellement montés dans la même config (`config.json`), et c'est le **projet résolu** (jamais un tenant) qui doit border chaque opération.
- **Mode CLOUD (`cli/auth.py`, `cloud_api_key`)** est le seul endroit où une notion d'identité distante existe (Bearer token / OAuth vers le service cloud Basic Machines) — cf. § « Credentials cloud / OAuth » ci-dessous. Même dans ce mode, l'auth protège l'accès **réseau au service cloud**, pas un cloisonnement inter-utilisateurs *dans* ce serveur MCP : chaque instance locale reste un client mono-utilisateur de son propre espace cloud.
- **Ce que ça ne dispense PAS** : validation d'input avant logique métier (zones path traversal / SQL / markdown ci-dessous), et absence de fuite d'énumération (404 vs 403) — ces invariants restent pleinement pertinents indépendamment du modèle mono-utilisateur, car l'entrée reste non fiable (LLM, fichiers vault, imports) même sans multi-tenant à protéger.

### Path traversal / résolution filesystem

- **Fichiers** : `src/basic_memory/utils.py` (`validate_project_path`, l.706 — frontière unique projet/filesystem), `src/basic_memory/file_utils.py` (`sanitize_for_directory`, l.494 — rejet `..` post-strip, fix BUG-004 ; whitelist Option B qui raise au lieu de stripper silencieusement, fix BUG-001), `src/basic_memory/services/file_service.py` (`write_file`, l.185-236 — défense last-mile `is_relative_to(base_path)` ; `move_file`, `delete_file`), call sites MCP : `mcp/tools/read_content.py` (l.242-251), `mcp/tools/move_note.py` (l.512-516 et l.728-731), `mcp/tools/write_note.py`.
- **Risque** : un identifier/path/folder forgé par le LLM (entrée non fiable par construction) qui échappe au project root = lecture/écriture/suppression arbitraire sur le poste de l'utilisateur.
- **Invariants** :
  - Tout path issu d'une entrée MCP/API passe par `validate_project_path` AVANT toute I/O.
  - La défense last-mile de `FileService.write_file` (resolve + `is_relative_to`) ne doit jamais être retirée, même si la validation amont semble suffisante (defense-in-depth, BUG-004).
  - `sanitize_for_directory` : le check `..` reste APRÈS le strip des caractères réservés (sinon `<>../etc` repasse) et couvre les deux séparateurs (`/` et `\`).
  - Le comportement Option B (ValueError sur caractère hors whitelist, pas de strip silencieux) est un choix du fork — ne pas le régresser lors des merges upstream.

### Handlers MCP write + knowledge router v2 (surface bulk_edit)

- **Fichiers** : `src/basic_memory/mcp/tools/{write_note,edit_note,delete_note,move_note}.py` ; `src/basic_memory/api/v2/routers/knowledge_router.py` (create/update/edit/delete entity l.365-604, `move_directory` l.687, `delete_directory` l.755 — destructif en masse) ; `src/basic_memory/services/entity_service.py` (`edit_entity` l.1110, `apply_edit_operation` l.1207 — fonction pure).
- **Risque** : ces handlers reçoivent du contenu et des identifiers générés par un LLM. Un bug = corruption ou suppression en masse de notes du vault. C'est la surface exacte de la feature `bulk_edit_notes` à venir (spec : invariants I-1..I-7).
- **Invariants** :
  - Invariants I-1..I-7 de la spec Bulk Edit Notes — notamment I-1 (traversal dans un identifier = item `failed SECURITY`, jamais d'I/O), I-4 (pas d'auto-création en bulk), I-7 (chemin single-note `edit_entity_by_id` / `edit_entity_with_content` / MCP `edit_note` byte-identique).
  - `apply_edit_operation` reste une fonction **pure** (pas d'I/O) — le dry-run `validate_first` en dépend.
  - Toute nouvelle opération destructive de masse (pattern `delete_directory`) exige une validation de path par item, pas seulement sur le dossier racine.
  - `_detect_cross_project_move_attempt` (`mcp/tools/move_note.py:17`) reste actif sur les moves.

### Scheduler de tasks background

- **Fichiers** : `src/basic_memory/deps/services.py` (l.478-527 : `LocalTaskScheduler.schedule` + `get_task_scheduler` — registre fermé : `sync_entity_vectors`, `sync_project`, `reindex_project`).
- **Risque** : une task non enregistrée droppée silencieusement = travail background perdu (index vectoriel désynchronisé) ; une task mal payloadée tourne détachée du request cycle. `bulk_edit_notes` doit y ajouter `sync_entity_vectors_batch` (édition additive).
- **Invariants** :
  - Le fail-fast `ValueError` sur task inconnue est préservé (pas de fallback silencieux).
  - Le no-op en env test (`BASIC_MEMORY_ENV=test`) est préservé — les tests exercent les codepaths sync directement.
  - Ajouts au registre = additifs uniquement ; ne jamais modifier le payload des tasks existantes.

### Construction de requêtes SQL/FTS5

- **Fichiers** : `src/basic_memory/repository/sqlite_search_repository.py` (`_prepare_boolean_query` l.118, `_prepare_single_term` l.238 — escaping FTS5 par doublement des `"` ; f-strings sur `text()` l.534-589 qui n'interpolent QUE des noms de placeholders, jamais des valeurs), `postgres_search_repository.py` (équivalent Postgres), `repository/search_repository_base.py`.
- **Risque** : une query utilisateur atteignant l'opérateur `MATCH` sans escaping = erreur de syntaxe FTS5 (DoS du search) ou sémantique de requête détournée. Une f-string interpolant une **valeur** au lieu d'un placeholder = injection SQL.
- **Invariants** :
  - Jamais de valeur issue d'une entrée utilisateur interpolée dans un `text(f"...")` — uniquement des noms de placeholders générés (`:rowid_0`, …), valeurs passées en params.
  - L'escaping FTS5 (`"` → `""` + quoting des termes) reste systématique dans les `_prepare_*`.
  - Toute modification de la couche search couvre les **deux** backends (SQLite + Postgres) — cf. Points d'attention §2.

### Isolation projets / workspaces

- **Fichiers** : `src/basic_memory/project_resolver.py` (`ProjectResolver.resolve` / `require_project`), `src/basic_memory/workspace_context.py` (contexte permalink workspace, headers), `src/basic_memory/mcp/project_context.py` (résolution workspace/projet côté MCP, `WorkspaceProjectIndex`).
- **Risque** : le vault PKM privé est un des projets servis. Un bleed cross-projet (résolution d'identifier qui matche dans un autre projet, permalink workspace-qualified mal routé) expose ou modifie des données hors du périmètre demandé.
- **Invariants** :
  - Une opération est **single-project** : le projet résolu (param explicite ou contexte) fait foi, jamais de fallback silencieux vers un autre projet.
  - Les permalinks workspace-qualified (`workspace/project/...`) ne contournent pas la résolution — validation via `workspace_context.validate_workspace_permalink_context_values`.
  - Côté bulk : I-6 (identifiers `memory://` et workspace-qualified rejetés au schéma).
  - Pas de substitution silencieuse d'une note non demandée — y compris **intra-projet, cross-note** (voir la zone « Résolution de contexte » ci-dessous). L'invariant « single-project » couvre le bleed cross-projet ; il ne dispense PAS de la fidélité de résolution à l'intérieur d'un même projet (BUG-022).

### Résolution de contexte (build_context / fidélité de résolution)

- **Fichiers** : `src/basic_memory/services/context_service.py` (branche URL exacte de `build_context`, ~l.179-192 — bascule lookup exact `search_repository.search(permalink=...)` → `link_resolver.resolve_link(...)`), `src/basic_memory/services/link_resolver.py` (`_resolve_in_project`, étape 5 « fall back to search », ~l.379-397 — retourne `results[0]` du FTS sans seuil de score).
- **Risque** : les URLs `memory://` sont des entrées non fiables (générées par un LLM, mal orthographiées, pointant vers des notes renommées/supprimées). Une URL **exacte** (sans `*`) qui ne résout pas mais dont les tokens matchent le corpus (FTS ou nearest-neighbor sémantique en mode `hybrid`) déclenchait une **substitution silencieuse** : `build_context` retournait une note arbitraire (`results[0]`, sans seuil), avec `metadata.uri` réécrit vers le permalink de la note trouvée — indiscernable d'un hit voulu par l'appelant. Un agent traite alors le contenu d'une note qu'il n'a pas demandée comme la réponse légitime, sans signal de rattrapage (le garde-fou « pivot à 1 échec MCP » ne se déclenche jamais, aucun échec n'étant perçu). C'est l'analogue **intra-projet cross-note** du bleed cross-projet de la zone « Isolation » (lettre non violée, esprit violé).
- **Invariants** :
  - L'appel `resolve_link` du call-site `build_context` (URL exacte) est **`strict=True`** : les résolutions exactes (permalink, titre, file_path) survivent, mais le fallback FTS/fuzzy non seuillé (`results[0]`) est bypassé. Une URL exacte non résolue rend un résultat **vide** (« No results found », `primary_count: 0`), symétriquement à `read_note`. Ne jamais repasser ce call-site en `strict=False`.
  - Le comportement `strict=False` par défaut de `resolve_link` reste **inchangé** : il est massivement utilisé pour résoudre les wikilinks `[[...]]` pendant sync/indexing (`sync_service`, `batch_indexer`, `entity_service`, `knowledge_router`) — ces call-sites ONT BESOIN du fuzzy. Le fix est scellé au seul call-site `build_context` ; ne pas modifier la signature ni le défaut de `resolve_link`.
  - Si un jour l'étape 5 (fuzzy `results[0]`) est réactivée pour un usage `build_context`, elle doit appliquer un **seuil de score** (`best_match.score`, bm25/rank) explicite — jamais un top-1 inconditionnel.

### Parsing markdown / frontmatter (entrée non fiable)

- **Fichiers** : `src/basic_memory/file_utils.py` (`parse_frontmatter` l.326 → `yaml.safe_load` l.352), `src/basic_memory/markdown/entity_parser.py` (l.257), `src/basic_memory/mcp/tools/read_note.py` (l.107), `src/basic_memory/api/v2/routers/knowledge_router.py` (l.299, `frontmatter.loads`).
- **Risque** : les fichiers du vault sont une entrée non fiable (Obsidian Sync multi-postes, imports ChatGPT/Claude, éditions manuelles). Un YAML hostile ou malformé peut crasher le parser ; `yaml.load` non-safe permettrait l'exécution d'objets arbitraires.
- **Invariants** :
  - Toujours `yaml.safe_load` (ou `frontmatter` qui l'utilise) — jamais `yaml.load` / `yaml.unsafe_load`.
  - Un frontmatter malformé fait échouer la note concernée, pas le daemon de sync (skip + log, pas de crash du cycle).

### Credentials cloud / OAuth

- **Fichiers** : `src/basic_memory/cli/auth.py` (`save_tokens` l.182-195 — token file `basic-memory-cloud.json` en clair, `chmod 0o600`), `src/basic_memory/cli/commands/cloud/rclone_config.py` (l.100 — `secret_access_key` S3 écrit dans la config rclone), `src/basic_memory/cli/commands/cloud/core_commands.py` (API keys `bmc_`, l.241-247).
- **Risque** : tokens OAuth, refresh tokens et clés S3 stockés en clair sur disque ; fuite via logs, messages d'erreur, ou commit accidentel (repo destiné à des PRs upstream publiques).
- **Invariants** :
  - Le `chmod 0o600` sur le token file est préservé après toute modification de `save_tokens`.
  - Jamais de token/secret/API key dans les logs (loguru/logfire) ni dans les messages d'exception.
  - Aucun credential réel dans le repo, les tests ou les fixtures — uniquement des valeurs factices.

### Daemon sync/watch, reindex et index vectoriel

- **Fichiers** : `src/basic_memory/sync/{watch_service,sync_service,coordinator}.py`, `src/basic_memory/mcp/tools/reindex.py`, `src/basic_memory/indexing/` (fork sémantique : `batch_indexer.py`), `src/basic_memory/repository/sqlite_search_repository.py` (`_ensure_vector_tables` l.418-479 — DROP/recreate des tables vectorielles sur changement de dimensions).
- **Risque** : le daemon observe et synchronise le vault réel en continu. Un bug dans la détection delete/move peut supprimer des records légitimes ou, pire, propager une suppression vers le filesystem. Le vault est aussi écrit par git auto-sync et Obsidian Sync (concurrence externe).
- **Invariants** :
  - **File-first absolu** : la DB est un index dérivé. Le flux sync va filesystem → DB ; la sync ne supprime/modifie JAMAIS un fichier source (seules les opérations explicites delete_note/move_note touchent les fichiers).
  - Les patterns `.bmignore`/gitignore (`ignore_utils.py`, `watch_service._get_ignore_patterns`) sont respectés sur tout nouveau chemin de scan.
  - Les opérations destructives sur l'index (DROP tables vectorielles, `reindex_all`) ne touchent que des données reconstructibles — jamais les tables `entity`/`observation`/`relation` sans migration Alembic.
  - Single-writer sur l'index SQLite : pas de writers concurrents introduits dans le cycle de sync.

### Conséquences du niveau Strict

1. `reviewer-security` est **BLOQUANT** sur ces zones (rework immédiat si FAIL).
2. Le builder charge `adversarial-testing`, écrit les tests d'adversité AVANT les tests nominaux, et produit le marqueur `[ADVERSARIAL] X/X`.
3. Les merges upstream (`merge/upstream-*`) qui touchent ces fichiers exigent une re-vérification des invariants marqués « choix du fork » (Option B de `sanitize_for_directory`, BUG-004).
