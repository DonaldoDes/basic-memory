---
title: Bases Executor Spec (Phase 1 — parity-first)
type: spec
permalink: basic-memory/foundation/design/spec-bases-executor
status: Proposed
description: Spec technique du module exécuteur Obsidian Bases (enable_bases) — surface
  DQL de parité Phase 1, grammaire fermée, bornes anti-DoS, câblage MCP, exclusions Phase 2
tags:
- type/spec
- basic-memory
- mcp
- bases
---

# Bases Executor Spec (Phase 1 — parity-first)

## Overview

Module fork-local `src/basic_memory/bases/` rendant côté agent les blocs ```` ```base ```` (Obsidian Bases, core ≥ 1.9) — symétrique au module Dataview existant (`src/basic_memory/dataview/`). Objectif **parity-first** : rendu octet-identique à Dataview sur la surface DQL de parité (équivalents `FROM` / `WHERE` simple / `SORT` / `LIMIT`, vues `TABLE`/`LIST`), zéro régression sur les ~287 blocs agent-read.

Cette spec dérive de l'**ADR-003 Bases Executor Module Design** (Accepted, 2026-06-12 — fait foi). En cas de divergence, l'ADR-003 prévaut. Implémentation **additive uniquement** : aucun fichier du core upstream n'est modifié hors du câblage des flags `enable_bases` sur les 3 tools MCP qui portent déjà `enable_dataview`.

## Architecture du module

Flux : **detect → parse/validate → resolve → filter → render**.

```
src/basic_memory/bases/
├── __init__.py          # exports
├── detector.py          # BasesDetector — fenced blocks ```base``` (pattern detector.py Dataview)
├── schema.py            # dataclasses typées : BasesQuery, BasesView, ViewType, bornes (constantes anti-DoS)
├── errors.py            # BasesError / BasesParseError / BasesUnsupportedError / BasesLimitError
├── parser.py            # SafeLoader fork (sans anchors/aliases) + validation schéma Phase 1
│                        #   + mini-parser d'expressions de filtre (grammaire fermée)
│                        #   → compile vers les nœuds AST Dataview (BinaryOpNode, FunctionCallNode, FieldNode, LiteralNode)
├── executor.py          # BasesExecutor — filtrage source (inFolder→préfixe), évaluation de l'arbre
│                        #   de filtres via ExpressionEvaluator (importé), sort/limit, dispatch vue
└── integration.py       # BasesIntegration — enveloppe résultat miroir de DataviewIntegration
```

**Décision structurante** : le parser compile les feuilles de filtre vers les nœuds AST du module Dataview (`dataview/ast.py`). L'évaluation réutilise alors `ExpressionEvaluator`, `FieldResolver` et `ResultFormatter` **par import direct** (~323 LOC partagées, rendu octet-identique garanti par construction).

## Réutilisation vs net-neuf

| Composant Dataview | Verdict | Usage Bases |
|---|---|---|
| `executor/field_resolver.py` `FieldResolver` | Import direct | `file.*` + frontmatter direct |
| `executor/expression_eval.py` `ExpressionEvaluator` | Import direct | 8 opérateurs + 4 fonctions de parité |
| `executor/result_formatter.py` `ResultFormatter` | Import direct (partiel) | `format_table` / `format_list` (rendu octet-identique) |
| `ast.py` nœuds d'expression | Import direct | Cible de compilation des feuilles de filtre |
| `clients/knowledge.py` `list_entities_for_dataview` | Réutilisé tel quel | Même dataset, format nested `{"file": {...}, "frontmatter": {...}}` |
| `integration.py` `DataviewIntegration` | Copie-adaptation | Enveloppe résultat + gestion d'erreurs 3 niveaux |
| `executor/executor.py` `DataviewExecutor` | Copie-adaptation | `_filter_by_from` / `_filter_by_where` / `_apply_sort` |
| `detector.py` `DataviewDetector` | Copie-adaptation | Scan ligne-à-ligne, regex `^```base\s*$` (pas d'inline) |
| `errors.py` | Copie-adaptation | Hiérarchie miroir |
| `executor/task_extractor.py` `TaskExtractor` | NON réutilisé (Phase 1) | Vue TASK retirée (NC-2 ADR-003) |
| `lexer.py` / `parser.py` (DQL) | NON réutilisable | Grammaire DQL textuelle disjointe du YAML Bases |

## Surface DQL Phase 1

### Schéma de bloc accepté

```yaml
filters:                      # optionnel — string (feuille unique) ou conjonction
  and:                        # and: / or: / not: — récursifs (profondeur ≤ 10)
    - file.inFolder("projects")     # feuille = chaîne d'expression (grammaire fermée)
    - status == "Active"
views:                        # obligatoire, ≥ 1 — seule views[0] est rendue côté agent
  - type: table               # table | list  (TASK / cards / inconnu → unsupported → bloc inerte)
    name: Projects            # optionnel
    order: [file.name, status]      # colonnes (TABLE) : file.* ou champ frontmatter
    sort:
      - property: file.mtime
        direction: DESC       # ASC | DESC
    limit: 20                 # optionnel (accepté niveau vue ET racine — NC-3)
properties:                   # optionnel — displayName → alias de colonne (≡ "as Alias" DQL)
  status:
    displayName: Status
```

### Grammaire fermée des feuilles de filtre

```
expr        := or_expr
or_expr     := and_expr ( "||" and_expr )*
and_expr    := unary ( "&&" unary )*
unary       := [ "!" ] comparison
comparison  := operand [ op operand ]
op          := "==" | "!=" | "<=" | ">=" | "<" | ">"
operand     := funcall | field_ref | literal
funcall     := field_ref "." name "(" args? ")"        # forme méthode : status.contains("x")
             | name "(" args? ")"                       # forme globale : contains(status, "x")
field_ref   := ident ( "." ident )*                     # status, file.name, note.champ
literal     := string_quoted | number | true | false | null
```

Normalisation vers l'AST Dataview : `==` → `=`, `&&` → `AND`, `||` → `OR`, forme méthode `champ.fn(args)` → `FunctionCallNode("fn", [FieldNode(champ), args...])`.

### Opérateurs (8)

`= != < > <= >= AND OR` — couverts par `ExpressionEvaluator._eval_binary_op`.

### Fonctions de parité (whitelist fermée, 5)

- `file.inFolder(str)` — ≡ `FROM` préfixe : match par préfixe de `file.path` des entités du dataset, **jamais d'accès filesystem**. **Négation (US-7, M-Bases-P4)** : `not file.inFolder("X")` / `!file.inFolder("X")` en feuille de filtre est une **exclusion de sous-arbre** (pas une source `FROM`) — réécrite en `not contains(file.path, "X")` (test de préfixe par row, grammaire fermée), routée vers le même sandbox que toute feuille fonction-appel niée ; `inFolder` n'est **jamais** ajouté à la whitelist de formules.
- `contains`, `length`, `lower`, `upper` — tolérantes aux **deux formes** (méthode `value.lower()` ET globale `lower(value)`), normalisées vers `FunctionCallNode`.

Toute autre fonction (`link`, `meta`, `round`, `dateformat`, …), toute property chain (`value.asFile()…`), tout lambda → `BasesUnsupportedError` → bloc inerte.

### Champs

`file.name`, `file.link`, `file.path`, `file.folder`, `file.size`, `file.ctime`, `file.mtime` (via `FieldResolver.FILE_FIELDS`) + frontmatter direct.

### Vues

`table` / `list` uniquement. `type: task`, `type: cards`, type inconnu → `BasesUnsupportedError` → bloc inerte (NC-2 : zéro requête TASK vivante mesurée dans le vault).

## Exclusions Phase 2 (rejet explicite → bloc inerte)

- **Formula evaluator** (`formulas:`, vues référençant `formula.*`)
- **Lambdas** (expressions fonctionnelles inline)
- **Property chains** (`value.asFile().path`, `link.file.name`, …)
- **GROUP BY** / agrégations / `summaries:`
- **FLATTEN**
- **Embeds `![[fichier.base]]`** et fichiers `.base` autonomes (fenced blocks only — NC-4)
- **Vue TASK** (retirée Phase 1 — NC-2)

Clés de présentation **ignorées sans erreur** (sans impact sémantique) : `cardSize`, `columnSize`, icônes.

## Gestion d'erreurs — bloc inerte, jamais de crash

Politique identique à `DataviewIntegration` (3 niveaux d'except). Toute erreur produit une enveloppe `status: error` avec `error_type` ∈ {`parse`, `unsupported`, `limit`, `execution`, `unexpected`} ; le bloc est **inerte** (non substitué), les autres blocs et le reste de la note rendent normalement. **Aucune exception ne remonte au handler MCP.**

## Bornes anti-DoS (chiffrées)

| Borne | Valeur | Au-delà |
|---|---|---|
| Taille d'un bloc ```base``` | 32 768 octets | inerte (`limit`) |
| Blocs ```base``` traités / note | 20 | surnuméraires inertes |
| Profondeur arbre `filters` | 10 | inerte |
| Nombre de feuilles de filtre | 50 | inerte |
| Longueur d'une expression feuille | 1 024 caractères | inerte |
| Profondeur d'AST d'expression | 20 | inerte |
| Nœuds YAML après parse | 1 000 | inerte |
| Vues déclarées | 10 (seule `views[0]` rendue) | inerte |
| Lignes rendues (cap dur sur LIMIT) | 500 (+ marqueur de troncature) | troncature |

**Chargement YAML** : loader fork-local dérivé de `yaml.SafeLoader` **interdisant anchors/aliases** (`&`/`*` → `BasesParseError`) — anti billion-laughs. Jamais `yaml.load`, jamais de constructeur custom, jamais d'`eval()`/`exec()`.

## Câblage MCP — flag `enable_bases`

| Tool | Défaut (symétrie `enable_dataview`) | Mécanisme |
|---|---|---|
| `read_note` | `True` (read_note.py:134) | `_enrich_with_bases` miroir de `_enrich_with_dataview`, section `## Bases Query Results` |
| `build_context` | `True` (build_context.py:167) | détection **on-the-fly** sur le contenu du primary result (pas de dépendance `sync_service`) |
| `search_notes` | `False` (search.py:640) | métadonnée `bases_results` par result |

- **Indépendance des flags** : chaque flag déclenche uniquement son détecteur. Une note avec un bloc ```dataview``` ET un bloc ```base``` rend les deux quand les deux flags sont actifs.
- **Dataset partagé** : même appel `list_entities_for_dataview()` — un seul fetch si les deux flags actifs sur le même appel.

## Invariants (zone sensible — input parsing + handlers MCP)

- Aucune expression hors surface Phase 1 (grammaire fermée + whitelist de 5 fonctions) n'est évaluée — refus explicite, jamais de fallback silencieux.
- Aucun code arbitraire issu du contenu de note n'est exécuté — pas d'`eval()`/`exec()`, YAML via SafeLoader fork sans anchors/aliases, aucun constructeur custom.
- Aucun bloc malformé ou hors bornes ne provoque de crash du handler MCP, d'exécution partielle, ni d'altération du rendu du reste de la note.
- Aucun dépassement d'une borne anti-DoS n'entraîne de traitement partiel — au-delà d'une borne, le bloc entier est inerte.
- Aucun fichier du core upstream n'est modifié hors du câblage des 3 flags `enable_bases`.
- L'activation de `enable_bases` ne modifie pas le comportement de `enable_dataview`, et réciproquement.
- Aucune évaluation Bases n'accède à une ressource hors du dataset `list_entities_for_dataview` — `file.inFolder` est un match de préfixe, jamais un accès filesystem.

## Relations

- implements [[products/Basic Memory/foundation/decisions/ADR-003 Bases Executor Module Design|ADR-003]]
- implements [[products/Basic Memory/milestones/M-Bases-P1/stories/US-001 Design exécuteur Bases et ADR-003|US-001]]
- references [[products/Basic Memory/foundation/decisions/ADR-002 Input Validation Strategy|ADR-002]]
