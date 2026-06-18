"""Bases Phase 2 aggregation layer — FLATTEN / GROUP BY / summaries.

US-005 / ADR-004 §3 (pipeline ``filter -> flatten -> group_by -> aggregate``).

Security invariant (ADR-004, constitution "Zones sensibles"):
    Aggregates are **pure Python functions over lists**. There is NO eval/exec,
    NO getattr/setattr on values from note content, NO ``__import__`` and NO
    dynamic dispatch. Aggregate resolution is a **closed dict lookup**
    (``AGGREGATE_WHITELIST``): a name absent from the dict raises
    ``BasesUnsupportedError`` (block inert) — never a getattr on builtins, never
    a fallback. This holds even when the requested name *is* a real Python
    builtin (``open``, ``compile``, ``eval``…): only the whitelist keys resolve.

Anti-DoS bounds (US-001 NC-3, FIGÉES — ADR-004 §2.(d)):
    * FLATTEN over ``MAX_FLATTEN_CARDINALITY`` per row -> block inert
      (``BasesLimitError`` -> error_type "limit").
    * GROUP BY over ``MAX_GROUP_BY_GROUPS`` distinct groups -> *truncation* with
      a visible marker (partial render allowed), NOT inert.

GROUP BY key (US-001 NC-4): a *simple property* only — a frontmatter key or a
``file.*`` field — never a formula expression. Resolved via the shared
``FieldResolver`` (dataset only, no filesystem access).
"""

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable

from basic_memory.bases.errors import BasesLimitError, BasesUnsupportedError
from basic_memory.dataview.executor.field_resolver import FieldResolver

# ---------------------------------------------------------------------------
# Closed aggregate whitelist (US-001 NC-1 + US-005 DoD).
#
# Each entry is a PURE function over a list of (non-None) values collected from
# the rows of a group. They never touch the filesystem, builtins, or globals.
# The dict is the ONLY dispatch surface: ``AGGREGATE_WHITELIST[name]`` with a
# KeyError translated to BasesUnsupportedError — no getattr, no eval.
# ---------------------------------------------------------------------------


def _agg_count(values: list[Any]) -> int:
    return len(values)


def _agg_sum(values: list[Any]) -> Any:
    # Pure numeric sum over the collected values. A non-numeric value raises
    # TypeError, caught by the caller and surfaced as an execution-level
    # degradation (never a crash of the MCP handler).
    return sum(values)


def _agg_average(values: list[Any]) -> Any:
    if not values:
        return None
    return sum(values) / len(values)


def _agg_min(values: list[Any]) -> Any:
    return min(values) if values else None


def _agg_max(values: list[Any]) -> Any:
    return max(values) if values else None


def _agg_list(values: list[Any]) -> list[Any]:
    # Identity over the collected values (preserves order).
    return list(values)


AGGREGATE_WHITELIST: dict[str, Callable[[list[Any]], Any]] = {
    "count": _agg_count,
    "sum": _agg_sum,
    "average": _agg_average,
    "min": _agg_min,
    "max": _agg_max,
    "list": _agg_list,
}


# ---------------------------------------------------------------------------
# Group result contract
# ---------------------------------------------------------------------------
@dataclass
class GroupResult:
    """Result of a GROUP BY, ordered and bounded.

    Attributes:
        groups: ordered list of ``(key, rows)`` pairs. Insertion order of the
            first row of each key is preserved (stable, predictable render).
        truncated: True if more than ``MAX_GROUP_BY_GROUPS`` distinct groups
            existed and the tail was dropped.
        total_groups: number of distinct groups *before* truncation.
        omitted: number of groups dropped by truncation (``total - kept``).
    """

    groups: list[tuple[Any, list[dict[str, Any]]]]
    truncated: bool = False
    total_groups: int = 0
    omitted: int = 0


@dataclass
class AggregatedGroup:
    """A group with its rows and computed summary values.

    Attributes:
        key: the group key (the GROUP BY field value).
        rows: the rows of the group.
        summary: ``{output_name: aggregated_value}`` for each summary clause.
    """

    key: Any
    rows: list[dict[str, Any]]
    summary: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# FLATTEN
# ---------------------------------------------------------------------------
def flatten(
    rows: list[dict[str, Any]], field_name: str, max_cardinality: int
) -> list[dict[str, Any]]:
    """Expand each row over the list-valued frontmatter ``field_name``.

    For a row whose ``field_name`` is a list, emit one row per element (a deep
    copy with that element substituted as a scalar). Scalar or missing values
    yield a single unchanged row. An empty list yields zero rows.

    Anti-DoS (ADR-004 §2.(d), FIGÉE): if a single row expands beyond
    ``max_cardinality`` elements, the whole block is **inert** — a
    ``BasesLimitError`` is raised (caught by the integration envelope as
    error_type "limit"). This is intentionally *not* a per-row truncation: a
    hostile note must not silently drop data.

    The input rows are never mutated (deep-copied on expansion).
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        value = _read_frontmatter_field(row, field_name)
        if not isinstance(value, list):
            # scalar or missing -> single unchanged row (no copy needed)
            out.append(row)
            continue
        if len(value) > max_cardinality:
            raise BasesLimitError(
                f"FLATTEN of '{field_name}' expands a row to {len(value)} "
                f"elements, over the bound of {max_cardinality}"
            )
        for element in value:
            new_row = deepcopy(row)
            new_row.setdefault("frontmatter", {})[field_name] = element
            out.append(new_row)
    return out


# ---------------------------------------------------------------------------
# GROUP BY
# ---------------------------------------------------------------------------
def group_by(rows: list[dict[str, Any]], field_name: str) -> GroupResult:
    """Group ``rows`` by the value of the simple property ``field_name``.

    The key is resolved via ``FieldResolver`` (frontmatter or ``file.*`` — a
    simple property only, NC-4). Insertion order of groups is preserved. An
    unhashable value (e.g. a list) is keyed by a stable string surrogate so the
    grouping never crashes.

    Anti-DoS (ADR-004 §2.(d), FIGÉE): beyond ``MAX_GROUP_BY_GROUPS`` distinct
    groups the tail is dropped and ``truncated``/``omitted`` are set so the
    renderer can append a visible marker (partial render — NOT inert).
    """
    from basic_memory.bases.schema import MAX_GROUP_BY_GROUPS

    ordered: dict[Any, list[dict[str, Any]]] = {}
    for row in rows:
        raw_key = FieldResolver.resolve_field(row, field_name)
        key = _hashable_key(raw_key)
        ordered.setdefault(key, []).append(row)

    total = len(ordered)
    items = list(ordered.items())
    if total > MAX_GROUP_BY_GROUPS:
        kept = items[:MAX_GROUP_BY_GROUPS]
        return GroupResult(
            groups=kept,
            truncated=True,
            total_groups=total,
            omitted=total - MAX_GROUP_BY_GROUPS,
        )
    return GroupResult(groups=items, truncated=False, total_groups=total, omitted=0)


# ---------------------------------------------------------------------------
# AGGREGATE / summaries
# ---------------------------------------------------------------------------
def aggregate(
    group_result: GroupResult,
    summaries: dict[str, tuple[str, str]],
    whitelist: dict[str, Callable[[list[Any]], Any]],
) -> list[AggregatedGroup]:
    """Apply whitelisted aggregates to each group.

    Args:
        group_result: output of ``group_by``.
        summaries: ``{output_name: (aggregate_fn_name, source_field)}``.
        whitelist: the closed aggregate dict (``AGGREGATE_WHITELIST``).

    Returns:
        one ``AggregatedGroup`` per group, in group order.

    Raises:
        BasesUnsupportedError: an aggregate name is absent from ``whitelist``.
            The block is made inert — this is the hard security boundary: a
            name is resolved by a closed dict lookup ONLY, never getattr/eval.
    """
    # Validate every requested aggregate against the closed whitelist FIRST, so
    # an unsupported function makes the block inert before any value is touched.
    for output_name, (fn_name, _source) in summaries.items():
        if fn_name not in whitelist:
            raise BasesUnsupportedError(
                f"Aggregate '{fn_name}' (for '{output_name}') is not in the "
                f"closed aggregate whitelist"
            )

    aggregated: list[AggregatedGroup] = []
    for key, group_rows in group_result.groups:
        summary: dict[str, Any] = {}
        for output_name, (fn_name, source) in summaries.items():
            fn = whitelist[fn_name]  # closed dict lookup — never getattr/eval
            values = _collect_values(group_rows, source)
            try:
                summary[output_name] = fn(values)
            except (TypeError, ValueError):
                # Degradation (ADR-004 §2.(e)): a heterogeneous/invalid input to
                # a pure aggregate degrades the cell to None, never crashes.
                summary[output_name] = None
        aggregated.append(
            AggregatedGroup(key=key, rows=group_rows, summary=summary)
        )
    return aggregated


# ---------------------------------------------------------------------------
# Internal helpers (pure, no I/O)
# ---------------------------------------------------------------------------
def _read_frontmatter_field(row: dict[str, Any], field_name: str) -> Any:
    """Read a frontmatter (or top-level) field for FLATTEN without FieldResolver.

    FLATTEN targets a list-valued *property* of the note; we read it directly
    from ``frontmatter`` (then the row root) so we can detect list-ness. No
    ``file.*`` resolution and no filesystem access.
    """
    frontmatter = row.get("frontmatter", {})
    if isinstance(frontmatter, dict) and field_name in frontmatter:
        return frontmatter[field_name]
    if field_name in row:
        return row[field_name]
    return None


def _collect_values(rows: list[dict[str, Any]], source: str) -> list[Any]:
    """Collect non-None values of ``source`` across ``rows`` (dataset only)."""
    values: list[Any] = []
    for row in rows:
        value = FieldResolver.resolve_field(row, source)
        if value is not None:
            values.append(value)
    return values


def _hashable_key(value: Any) -> Any:
    """Return a hashable, stable group key for an arbitrary value.

    Hashable scalars pass through unchanged (preserving ``None``). Unhashable
    values (lists/dicts) are mapped to a stable string surrogate so grouping
    never raises ``TypeError: unhashable type``.
    """
    try:
        hash(value)
        return value
    except TypeError:
        return repr(value)
