"""Typed schema for the Bases Phase 1 surface + anti-DoS bounds.

These dataclasses are the parser output consumed by the executor. Expression
leaves (WHERE) are compiled to the existing Dataview AST nodes
(``dataview/ast.py``) so ``ExpressionEvaluator`` can be reused unchanged.
"""

from dataclasses import dataclass, field
from enum import Enum

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

# Re-export for executor convenience.
__all__ = [
    "MAX_BLOCK_BYTES",
    "MAX_BLOCKS_PER_NOTE",
    "MAX_FILTER_DEPTH",
    "MAX_FILTER_LEAVES",
    "MAX_LEAF_EXPR_CHARS",
    "MAX_AST_DEPTH",
    "MAX_YAML_NODES",
    "MAX_VIEWS",
    "MAX_RENDERED_ROWS",
    "ViewType",
    "BasesSortClause",
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
class BasesView:
    """A rendered view (only views[0] is rendered in Phase 1)."""

    view_type: ViewType
    name: str | None = None
    order: list[str] = field(default_factory=list)
    sort: list[BasesSortClause] = field(default_factory=list)
    limit: int | None = None


@dataclass
class BasesQuery:
    """Complete parsed Bases block, ready for execution.

    Attributes:
        view: the first (rendered) view.
        from_source: path prefix derived from ``file.inFolder(...)`` (≡ FROM).
        where: filter expression compiled to Dataview AST nodes, or None.
        aliases: column display-name overrides (≡ "as Alias" in DQL).
    """

    view: BasesView
    from_source: str | None = None
    where: ExpressionNode | None = None
    aliases: dict[str, str] = field(default_factory=dict)
