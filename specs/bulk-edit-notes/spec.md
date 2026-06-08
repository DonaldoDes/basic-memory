---
title: Bulk Edit Notes Spec
type: spec
permalink: basic-memory/foundation/design/spec-bulk-edit-notes
status: Proposed
description: Spec technique de l'outil MCP bulk_edit_notes et de l'endpoint API v2
  bulk-edit (batch best-effort, dry-run, vector sync batché)
tags:
- type/spec
- basic-memory
- mcp
---

# Bulk Edit Notes Spec

## Overview

Outil MCP `bulk_edit_notes` + endpoint `POST /v2/projects/{project_id}/knowledge/entities/bulk-edit` permettant d'appliquer jusqu'à 100 opérations d'édition (mêmes opérations que `edit_note` single-note) en un seul appel, avec exécution **best-effort par note** (résultats item par item `success` / `failed` / `skipped`), mode dry-run (`validate_first`) et arrêt anticipé optionnel (`stop_on_error`).

Implémentation dans le fork (`/Users/donaldo/Developer/Open-Source/basic-memory`, HEAD `8ff1613d`), **fichiers additifs uniquement** — aucun refactor du chemin single-note.

Cette spec implémente l'US-004 (voir Relations). Structure transposable telle quelle vers `specs/bulk-edit-notes/spec.md` du repo (le builder committera).

## Décisions actées (validées utilisateur — ne pas rouvrir)

- **Option A** : outil MCP `bulk_edit_notes` + endpoint REST dédié `POST /v2/projects/{project_id}/knowledge/entities/bulk-edit`.
- **Best-effort par note** : un item en échec n'interrompt pas le batch (sauf `stop_on_error=true`).
- **Limite 100 items** par batch ; batch **single-project** (le `project_id` de l'URL fait foi ; identifiers `memory://` ou workspace-qualified rejetés au schéma).
- **Ordre d'exécution = ordre de la requête.** Edits séquentiels sur la même note autorisés : le 2e edit voit le résultat du 1er.
- **Pas d'auto-création** : identifier non résolu = item `failed` avec `NOT_FOUND` (divergence assumée vs write_note qui crée).
- **Vector sync 1× en fin de batch** via `sync_entity_vectors_batch` (et non 1× par note).
- **Optimisations batch-only** : C-1 (pas de `POST /resolve` préalable par item) et C-2 (indexation FTS via `background_tasks`) s'appliquent au chemin batch UNIQUEMENT. **Tout refactor du chemin single-note est INTERDIT** (invariant I-7).
- `validate_first` = **dry-run PUR** : aucune écriture, même si tous les items valident (conforme au Gherkin US-004).

## Sémantique d'exécution

### Pipeline (chemin batch)

1. **Validation Pydantic globale** (bornes 1..100, tailles, identifiers) → toute violation = `422` global, AUCUN item traité.
2. **Boucle séquentielle** sur `edits` dans l'ordre de la requête. Pour chaque item : résolution de l'identifier → lecture du contenu courant → `apply_edit_operation` → écriture + checksum → résultat item.
3. Si `stop_on_error=true` et qu'un item échoue : tous les items restants passent en `skipped` (non évalués).
4. **Fin de batch** : 1 seul appel `sync_entity_vectors_batch` sur les entity_ids modifiés avec succès. Si embeddings désactivés → `vector_sync: "disabled"` dans la réponse.

### Edits séquentiels sur la même note

Plusieurs items peuvent cibler la même note ; ils s'appliquent dans l'ordre, chaque edit voyant le contenu résultant du précédent. En mode `validate_first`, cette sémantique est préservée via une **map de contenu projeté** (`entity_id → contenu simulé`) : `apply_edit_operation` étant une fonction pure (`entity_service.py:1207-1265`), on la rejoue sur le contenu projeté sans rien persister.

### validate_first (dry-run pur)

- Tous les items sont évalués (résolution + validation de l'opération sur le contenu courant ou projeté).
- Statut item = `validated` (succès de validation) ou `failed` (avec `error_code`).
- **AUCUNE écriture** disque/DB/index, même si 100 % des items valident. Aucun vector sync.

### stop_on_error

- `false` (défaut) : best-effort — chaque item est tenté indépendamment.
- `true` : à la première failure, les items restants sont marqués `skipped` (ni évalués ni écrits). Les items déjà réussis NE SONT PAS rollback (pas de transaction batch — assumé, cohérent avec le modèle fichier-par-fichier de Basic Memory).

### Vector sync et scheduler

- Le sync vectoriel est déclenché 1× en fin de batch via la task scheduler, qui appelle `SearchService.sync_entity_vectors_batch(entity_ids)` (`search_service.py:542-546`, retourne `VectorSyncBatchResult`).
- **Point d'attention vérifié** : le registre des tasks du scheduler est `deps/services.py:498-527` (`get_task_scheduler`) et n'enregistre aujourd'hui QUE `sync_entity_vectors`, `sync_project`, `reindex_project`. `sync_entity_vectors_batch` n'y est PAS enregistré → **édition additive obligatoire** du registre. Le scheduler lève `ValueError` sur task inconnue (`deps/services.py:483-484`) et est **no-op en env test** (`deps/services.py:491-492`) — impacte le test BULK-07.
- Réponse : champ `vector_sync` = `"scheduled"` (sync planifié) ou `"disabled"` (embeddings off ou 0 note modifiée).

## Schema / Structure

Nouveaux modèles Pydantic dans `src/basic_memory/schemas/v2/bulk_edit.py` (fichier nouveau).

```python
EditOperationType = Literal[
    "append", "prepend", "find_replace",
    "replace_section", "insert_before_section", "insert_after_section",
]

class BulkEditOperation(BaseModel):
    identifier: str          # titre ou permalink — JAMAIS memory:// ni workspace-qualified
    operation: EditOperationType
    content: str             # max 1 MiB (I-2)
    section: str | None = None        # requis pour replace_section / insert_*_section
    find_text: str | None = None      # requis pour find_replace
    expected_replacements: int = Field(default=1, ge=1)
    # ge=1 : DIVERGENCE DOCUMENTEE vs single-note edit_note qui accepte 0.
    # En bulk, un expected_replacements=0 n'a pas de semantique utile et
    # masquerait des erreurs silencieuses sur 100 items.

    # Validators (model_validator) :
    # - operation == find_replace  => find_text requis et non-vide
    # - operation in section-ops   => section requise et non-vide
    # - identifier : rejet si prefixe "memory://" ou forme workspace-qualified (I-6)
    # - len(content.encode()) <= 1 MiB (I-2)

class BulkEditRequest(BaseModel):
    edits: list[BulkEditOperation] = Field(min_length=1, max_length=100)  # I-5
    validate_first: bool = False
    stop_on_error: bool = False
    # model_validator : somme des tailles content <= 10 MiB (I-3)

class BulkEditItemResult(BaseModel):
    identifier: str
    status: Literal["success", "failed", "skipped", "validated"]
    permalink: str | None = None
    file_path: str | None = None
    checksum: str | None = None
    error: str | None = None        # message lisible
    error_code: str | None = None   # voir table des codes

class BulkEditResponse(BaseModel):
    total: int
    succeeded: int
    failed: int
    skipped: int
    validated: int
    results: list[BulkEditItemResult]   # meme ordre que la requete
    vector_sync: Literal["scheduled", "disabled"]
```

## API Contract

### Endpoint REST (nouveau, additif dans `knowledge_router.py`)

```
POST /v2/projects/{project_id}/knowledge/entities/bulk-edit
Body     : BulkEditRequest
200      : BulkEditResponse (y compris si tous les items sont failed — best-effort)
422      : violation de schema globale (bornes 1..100, tailles I-2/I-3,
           identifier memory:// ou workspace-qualified, champs requis manquants)
           => AUCUN item traite
404      : project_id inconnu
```

Le chemin single-note existant `PATCH /v2/projects/{project_id}/knowledge/entities/{entity_id}` (`knowledge_router.py:489-490`, `edit_entity_by_id`) reste STRICTEMENT inchangé (I-7).

### Outil MCP (nouveau fichier `src/basic_memory/mcp/tools/bulk_edit.py`)

```
bulk_edit_notes(
    edits: list[dict],            # items BulkEditOperation
    validate_first: bool = False,
    stop_on_error: bool = False,
    project: str | None = None,   # resolution projet standard (hierarchie)
    project_id: str | None = None,
) -> str | dict                   # resume markdown + detail par item
```

Le tool délègue au client HTTP (`mcp/clients/knowledge.py`, méthode additive `bulk_edit_entities`) qui appelle l'endpoint ci-dessus. Un seul round-trip HTTP par batch (C-1 : pas de `POST /resolve` préalable par item — la résolution se fait côté API dans la boucle).

## Codes d'erreur (par item)

Alignés sur les erreurs du chemin single-note (`apply_edit_operation`, `entity_service.py:1207-1265`) :

| error_code | Déclencheur | Source single-note |
|------------|-------------|--------------------|
| `NOT_FOUND` | identifier non résolu (pas d'auto-création, I-4) | 404 edit_entity_by_id |
| `TEXT_NOT_FOUND` | find_replace : `find_text` absent du contenu | ValueError "Text to replace not found" (entity_service.py:1240) |
| `REPLACEMENT_COUNT_MISMATCH` | occurrences réelles ≠ `expected_replacements` | ValueError "Expected N occurrences" (entity_service.py:1242-1245) |
| `DUPLICATE_SECTION` | section cible présente plusieurs fois (replace_section / insert_*) | erreur replace_section_content |
| `AMBIGUOUS_IDENTIFIER` | identifier résolvant 2+ entités | résolution |
| `SECURITY` | path traversal détecté dans l'identifier (I-1) | nouveau (batch) |
| `UNKNOWN_ERROR` | code de dernier recours — exception interne non classée (ne matche aucun cas ci-dessus) | fallback `_map_bulk_edit_error` |

Un item `failed` porte toujours `error_code` + `error` (message). Le batch n'est jamais interrompu par une erreur item (hors `stop_on_error=true`).

## Invariants de sécurité (I-1..I-7)

Le builder chargera les skills `mcp-server` + `adversarial-testing`. Chaque invariant est traduit en test d'adversité.

- **I-1** — Aucun identifier contenant un path traversal (`../`, chemin absolu hors projet, séquences encodées) ne peut atteindre le filesystem : l'item passe en `failed` avec `error_code: SECURITY`, le batch n'est PAS interrompu.
- **I-2** — Aucun `content` d'item ne peut dépasser 1 MiB : rejet Pydantic → `422` global avant tout traitement.
- **I-3** — Aucune requête dont les `content` cumulés dépassent 10 MiB ne peut être traitée : rejet Pydantic → `422` global avant tout traitement.
- **I-4** — Aucun item ne peut créer une note : identifier non résolu = `failed NOT_FOUND`, jamais d'auto-création.
- **I-5** — Aucun batch ne peut contenir plus de 100 items (ni 0) : `422` global.
- **I-6** — Aucun item ne peut cibler un autre projet que le `project_id` de l'URL : identifiers `memory://` et workspace-qualified rejetés au schéma (`422` global).
- **I-7** — Aucune modification du chemin single-note : `edit_entity_by_id` (`knowledge_router.py:489-490`), `edit_entity_with_content` et le flow MCP `edit_note` restent byte-identiques. Les optimisations C-1/C-2 vivent exclusivement dans le chemin batch.

## Stratégie fichiers (implémentation — additive uniquement)

### 2 fichiers nouveaux

| Fichier | Contenu |
|---------|---------|
| `src/basic_memory/schemas/v2/bulk_edit.py` | Modèles Pydantic (section Schema) |
| `src/basic_memory/mcp/tools/bulk_edit.py` | Outil MCP `bulk_edit_notes` |

### 5 éditions additives (append-only, aucun refactor)

| Fichier | Édition |
|---------|---------|
| `src/basic_memory/api/v2/routers/knowledge_router.py` | + handler `POST .../bulk-edit` |
| `src/basic_memory/mcp/clients/knowledge.py` | + méthode client `bulk_edit_entities` |
| `src/basic_memory/mcp/tools/__init__.py` | + export `bulk_edit_notes` |
| `src/basic_memory/schemas/v2/__init__.py` | + exports modèles bulk_edit |
| `src/basic_memory/deps/services.py` | + enregistrement task `sync_entity_vectors_batch` dans le registre `get_task_scheduler` (:498-527) — OBLIGATOIRE, sinon `ValueError` (:483-484) |

### 5 fichiers de tests

| Fichier | Couverture |
|---------|-----------|
| `tests/schemas/test_bulk_edit_schemas.py` | BULK-01, 09, 12, 13 (validation) |
| `tests/api/v2/test_bulk_edit_router.py` | BULK-02, 03, 04, 05, 08, 11, 14 |
| `tests/mcp/test_tool_bulk_edit.py` | tool MCP (mapping params/réponse) |
| `test-int/mcp/test_bulk_edit_integration.py` | BULK-06, 07 (FTS, vector sync) |
| `test-int/test_bulk_edit_benchmark.py` | BULK-10 (benchmark) |

## Ancrages code vérifiés (HEAD 8ff1613d, re-validés 2026-06-05)

| Ancrage | Localisation | Vérifié |
|---------|--------------|---------|
| `apply_edit_operation` (pure, réutilisée pour le dry-run) | `src/basic_memory/services/entity_service.py:1207-1265` | OUI |
| Registre tasks scheduler (`get_task_scheduler`) | `src/basic_memory/deps/services.py:498-527` — correction : PAS :460-495 qui est `LocalTaskScheduler` | OUI |
| `ValueError` sur task non enregistrée | `src/basic_memory/deps/services.py:483-484` | OUI |
| Scheduler no-op en env test (`BASIC_MEMORY_ENV=test`) | `src/basic_memory/deps/services.py:491-492` — impacte BULK-07 | OUI |
| `sync_entity_vectors_batch(entity_ids, progress_callback) -> VectorSyncBatchResult` | `src/basic_memory/services/search_service.py:542-546` | OUI |
| Endpoint single-note `edit_entity_by_id` (intouchable, I-7) | `src/basic_memory/api/v2/routers/knowledge_router.py:489-490` | OUI |
| `POST /resolve` (NON appelé par le chemin batch — C-1) | `src/basic_memory/api/v2/routers/knowledge_router.py:152` | OUI |
| Erreurs find_replace (messages sources des codes) | `src/basic_memory/services/entity_service.py:1240, 1242-1245` | OUI |

## Test IDs

| ID | Type | Description |
|----|------|-------------|
| BULK-01 | Unit (schema) | Bornes batch : `edits` vide → 422 ; 101 items → 422 ; 1 et 100 items acceptés |
| BULK-02 | API | `validate_first=true` : items `validated`, AUCUNE écriture disque/DB/index même si tout valide (dry-run pur) |
| BULK-03 | API | Best-effort : item NOT_FOUND au milieu du batch → items suivants traités, compteurs et statuts item par item corrects |
| BULK-04 | API | `stop_on_error=true` : première failure → tous les items restants en `skipped`, items déjà réussis non rollback |
| BULK-05 | API | 2 edits séquentiels sur la même note : le 2e voit le résultat du 1er (ordre = ordre requête) — y compris en dry-run (map projetée) |
| BULK-06 | Integration (test-int) | Cohérence FTS post-batch : 20 notes éditées → la recherche retrouve le nouveau contenu après flush des background_tasks (C-2) |
| BULK-07 | Integration (test-int) | Vector sync appelé exactement 1× en fin de batch via `sync_entity_vectors_batch` — ATTENTION : scheduler no-op en env test (`deps/services.py:491-492`) → asserter sur l'appel `schedule` (spy/mock) ou exercer le codepath sync directement |
| BULK-08 | API | Pas d'auto-création : identifier inexistant → `failed NOT_FOUND`, aucune note créée sur disque ni en DB (I-4) |
| BULK-09 | Unit (schema) | Identifiers `memory://` et workspace-qualified rejetés au schéma → 422 global (I-6, single-project) |
| BULK-10 | Benchmark (test-int) | 50 notes via 1 `bulk_edit_notes` vs 50× `edit_note` : gain wall-clock mesuré et rapporté (C-1 + C-2 + vector sync batché) |
| BULK-11 | Security | Path traversal dans identifier (`../`, absolu, encodé) → item `failed` avec `error_code: SECURITY`, batch non interrompu, aucun accès fichier hors projet (I-1) |
| BULK-12 | Unit (schema) | Limites de taille : `content` > 1 MiB → 422 (I-2) ; cumul > 10 MiB → 422 (I-3) ; dans les deux cas AUCUN item traité |
| BULK-13 | Unit + API | `expected_replacements=0` rejeté au schéma (ge=1, divergence documentée vs single-note) ; occurrences ≠ attendu → item `failed REPLACEMENT_COUNT_MISMATCH` |
| BULK-14 | API | Identifier ambigu (résout 2+ entités) → item `failed AMBIGUOUS_IDENTIFIER`, batch continue (best-effort) |

## Contraintes

- **Repo (CONTRIBUTING.md)** : couverture tests ~100 % sur le code nouveau, `ruff` clean, type hints Python 3.12+, benchmarks dans `test-int/`, DCO sign-off sur les commits (upstream ultérieur).
- **Additif uniquement** : aucun diff sur le chemin single-note (I-7) — vérifiable par review du diff.
- **Single-writer** : l'exécution batch est séquentielle côté API (pas de parallélisation intra-batch) — cohérent avec SQLite et le modèle fichier de Basic Memory.

## Migration

Aucune : fichiers additifs uniquement, pas de changement de schéma DB, pas de breaking change API (nouvel endpoint).

## Relations

- implements [[products/Basic Memory/backlog/stories/US-004 Bulk Edit Notes via MCP|US-004 Bulk Edit Notes via MCP]]
- part_of [[products/Basic Memory/Basic Memory|Basic Memory]]
