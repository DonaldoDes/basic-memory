"""Typed schema for the Bases Phase 1 surface + anti-DoS bounds.

These dataclasses are the parser output consumed by the executor. Expression
leaves (WHERE) are compiled to the existing Dataview AST nodes
(``dataview/ast.py``) so ``ExpressionEvaluator`` can be reused unchanged.
"""

from dataclasses import dataclass, field
from enum import Enum

from basic_memory.bases.formula_ast import FormulaNode
from basic_memory.dataview.ast import ExpressionNode, SortDirection

# ---------------------------------------------------------------------------
# Anti-DoS bounds (ADR-003 §3.4) — chiffrées
# ---------------------------------------------------------------------------
MAX_BLOCK_BYTES = 32_768
MAX_BLOCKS_PER_NOTE = 20
MAX_FILTER_DEPTH = 10
MAX_FILTER_LEAVES = 50
MAX_LEAF_EXPR_CHARS = 1_024
MAX_AST_DEPTH = 20
MAX_YAML_NODES = 1_000
MAX_VIEWS = 10
MAX_RENDERED_ROWS = 500

# ---------------------------------------------------------------------------
# Phase 2 aggregation bounds (ADR-004 §2.(d), US-001 NC-3) — FIGÉES.
#
# MAX_GROUP_BY_GROUPS: maximum number of distinct groups kept after GROUP BY.
#   Crossing it is a *partial render*: the first MAX_GROUP_BY_GROUPS groups are
#   rendered and a visible truncation marker is appended ("N groupes non
#   affichés") — ADR-004 §2.(d) "Troncature + marqueur". NOT inert.
#
# MAX_FLATTEN_CARDINALITY: maximum expansion of a single row by FLATTEN. A row
#   whose list field expands beyond this bound makes the *block inert*
#   (BasesLimitError -> error_type "limit") — ADR-004 §2.(d) "Bloc inerte",
#   distinct from GROUP BY which truncates. Both values are FIGÉES in US-001.
# ---------------------------------------------------------------------------
MAX_GROUP_BY_GROUPS = 500
MAX_FLATTEN_CARDINALITY = 100

# ---------------------------------------------------------------------------
# Phase 3 aggregate-only bound (US-d / ADR-005 §Axe 4) — PROVISOIRE (NC-4).
#
# MAX_AGG_ONLY_GROUPS: maximum number of distinct groups an *aggregate-only*
#   view (``aggregate: true`` — one row per group, no items) may render. Unlike
#   the grouped item view (``MAX_GROUP_BY_GROUPS``, which TRUNCATES + marker),
#   an aggregate-only view over the bound is made INERT (BasesLimitError ->
#   error_type "limit"): the aggregate-only table is the per-milestone
#   Progression surface, where a silent truncation would mislead a reader into
#   thinking the rollup is complete. Inert + visible error is safer than a
#   partial rollup. Value mirrors MAX_GROUP_BY_GROUPS (500) — recalibrer sur
#   baseline réelle (NC-4, ADR-005 §Axe 4).
# ---------------------------------------------------------------------------
MAX_AGG_ONLY_GROUPS = 500

# ---------------------------------------------------------------------------
# Phase 2 formula bounds (ADR-004 §2.(d)) — anti-DoS for formula parsing.
# MAX_AST_DEPTH (20) above is reused as the formula AST depth bound (ADR-004
# fixes formula AST depth = 20, identical to the Phase 1 value).
# PROVISOIRE — à recalibrer lors de US-003/US-005 (ADR-004 §2.(d))
# ---------------------------------------------------------------------------
MAX_FORMULA_LENGTH = 1_024

# Maximum number of calculated formulas declared in a single base block
# (ADR-004 §2.(d) — table "Nombre de formules par bloc" = 20, FIGÉE / dérivée
# de l'inspection US-001). Crossing it makes the block inert (error_type
# "limit"): a block over the bound is refused at parse time, never executed.
MAX_FORMULAS_PER_BLOCK = 20

# ---------------------------------------------------------------------------
# Phase 2 formula *evaluation* bounds (ADR-004 §2.(d)) — anti-DoS for the
# sandboxed tree-walk interpreter (formula_eval.py, US-003). These bound the
# evaluation itself (not parsing): how deep the tree-walk may recurse, how many
# total node evaluations a block may perform, and the wall-clock budget per
# block. Crossing any of them makes the block inert with error_type "limit".
#
# PROVISOIRE — à recalibrer sur baseline réelle US-003/US-005 (ADR-004 §2.(d)
# marks these three values as provisional; MAX_FORMULA_LENGTH above is figée).
# ---------------------------------------------------------------------------
MAX_FORMULA_RECURSION = 20  # PROVISOIRE — recalibrer (ADR-004 §2.(d))
MAX_FORMULA_ITERATIONS = 100_000  # PROVISOIRE — recalibrer (ADR-004 §2.(d))
FORMULA_BUDGET_MS = 1_000  # PROVISOIRE — recalibrer (ADR-004 §2.(d))

# Upper bound on the *result size* of any single amplifying operation
# (str/list repetition `*`, str/list concatenation `+`). The projected size is
# computed BEFORE allocation; crossing it raises BasesLimitError (block inert).
# This closes the memory-allocation bomb class: the time/iteration/recursion
# bounds above do NOT cap how large a single allocation may be, so a payload
# like  s * 10**6  (1 KiB content × 1e6) would allocate ~1 GiB in one node and
# OOM the MCP host before any other bound fires.
#
# Unit: characters for strings, elements for lists. 100_000 is chosen as a
# generous ceiling for any legitimate display formula (a rendered cell far
# below this stays usable) while keeping a single result allocation in the
# ~100 KB / 100k-element range — orders of magnitude below an OOM. NOT derived
# from a measured baseline.
# PROVISOIRE — recalibrer (ADR-004 §2.(d))
MAX_FORMULA_RESULT_SIZE = 100_000  # PROVISOIRE — recalibrer (ADR-004 §2.(d))

# Re-export for executor convenience.
__all__ = [
    "MAX_BLOCK_BYTES",
    "MAX_BLOCKS_PER_NOTE",
    "MAX_FILTER_DEPTH",
    "MAX_FILTER_LEAVES",
    "MAX_LEAF_EXPR_CHARS",
    "MAX_AST_DEPTH",
    "MAX_FORMULA_LENGTH",
    "MAX_FORMULAS_PER_BLOCK",
    "MAX_FORMULA_RECURSION",
    "MAX_FORMULA_ITERATIONS",
    "FORMULA_BUDGET_MS",
    "MAX_FORMULA_RESULT_SIZE",
    "MAX_YAML_NODES",
    "MAX_VIEWS",
    "MAX_RENDERED_ROWS",
    "MAX_GROUP_BY_GROUPS",
    "MAX_FLATTEN_CARDINALITY",
    "MAX_AGG_ONLY_GROUPS",
    "ViewType",
    "BasesSortClause",
    "BasesFormula",
    "BasesGroupBy",
    "BasesView",
    "BasesQuery",
]


class ViewType(Enum):
    """Supported Bases view types in Phase 1.

    Only TABLE and LIST are supported. task/cards/unknown are rejected as
    BasesUnsupportedError (NC-2: zero live TASK queries measured).
    """

    TABLE = "table"
    LIST = "list"


@dataclass
class BasesSortClause:
    """A single sort directive (property + direction)."""

    field: str
    direction: SortDirection = SortDirection.ASC


@dataclass
class BasesFormula:
    """A compiled calculated column (Phase 2, ADR-004 §4, §6).

    Attributes:
        name: the column name (the key under ``formulas:``).
        ast: the formula AST compiled by ``formula_parser`` — never an
            evaluated-at-runtime string. The executor walks this AST in the
            sandbox (``formula_eval``), never ``eval``/``exec``.
    """

    name: str
    ast: FormulaNode


@dataclass
class BasesGroupBy:
    """A GROUP BY clause (Phase 2, ADR-004 §4, NC-4).

    In Phase 2 v1 the group key is a *simple property* (frontmatter or
    ``file.*``) — never a formula expression. Declared here so the schema is
    complete; the executor wiring lands in US-005.
    """

    field: str


@dataclass
class BasesView:
    """A rendered view (only views[0] is rendered in Phase 1)."""

    view_type: ViewType
    name: str | None = None
    order: list[str] = field(default_factory=list)
    sort: list[BasesSortClause] = field(default_factory=list)
    limit: int | None = None
    # Phase 2 (ADR-004 §4, US-005) — GROUP BY / FLATTEN / summaries.
    group_by: BasesGroupBy | None = None
    flatten: str | None = None
    # summaries: {output_name: (aggregate_fn_name, source_field)}. The fn name
    # is validated against the closed AGGREGATE_WHITELIST at parse time; an
    # aggregate outside the whitelist makes the block inert (unsupported). Never
    # an evaluated string — a closed dict-dispatch over pure list functions.
    summaries: dict[str, tuple[str, str]] = field(default_factory=dict)
    # Phase 3 (US-d / ADR-005 §Axe 4) — CONDITIONAL aggregates. A summary whose
    # argument is a *predicate expression* (e.g. ``count(status == "Done")``)
    # rather than a plain field. The predicate is compiled to a formula AST by
    # ``parse_formula`` (closed grammar) and evaluated in the Phase 2 SANDBOX per
    # row of the group — count/sum of the rows whose predicate is truthy. Never
    # eval/exec. Keyed by output_name → (aggregate_fn_name, predicate_ast).
    summary_predicates: dict[str, tuple[str, "FormulaNode"]] = field(default_factory=dict)
    # Phase 3 (US-d / ADR-005 §Axe 4) — POST-AGGREGATION formulas (cross-summary
    # arithmetic). Keyed by output_name → formula AST referencing summary names
    # by identifier (e.g. ``round(Done / Total * 100)``). Evaluated in the Phase 2
    # sandbox AFTER the group summaries are computed, against a synthetic row =
    # the group's summary dict. Division by zero degrades the cell to None (the
    # sandbox raises BasesExecutionError on ``/0`` → safe_evaluate maps to None).
    agg_formulas: dict[str, "FormulaNode"] = field(default_factory=dict)
    # Phase 3 (US-d / ADR-005 §Axe 4) — aggregate-only view flag. When True the
    # view renders ONE row per group (the summary/agg-formula values), WITHOUT
    # the group's row-items — distinct from the Phase 2 grouped view that renders
    # items + a summary line.
    aggregate_only: bool = False


@dataclass
class BasesQuery:
    """Complete parsed Bases block, ready for execution.

    Attributes:
        view: the first (rendered) view.
        from_source: path prefix derived from ``file.inFolder(...)`` (≡ FROM).
        where: filter expression compiled to Dataview AST nodes, or None.
        aliases: column display-name overrides (≡ "as Alias" in DQL).
        formulas: calculated columns (``formula.<name>``) keyed by name.
    """

    view: BasesView
    from_source: str | None = None
    where: ExpressionNode | None = None
    aliases: dict[str, str] = field(default_factory=dict)
    # Phase 2 (ADR-004 §4) — calculated columns keyed by name; each value is a
    # BasesFormula whose ``ast`` is evaluated in the sandbox during projection.
    formulas: dict[str, "BasesFormula"] = field(default_factory=dict)
