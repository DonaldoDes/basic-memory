# CLAUDE.md — Basic Memory (fork local)

Guide de contribution au code de ce fork. Il couvre ce qu'il faut pour
modifier la codebase sans casser la CI.

## Commandes

**Boucle courte (par défaut)** : coder → `just fast-check` → `just doctor`.

```
just install       # installation dev (ou: pip install -e ".[dev]")
just fast-check    # lint + format + typecheck + tests impactés (testmon)
just doctor        # vérif de cohérence bout-en-bout (file <-> DB), en projet temporaire
just test          # suite complète (SQLite + Postgres)
just test-sqlite   # suite complète, SQLite seul (pas de Docker requis)
just lint          # ruff check . --fix
just typecheck     # uv run ty check src tests test-int
just format        # uv run ruff format .
just check         # lint + format + typecheck + test
just migration "message"   # crée une migration Alembic
```

Test unitaire ciblé : `pytest tests/chemin/test_fichier.py::test_nom`.

Notes :
- Les tests Postgres passent par testcontainers (Docker doit tourner). Par
  défaut, tout tourne sur SQLite.
- `just doctor` utilise un HOME/config temporaire : il ne touche pas la
  configuration Basic Memory locale.
- `just testmon` peut collecter 0 test si rien n'a changé — c'est attendu.

## Style de code

- **Python 3.12+** (syntaxe de paramètres de type et alias `type`), annotations
  de type **complètes**.
- **Ligne : 100 caractères max**. Formatage et lint par **ruff**.
- Ordre des imports : stdlib, tiers, local. `snake_case` pour les
  fonctions/variables, `PascalCase` pour les classes.
- **Pydantic v2 aux frontières** (API, CLI, MCP, persistance) où validation et
  sérialisation comptent ; **dataclasses en interne** pour les objets valeur et
  les résultats d'opération.
- Async avec SQLAlchemy 2.0. Frontières async explicites : le code qui détient
  une ressource utilise des context managers et propage l'annulation.
- **Fail fast** : pas de fallback silencieux, pas de `except` large, pas de
  `getattr` spéculatif, pas de cast masquant un modèle mal défini.
- Pattern repository pour l'accès aux données. Les outils MCP parlent aux
  routers API via le client ASGI httpx (in-process).
- **Couverture à 100 %** : écrire les tests du code ajouté. `# pragma: no cover`
  réservé aux cas exigeant un mocking excessif (blocs `TYPE_CHECKING`,
  handlers d'erreur à injection de panne, chemins dépendant du mode runtime).
- Les commentaires expliquent *pourquoi* une branche, un invariant ou une
  contrainte existe — pas ce que le code fait déjà lire.
- Lire un fichier en entier avant de l'éditer ; minimiser le diff, pas de
  refactor non demandé.

Style de maison détaillé : `docs/ENGINEERING_STYLE.md`.

## Architecture

Flux d'un appel : **MCP Tool → client typé → API HTTP → Router → Service →
Repository**. Les fichiers markdown sont la source de vérité ; SQLite sert
d'index et de moteur de recherche plein texte.

Répertoires sous `src/basic_memory/` :

- `alembic/` — migrations de base
- `api/` — endpoints FastAPI + `container.py` (composition root)
- `cli/` — CLI Typer + `container.py` (composition root)
- `deps/` — dépendances FastAPI par domaine (config, db, projects,
  repositories, services, importers)
- `importers/` — imports Claude, ChatGPT, etc.
- `markdown/` — parsing et traitement markdown
- `mcp/` — serveur MCP + `container.py` + `clients/` (clients API typés)
- `models/` — modèles ORM SQLAlchemy
- `repository/` — couche d'accès aux données
- `schemas/` — modèles Pydantic de validation
- `services/` — logique métier
- `sync/` — synchronisation de fichiers + `coordinator.py` (cycle de vie)

Hors du cœur Python : `plugins/claude-code/`, `skills/`,
`integrations/hermes/`, `integrations/openclaw/`.

Chaque point d'entrée (API, MCP, CLI) a un composition root qui lit le
`ConfigManager` (seul endroit qui lit la config globale), résout le mode via
`RuntimeMode` (TEST > CLOUD > LOCAL) et injecte explicitement les dépendances.

Clients typés MCP (`mcp/clients/`) : `KnowledgeClient`, `SearchClient`,
`MemoryClient`, `DirectoryClient`, `ResourceClient`, `ProjectClient`.

Détail : `docs/ARCHITECTURE.md`.

### Structure des tests

- `tests/` — unitaires, rapides, mocks quand nécessaire
- `test-int/` — intégration, implémentations réelles, pas de mocks
- Marqueurs pytest : `@pytest.mark.benchmark`, `@pytest.mark.slow`,
  `@pytest.mark.smoke`

## Pattern client async

Dans les **outils MCP**, utiliser `get_project_client()` (routage par projet) :

```python
from basic_memory.mcp.project_context import get_project_client

@mcp.tool()
async def my_tool(project: str | None = None, context: Context | None = None):
    async with get_project_client(project, context) as (client, active_project):
        response = await call_get(client, "/path")
        return response
```

**Ailleurs** (commandes CLI, code non scopé projet), utiliser `get_client()` :

```python
from basic_memory.mcp.async_client import get_client

async with get_client() as client:
    response = await call_get(client, "/path")

# Routage par projet quand le nom est connu :
async with get_client(project_name="research") as client:
    ...
```

Proscrit :

- `from basic_memory.mcp.async_client import client` (client module-level déprécié)
- gestion manuelle des en-têtes d'auth
- `inject_auth_header()` (supprimé)
- `get_client()` + `get_active_project()` séparés dans un outil MCP — utiliser
  `get_project_client()`

L'auth se fait à la création du client, pas par requête. Chaque projet est
LOCAL ou CLOUD indépendamment ; priorité de routage : injection de factory >
force-local > cloud par projet > cloud global > ASGI local.

## Contribution — ce qui fait échouer la CI

- **Signer les commits** : `git commit -s` (DCO). Une branche portant des
  commits non signés doit être réécrite avant review.
- **Titre de PR sémantique** `type(scope): summary`, validé par
  `.github/workflows/pr-title.yml`. Scopes autorisés : `core`, `cli`, `api`,
  `mcp`, `sync`, `ui`, `ci`, `deps`, `installer`, `plugins`, `skills`,
  `integrations`.
- **`main` n'accepte que des PR** (poussée directe rejetée, pas de commit de
  merge — les PR sont rebase-mergées).
- Lancer `just typecheck` en plus des `ruff`/`pytest` ciblés dès que des tests
  sont ajoutés ou modifiés.
- Lancer `just package-check` si le changement touche `plugins/`, `skills/`,
  `integrations/`, les métadonnées de package ou le câblage de release.

## Règles du dépôt

`AGENTS.md` reste le guide amont, maintenu par le projet upstream ; ce fichier
en est une réduction locale, centrée sur la contribution au code. Les règles
propres à ce dépôt vivent dans `constitution.md`, importé ci-dessous.

@constitution.md
