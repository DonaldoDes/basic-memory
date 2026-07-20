---
title: Résumé court indexé — Calcul et persistance à l'indexation (US-006a)
type: spec
permalink: basic-memory/foundation/design/spec-resume-court-indexation
status: Proposed
description: Spec technique de la couche indexation du résumé court — fonction pure compute_summary,
  colonne summary de search_index (SQLite FTS5 UNINDEXED / Postgres TEXT), migration Alembic
  drop+recreate (SQLite) / ADD COLUMN (Postgres), backfill par reindex, parité par construction
tags:
- type/spec
- basic-memory
- search
- indexing
---

# Résumé court indexé — Calcul et persistance à l'indexation (US-006a)

## Overview

Première moitié du split de US-006 (cf. ADR-007 § Decision 1). Cette spec couvre **uniquement la couche
indexation** : calcul d'un résumé court (~200 caractères) au moment de l'indexation et sa persistance
dans une nouvelle colonne `summary` de `search_index`, sur les deux backends (SQLite FTS5 / Postgres).

**Aucun changement de comportement observable** sur `build_context` ou `search_notes` : la colonne est
peuplée mais pas exposée par les renderers MCP (c'est le périmètre de US-006b). La donnée est écrite,
lue au niveau du dataclass `SearchIndexRow`, mais aucun renderer ne la projette en sortie.

Référence normative : ADR-007 (Decisions 1, 2, 4, 5) et US-006a. En cas de divergence, l'US-006a fait foi.

## Architecture

Flux d'indexation inchangé : `SearchService.index_entity_markdown` construit un `SearchIndexRow` par
entité/observation/relation et délègue l'INSERT au repository backend. Cette spec ajoute un unique champ
calculé sur les lignes **entité** uniquement.

### 1. Fonction pure `compute_summary`

`services/search_service.py` (module-level, à côté de `_strip_nul`) :

```python
def compute_summary(description: str | None, content_snippet: str | None) -> str | None: ...
```

Algorithme (ADR-007 § Decision 4), déterministe, sans LLM :

1. **Source prioritaire** : `description` (frontmatter, lue de `entity.entity_metadata["description"]`)
   si non vide après `strip()`.
2. **Fallback** : lead de `content_snippet` — après (a) retrait d'un bloc frontmatter résiduel de tête,
   (b) retrait des lignes de titre markdown de tête (`# …`) et lignes vides de tête, (c) normalisation
   des espaces/retours en une seule ligne.
3. **Dégradation** : `None` si ni description ni lead exploitable — aucune erreur levée.
4. **Cap + coupe propre** (constante `SUMMARY_MAX_LENGTH = 200`) :
   - si la source normalisée tient dans la limite → retour tel quel, **sans** suffixe ;
   - sinon, si une fin de phrase (`.`, `!`, `?`) existe dans la fenêtre `[SUMMARY_SENTENCE_WINDOW=120,
     SUMMARY_MAX_LENGTH=200]`, couper juste après cette ponctuation (lecture complète → **pas** de suffixe) ;
   - sinon reculer au dernier espace avant la limite (jamais de coupe en plein mot) et suffixer `…`.
5. **Sûreté** : `_strip_nul` appliqué ; normalisation whitespace garantit l'absence de retour à la ligne
   dans la valeur finale.

### 2. Colonne `summary` (DDL — `models/search.py`)

- **SQLite** (FTS5 virtual table `CREATE_SEARCH_INDEX`) : colonne `summary UNINDEXED` (non recherchable,
  stockage/affichage seul).
- **Postgres** (`CREATE_POSTGRES_SEARCH_INDEX_TABLE`) : colonne `summary TEXT`.

Les DDL (installs fraîches + tests) sont synchronisées avec la migration Alembic.

### 3. Câblage `SearchIndexRow` (`repository/search_index_row.py`)

- Nouveau champ `summary: Optional[str] = None`.
- `to_insert()` expose `"summary": self.summary`.

### 4. Chemins d'écriture (INSERT) — deux backends

- **Base partagée** (`search_repository_base.py`) `index_item` / `bulk_index_items` : colonne `summary`
  + placeholder `:summary` (utilisé par SQLite).
- **Postgres** (`postgres_search_repository.py`) `index_item` / `bulk_index_items` : upsert — colonne
  `summary`, `:summary`, et `summary = EXCLUDED.summary` dans le `DO UPDATE SET`.

### 5. Chemin de lecture (SELECT) — deux backends

- SQLite `search()` et Postgres `search()` : `search_index.summary` ajouté au SELECT, hydraté dans
  `SearchIndexRow(summary=row.summary)`. Aucun renderer ne le consomme (non-goal US-006b respecté).

### 6. Point de calcul (`index_entity_markdown`)

Dans la même passe que `content_snippet`, sur la ligne **entité** uniquement :

```python
description = entity.entity_metadata.get("description") if entity.entity_metadata else None
summary = compute_summary(description, content_snippet)
```

`summary` passé au `SearchIndexRow` entité. Observations/relations conservent `summary=None` (défaut).

### 7. Migration Alembic

Nouveau fichier `versions/`, `down_revision = "n7i8j9k0l1m2"` (head courant) :

- **Postgres** : `ALTER TABLE search_index ADD COLUMN summary TEXT` (si la table existe).
- **SQLite** : FTS5 n'accepte pas `ALTER TABLE ADD COLUMN` sur une virtual table → **drop + recreate**
  de `search_index` avec la colonne `summary UNINDEXED` (DDL inline, self-contained). La table étant un
  index **dérivé**, le drop est sûr.
- **Backfill = reindex** : dans les deux cas, la colonne se peuple au prochain `basic-memory reindex --full`
  (ou en continu via l'auto-sync du vault cible). Documenté en post-deploy obligatoire dans le docstring
  de la migration.
- `downgrade` : Postgres `DROP COLUMN` ; SQLite drop+recreate sans `summary`.

## Parité SQLite/Postgres

Par construction : le résumé est calculé **une fois en Python** à l'indexation, avant persistance.
La valeur est donc byte-identique quel que soit le backend. Aucune logique SQL de calcul par dialecte.

## Test IDs (ADR-007)

| ID | Type | Backend | Couverture |
|----|------|---------|-----------|
| SUM-01 | Unit | — | `description` présente → résumé = description, borné ~200 |
| SUM-02 | Unit | — | Fallback lead de `content_snippet`, borné ~200 |
| SUM-03 | Unit | — | Coupe propre jamais en plein mot ; `…` seulement si tronqué |
| SUM-04 | Unit | — | Coupe sur fin de phrase dans la fenêtre de tolérance |
| SUM-05 | Unit | — | Dégradation ni description ni lead → `None`, aucune erreur |
| SUM-06 | Unit | — | Lead : frontmatter résiduel + H1 retirés, whitespace normalisé, NUL strippé |
| SUM-07 | Integration | SQLite | Indexation → colonne `summary` peuplée |
| SUM-08 | Integration | Postgres | Idem SUM-07 (même test sous `BASIC_MEMORY_TEST_POSTGRES=1`) |
| SUM-09 | Integration | SQLite+PG | `summary` == valeur `compute_summary` attendue (parité) |
| SUM-10 | Integration | SQLite | Backfill par reindex → toutes les lignes entité peuplées |
| SUM-11 | Integration | Postgres | Idem SUM-10 |
| SUM-12 | Integration | SQLite+PG | Fraîcheur : modifier `description` puis reindex → `summary` reflète la nouvelle valeur |

Note double-backend : le harness rejoue les mêmes fichiers de test sous SQLite (défaut) et sous Postgres
(`BASIC_MEMORY_TEST_POSTGRES=1`). SUM-07/08 et SUM-10/11 sont le même test exécuté sous les deux
configurations ; SUM-09 vérifie l'égalité à la valeur Python calculée, garantissant la parité sous chaque run.

## Hors scope (US-006a — couvert par US-006b ci-dessous)

Le périmètre suivant était hors scope d'US-006a. Il est **désormais couvert par US-006b** (section
« Exposition » ci-dessous) :

- Projection `si.summary` dans les CTE `build_context`.
- Champ `EntitySummary.summary` et exposition JSON/markdown.
- Exposition sur `search_notes`.
- Renderers MCP `build_context.py` / `search.py`, sérialiseur `api/v2/utils.py`.

---

# Exposition du résumé — build_context et search_notes (US-006b)

Seconde moitié du split de US-006 (ADR-007 § Decision 1, 3). Cette section couvre **uniquement la couche
exposition** : le résumé déjà persisté par US-006a (colonne `search_index.summary`, hydraté sur
`SearchIndexRow.summary`) est désormais rendu visible sur les deux surfaces MCP. **Aucune nouvelle donnée
n'est calculée ni persistée** — cette couche consomme exclusivement la colonne existante.

Référence normative : ADR-007 (Decisions 1, 3) et US-006b. En cas de divergence, l'US-006b fait foi.

## Levée du `[NEEDS CLARIFICATION]` (couverture de peuplement primaire vs related)

ADR-007 § Decision 3 laissait ouvert : le résultat **primaire** de `build_context` et les résultats de
`search_notes` passent-ils par la même classe `EntitySummary`, ou le primaire est-il un `SearchIndexRow` ?
Résolution par lecture du code (`context_service.py`, `api/v2/utils.py`, `schemas/memory.py`,
`schemas/search.py`) :

| Surface | Résultat | Type interne (service) | Classe de sortie (schéma public) | `summary` disponible avant 006b ? |
|---------|----------|------------------------|----------------------------------|-----------------------------------|
| `build_context` | **primaire** | `SearchIndexRow` | `EntitySummary` | **Oui** — `SearchIndexRow.summary` hydraté par US-006a |
| `build_context` | **related** | `ContextResultRow` (CTE `find_related`) | `EntitySummary` | Non — la CTE ne projette pas encore `si.summary` |
| `search_notes` | résultats | `SearchIndexRow` | `SearchResult` | **Oui** — `SearchIndexRow.summary` hydraté par US-006a |

Conclusions tranchées :

1. **build_context** : primaire ET related sont sérialisés vers la **même** classe `EntitySummary`
   (helper `to_summary` dans `api/v2/utils.py`). Le primaire (`SearchIndexRow`) porte déjà `summary`
   depuis US-006a ; il suffit que `to_summary` le lise. Les related (`ContextResultRow`) exigent la
   projection CTE + un nouveau champ dataclass. → **`summary` est peuplé sur le primaire ET les related.**
2. **search_notes** : les résultats passent par `SearchResult` (schéma distinct d'`EntitySummary`), et non
   par `EntitySummary`. `SearchResult` reçoit un nouveau champ `summary`, alimenté depuis
   `SearchIndexRow.summary` (déjà hydraté).

Décision : **peupler `summary` sur le primaire de `build_context`** (utile, gratuit — la donnée est déjà
présente sur `SearchIndexRow`), en plus des related. Le contrat n'est pas modifié — seule la couverture de
peuplement l'est, comme cadré par l'ADR.

## 1. Projection CTE `build_context` (`services/context_service.py`)

- `ContextResultRow` (dataclass) : nouveau champ `summary: Optional[str] = None` (symétrique à `content`).
- **CTE SQLite** (`_build_sqlite_query`, 3 branches `UNION ALL`) :
  - branche de base (seed entity) et branche entités-connectées : `si.summary as summary` (LEFT JOIN
    `search_index si` déjà présent) ;
  - branche relations : `NULL as summary` (symétrique à `NULL as content`) ;
  - `summary` ajouté au `SELECT DISTINCT` final et au `GROUP BY`.
- **CTE Postgres** (`_build_postgres_query`, base + terme récursif `CROSS JOIN LATERAL`) :
  - branche de base : `si.summary as summary` ;
  - terme récursif : `CASE WHEN step_type = 1 THEN CAST(NULL AS TEXT) ELSE si.summary END as summary` ;
  - `summary` ajouté au `SELECT DISTINCT` final et au `GROUP BY`.
- `find_related` : mapping `row → ContextResultRow` reçoit `summary=row.summary`.

Contrainte d'arité `UNION ALL` : la colonne `summary` est présente dans **toutes** les branches, à la même
position (juste après `content`), sur les deux backends.

## 2. Contrat public `EntitySummary` (`schemas/memory.py`) — additif

- Ajout `summary: Optional[str] = None`.
- **Conservation** de `content: Optional[str]` avec sa sémantique inchangée, marqué `DEPRECATED` en
  commentaire (retrait différé à un bump majeur ≥ v0.19, ticket de dette séparé). Pas de rename cassant.

## 3. Contrat public `SearchResult` (`schemas/search.py`) — additif

- Ajout `summary: Optional[str] = None`, en plus du `matched_chunk` existant (sémantiques distinctes :
  `matched_chunk` = extrait ayant matché la requête ; `summary` = digest court stable de la note).

## 4. Sérialiseurs (`api/v2/utils.py`)

- `to_summary` (build_context, branche `EntitySummary`) : `summary=getattr(item, "summary", None)` —
  couvre le primaire (`SearchIndexRow.summary`) et les related (`ContextResultRow.summary`).
- `to_search_results` (search_notes → `SearchResult`) : `summary=result.summary`.

## 5. Renderers MCP

- `mcp/tools/build_context.py` (`_format_entity_block`) : rendu markdown du `summary` sur les entités
  related (sous le lien `[[title]] (permalink)`). Le JSON est automatique via `model_dump`.
- `mcp/tools/search.py` (`_format_search_markdown`) : ligne `- summary: …` en plus de
  `- match: …` (matched_chunk). Le JSON est automatique via `model_dump`.

## Test IDs (ADR-007)

| ID | Type | Backend | Couverture |
|----|------|---------|-----------|
| SUM-13 | Integration | SQLite | CTE `build_context` : related portent `summary` peuplé, relations `summary = NULL` |
| SUM-14 | Integration | Postgres | Idem SUM-13 (arité `UNION ALL` + `CAST` respectés) |
| SUM-15 | Unit | — | `EntitySummary` additif : `summary` présent et peuplé, `content` conservé (compat v0.18) |
| SUM-16 | Integration | — | Rendu MCP `build_context` : `summary` présent en markdown ET JSON sur les related |
| SUM-17 | Integration | — | Rendu MCP `search_notes` : `summary` présent en markdown ET JSON, en plus de `matched_chunk` |
| SUM-18 | E2E/smoke | — | Consommation via les deux outils MCP : le markdown affiche un résumé par related/résultat qui en a un |

## Non-régression (US-006a et BUG-022)

- Aucune modification de `search_service.py`, `compute_summary`, la migration Alembic, le DDL de
  `search_index` (colonne `summary` déjà en place). US-006b consomme, ne recalcule pas.
- La logique de résolution stricte `build_context` (BUG-022, `context_service.py` branche URL exacte,
  `resolve_link(strict=True)`) est **inchangée** : la projection `si.summary` est orthogonale à la
  résolution du primaire. Les tests BUG-022 restent verts.

## Hors scope (US-006b)

- Calcul/persistance du résumé (US-006a).
- Retrait de `EntitySummary.content` (breaking, différé ≥ v0.19).
- Modification du cap 4000 de `SearchIndexRow.content`.
- Génération de résumé par LLM.
- Modification du cap 4000 de `SearchIndexRow.content`.
