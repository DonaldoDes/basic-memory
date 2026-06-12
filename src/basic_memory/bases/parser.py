"""Parser: Bases YAML block -> validated BasesQuery AST.

Pipeline:
  1. load YAML via the fork SafeLoader (no anchors/aliases).
  2. reject Phase 2 / unsupported top-level keys (formulas, summaries).
  3. validate the views list, take views[0], map its type to ViewType
     (task/cards/unknown -> BasesUnsupportedError).
  4. compile the ``filters`` tree into:
       - a FROM source (from file.inFolder("...") leaves), and
       - a WHERE expression (compiled to Dataview AST nodes).
  5. enforce anti-DoS bounds (filter depth, leaf count, YAML node count).
"""

from typing import Any

from basic_memory.bases.errors import (
    BasesLimitError,
    BasesParseError,
    BasesUnsupportedError,
)
from basic_memory.bases.leaf_parser import parse_leaf
from basic_memory.bases.schema import (
    MAX_FILTER_DEPTH,
    MAX_FILTER_LEAVES,
    MAX_VIEWS,
    MAX_YAML_NODES,
    BasesQuery,
    BasesSortClause,
    BasesView,
    ViewType,
)
from basic_memory.bases.yaml_loader import safe_load_no_aliases
from basic_memory.dataview.ast import (
    BinaryOpNode,
    ExpressionNode,
    LiteralNode,
    SortDirection,
)

# Top-level keys that signal a Phase 2 / unsupported construct.
_UNSUPPORTED_TOP_KEYS = {"formulas", "summaries"}

# Keys recognized at the view level. Anything implying aggregation/grouping is
# rejected.
_UNSUPPORTED_VIEW_KEYS = {"groupBy", "group_by", "summaries", "formulas"}

# inFolder reference (FROM source).
_INFOLDER = "file.inFolder"


class BasesParser:
    """Parses a Bases YAML block into a validated BasesQuery."""

    @classmethod
    def parse(cls, yaml_text: str) -> BasesQuery:
        data = safe_load_no_aliases(yaml_text)

        if data is None:
            raise BasesParseError("Empty base block")
        if not isinstance(data, dict):
            raise BasesParseError("Base block must be a YAML mapping")

        cls._check_yaml_node_count(data)

        for key in _UNSUPPORTED_TOP_KEYS:
            if key in data:
                raise BasesUnsupportedError(
                    f"'{key}:' is outside the Phase 1 surface (Phase 2)"
                )

        view = cls._parse_view(data)

        from_source, where = cls._parse_filters(data.get("filters"))

        # NC-3: limit accepted at root level too; view-level wins if both set.
        if view.limit is None and "limit" in data:
            root_limit = data["limit"]
            if isinstance(root_limit, int) and not isinstance(root_limit, bool):
                view.limit = root_limit

        aliases = cls._parse_aliases(data.get("properties"))

        return BasesQuery(
            view=view,
            from_source=from_source,
            where=where,
            aliases=aliases,
        )

    # ------------------------------------------------------------------ views
    @classmethod
    def _parse_view(cls, data: dict[str, Any]) -> BasesView:
        views = data.get("views")
        if not isinstance(views, list) or len(views) == 0:
            raise BasesParseError("Base block requires a non-empty 'views' list")
        if len(views) > MAX_VIEWS:
            raise BasesLimitError(f"More than {MAX_VIEWS} views declared")

        raw = views[0]
        if not isinstance(raw, dict):
            raise BasesParseError("Each view must be a YAML mapping")

        vtype = raw.get("type")
        try:
            view_type = ViewType(vtype)
        except ValueError:
            # task / cards / unknown -> unsupported (NC-2)
            raise BasesUnsupportedError(
                f"View type '{vtype}' is outside the Phase 1 surface (table/list only)"
            )

        for key in _UNSUPPORTED_VIEW_KEYS:
            if key in raw:
                raise BasesUnsupportedError(
                    f"View key '{key}' is outside the Phase 1 surface (Phase 2)"
                )

        order = cls._parse_order(raw.get("order"))
        sort = cls._parse_sort(raw.get("sort"))

        limit = None
        if "limit" in raw:
            raw_limit = raw["limit"]
            if not isinstance(raw_limit, int) or isinstance(raw_limit, bool):
                raise BasesParseError("'limit' must be an integer")
            limit = raw_limit

        return BasesView(
            view_type=view_type,
            name=raw.get("name"),
            order=order,
            sort=sort,
            limit=limit,
        )

    @classmethod
    def _parse_order(cls, order: Any) -> list[str]:
        if order is None:
            return []
        if not isinstance(order, list):
            raise BasesParseError("'order' must be a list of field names")
        cols: list[str] = []
        for col in order:
            if not isinstance(col, str):
                raise BasesParseError("'order' entries must be strings")
            if col.startswith("formula.") or col.startswith("formulas."):
                raise BasesUnsupportedError(
                    "formula.* columns are outside the Phase 1 surface (Phase 2)"
                )
            cols.append(col)
        return cols

    @classmethod
    def _parse_sort(cls, sort: Any) -> list[BasesSortClause]:
        if sort is None:
            return []
        if not isinstance(sort, list):
            raise BasesParseError("'sort' must be a list")
        clauses: list[BasesSortClause] = []
        for entry in sort:
            if not isinstance(entry, dict) or "property" not in entry:
                raise BasesParseError("Each sort entry must have a 'property'")
            prop = entry["property"]
            if not isinstance(prop, str):
                raise BasesParseError("Sort 'property' must be a string")
            direction_raw = str(entry.get("direction", "ASC")).upper()
            if direction_raw not in ("ASC", "DESC"):
                raise BasesParseError(
                    f"Sort direction must be ASC or DESC, got {direction_raw!r}"
                )
            clauses.append(
                BasesSortClause(
                    field=prop,
                    direction=SortDirection.ASC
                    if direction_raw == "ASC"
                    else SortDirection.DESC,
                )
            )
        return clauses

    @classmethod
    def _parse_aliases(cls, properties: Any) -> dict[str, str]:
        if properties is None:
            return {}
        if not isinstance(properties, dict):
            raise BasesParseError("'properties' must be a mapping")
        aliases: dict[str, str] = {}
        for field_name, spec in properties.items():
            if isinstance(spec, dict) and "displayName" in spec:
                aliases[str(field_name)] = str(spec["displayName"])
        return aliases

    # ---------------------------------------------------------------- filters
    @classmethod
    def _parse_filters(
        cls, filters: Any
    ) -> tuple[str | None, ExpressionNode | None]:
        """Walk the filters tree, returning (from_source, where_expression).

        ``file.inFolder("x")`` leaves are extracted as the FROM source and
        removed from the WHERE expression. Remaining leaves are AND/OR/NOT
        combined per the tree structure.
        """
        if filters is None:
            return None, None

        leaf_count = [0]
        from_holder: dict[str, str] = {}
        where = cls._walk_filter(filters, depth=0, leaf_count=leaf_count, from_holder=from_holder)
        return from_holder.get("from"), where

    @classmethod
    def _walk_filter(
        cls,
        node: Any,
        depth: int,
        leaf_count: list[int],
        from_holder: dict[str, str],
    ) -> ExpressionNode | None:
        if depth > MAX_FILTER_DEPTH:
            raise BasesLimitError(
                f"Filter tree exceeds depth {MAX_FILTER_DEPTH}"
            )

        # Leaf: a string expression.
        if isinstance(node, str):
            return cls._compile_leaf(node, leaf_count, from_holder)

        if isinstance(node, dict):
            if len(node) != 1:
                raise BasesParseError(
                    "Filter conjunction must have exactly one of and/or/not"
                )
            key, value = next(iter(node.items()))
            key_l = str(key).lower()
            if key_l not in ("and", "or", "not"):
                raise BasesParseError(f"Unknown filter conjunction: {key!r}")

            if key_l == "not":
                child = cls._walk_filter(value, depth + 1, leaf_count, from_holder)
                if child is None:
                    return None
                return BinaryOpNode(operator="=", left=child, right=LiteralNode(value=False))

            if not isinstance(value, list):
                raise BasesParseError(f"'{key_l}:' must be a list of leaves")

            operator = "AND" if key_l == "and" else "OR"
            children: list[ExpressionNode] = []
            for item in value:
                child = cls._walk_filter(item, depth + 1, leaf_count, from_holder)
                if child is not None:
                    children.append(child)
            if not children:
                return None
            combined = children[0]
            for child in children[1:]:
                combined = BinaryOpNode(operator=operator, left=combined, right=child)
            return combined

        raise BasesParseError(
            f"Filter node must be a string or and/or/not mapping, got {type(node).__name__}"
        )

    @classmethod
    def _compile_leaf(
        cls,
        leaf: str,
        leaf_count: list[int],
        from_holder: dict[str, str],
    ) -> ExpressionNode | None:
        leaf_count[0] += 1
        if leaf_count[0] > MAX_FILTER_LEAVES:
            raise BasesLimitError(
                f"Filter tree exceeds {MAX_FILTER_LEAVES} leaves"
            )

        # Extract file.inFolder("...") as the FROM source BEFORE leaf parsing
        # (inFolder is not a WHERE function — it maps to FROM, a path prefix).
        infolder = cls._extract_infolder(leaf)
        if infolder is not None:
            from_holder["from"] = infolder
            return None

        return parse_leaf(leaf)

    @staticmethod
    def _extract_infolder(leaf: str) -> str | None:
        """Return the folder path if the leaf is exactly file.inFolder("...")."""
        import re

        m = re.fullmatch(
            r'\s*' + re.escape(_INFOLDER) + r'\(\s*(["\'])(.*?)\1\s*\)\s*',
            leaf,
        )
        if m:
            return m.group(2)
        return None

    # ------------------------------------------------------------------ bounds
    @classmethod
    def _check_yaml_node_count(cls, data: Any) -> None:
        count = cls._count_nodes(data)
        if count > MAX_YAML_NODES:
            raise BasesLimitError(
                f"YAML structure exceeds {MAX_YAML_NODES} nodes"
            )

    @classmethod
    def _count_nodes(cls, obj: Any) -> int:
        if isinstance(obj, dict):
            return 1 + sum(cls._count_nodes(v) for v in obj.values())
        if isinstance(obj, list):
            return 1 + sum(cls._count_nodes(v) for v in obj)
        return 1
