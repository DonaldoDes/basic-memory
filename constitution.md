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
