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

from basic_memory.bases.aggregate import AGGREGATE_WHITELIST
from basic_memory.bases.errors import (
    BasesLimitError,
    BasesParseError,
    BasesUnsupportedError,
)
from basic_memory.bases.formula_parser import parse_formula
from basic_memory.bases.leaf_parser import parse_leaf
from basic_memory.bases.schema import (
    MAX_FILTER_DEPTH,
    MAX_FILTER_LEAVES,
    MAX_FORMULAS_PER_BLOCK,
    MAX_VIEWS,
    MAX_YAML_NODES,
    BasesFormula,
    BasesGroupBy,
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

# Top-level keys that signal an unsupported construct. ``formulas:`` is now
# accepted (US-004, Phase 2 calculated columns); ``summaries:`` (aggregation)
# remains out of scope until US-005.
_UNSUPPORTED_TOP_KEYS = {"summaries"}

# Keys still unsupported at the view level. ``groupBy``/``group_by``/
# ``summaries``/``flatten`` are now accepted (US-005). ``formulas`` is a
# top-level construct (US-004), never a view key.
_UNSUPPORTED_VIEW_KEYS = {"formulas"}

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
                raise BasesUnsupportedError(f"'{key}:' is outside the Phase 1 surface (Phase 2)")

        formulas = cls._parse_formulas(data.get("formulas"))

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
            formulas=formulas,
        )

    # --------------------------------------------------------------- formulas
    @classmethod
    def _parse_formulas(cls, formulas: Any) -> dict[str, BasesFormula]:
        """Compile the ``formulas:`` mapping into ``{name: BasesFormula}``.

        Each value is a formula expression string from untrusted note content;
        it is compiled by ``parse_formula`` (the closed-grammar parser, US-002),
        which is the first line of defense — any hostile construct (function
        outside the whitelist, forbidden property chain, dunder, over-long
        expression) is rejected there, making the block inert. The formula AST
        is stored, never an evaluated-at-runtime string.

        Raises:
            BasesParseError: ``formulas:`` is not a mapping of name -> string.
            BasesLimitError: more than ``MAX_FORMULAS_PER_BLOCK`` formulas, or a
                single expression over ``MAX_FORMULA_LENGTH`` (block inert).
            BasesUnsupportedError: a formula uses a construct outside the closed
                grammar (block inert).
        """
        if formulas is None:
            return {}
        if not isinstance(formulas, dict):
            raise BasesParseError("'formulas:' must be a mapping of name -> expression")
        if len(formulas) > MAX_FORMULAS_PER_BLOCK:
            raise BasesLimitError(
                f"Base block declares more than {MAX_FORMULAS_PER_BLOCK} formulas"
            )
        compiled: dict[str, BasesFormula] = {}
        for name, expr in formulas.items():
            if not isinstance(name, str):
                raise BasesParseError("Formula names must be strings")
            if not isinstance(expr, str):
                raise BasesParseError(f"Formula '{name}' must be a string expression")
            # parse_formula raises BasesLimitError / BasesParseError /
            # BasesUnsupportedError on any hostile or malformed input — the
            # block goes inert, the expression is never evaluated.
            compiled[name] = BasesFormula(name=name, ast=parse_formula(expr))
        return compiled

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

        group_by = cls._parse_group_by(raw)
        flatten = cls._parse_flatten(raw.get("flatten"))
        summaries = cls._parse_summaries(raw.get("summaries"), group_by)

        return BasesView(
            view_type=view_type,
            name=raw.get("name"),
            order=order,
            sort=sort,
            limit=limit,
            group_by=group_by,
            flatten=flatten,
            summaries=summaries,
        )

    @classmethod
    def _parse_group_by(cls, raw: dict[str, Any]) -> BasesGroupBy | None:
        """Parse ``groupBy:``/``group_by:`` — a *simple property* only (NC-4).

        The group key is a frontmatter key or a ``file.*`` field name (a plain
        string), never a formula expression in v1 (ADR-004 §4). Anything else
        (a list, a mapping) is a parse error -> block inert.
        """
        value = raw.get("groupBy", raw.get("group_by"))
        if value is None:
            return None
        if not isinstance(value, str):
            raise BasesParseError(
                "'groupBy' must be a simple property name (string) — formula "
                "expressions are not allowed as a group key (ADR-004 NC-4)"
            )
        return BasesGroupBy(field=value)

    @classmethod
    def _parse_flatten(cls, flatten: Any) -> str | None:
        """Parse ``flatten:`` — the name of a list-valued field to expand."""
        if flatten is None:
            return None
        if not isinstance(flatten, str):
            raise BasesParseError("'flatten' must be a field name (string)")
        return flatten

    @classmethod
    def _parse_summaries(
        cls, summaries: Any, group_by: BasesGroupBy | None
    ) -> dict[str, tuple[str, str]]:
        """Parse ``summaries:`` into ``{output_name: (fn_name, source_field)}``.

        Each value is a closed-form aggregate call ``fn(field)`` where ``fn`` is
        validated against the closed ``AGGREGATE_WHITELIST`` AT PARSE TIME — an
        aggregate outside the whitelist raises ``BasesUnsupportedError`` and the
        block is inert (the hostile call is never evaluated). ``summaries`` only
        make sense with a ``groupBy``; standalone summaries are unsupported.
        """
        if summaries is None:
            return {}
        if group_by is None:
            raise BasesUnsupportedError(
                "'summaries:' requires a 'groupBy:' — standalone aggregation is "
                "outside the supported surface"
            )
        if not isinstance(summaries, dict):
            raise BasesParseError("'summaries:' must be a mapping of name -> aggregate")
        parsed: dict[str, tuple[str, str]] = {}
        for name, expr in summaries.items():
            if not isinstance(name, str):
                raise BasesParseError("Summary names must be strings")
            if not isinstance(expr, str):
                raise BasesParseError(f"Summary '{name}' must be a string aggregate call")
            fn_name, source = cls._parse_aggregate_call(name, expr)
            if fn_name not in AGGREGATE_WHITELIST:
                # Hard security boundary: an aggregate outside the closed
                # whitelist makes the block inert. Never a getattr/eval dispatch.
                raise BasesUnsupportedError(
                    f"Aggregate '{fn_name}' (for summary '{name}') is not in the "
                    f"closed aggregate whitelist (block inert)"
                )
            parsed[name] = (fn_name, source)
        return parsed

    @staticmethod
    def _parse_aggregate_call(name: str, expr: str) -> tuple[str, str]:
        """Parse a closed-form ``fn(field)`` aggregate call into ``(fn, field)``.

        Only the shape ``identifier(identifier-or-field)`` is accepted — a
        regex with no evaluation. Anything else is a parse error.
        """
        import re

        m = re.fullmatch(
            r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*([A-Za-z_][\w.]*)\s*\)\s*",
            expr,
        )
        if not m:
            raise BasesParseError(
                f"Summary '{name}' must be of the form fn(field), got {expr!r}"
            )
        return m.group(1), m.group(2)

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
            # ``formula.<name>`` columns are accepted (US-004); they are
            # resolved at projection time against ``BasesQuery.formulas``.
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
                raise BasesParseError(f"Sort direction must be ASC or DESC, got {direction_raw!r}")
            clauses.append(
                BasesSortClause(
                    field=prop,
                    direction=SortDirection.ASC if direction_raw == "ASC" else SortDirection.DESC,
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
    def _parse_filters(cls, filters: Any) -> tuple[str | None, ExpressionNode | None]:
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
            raise BasesLimitError(f"Filter tree exceeds depth {MAX_FILTER_DEPTH}")

        # Leaf: a string expression.
        if isinstance(node, str):
            return cls._compile_leaf(node, leaf_count, from_holder)

        if isinstance(node, dict):
            if len(node) != 1:
                raise BasesParseError("Filter conjunction must have exactly one of and/or/not")
            key, value = next(iter(node.items()))
            key_l = str(key).lower()
            if key_l not in ("and", "or", "not"):
                raise BasesParseError(f"Unknown filter conjunction: {key!r}")

            # not: accepts a single leaf/mapping OR a list (negate the AND).
            if key_l == "not":
                if isinstance(value, list):
                    inner = cls._combine(value, "AND", depth, leaf_count, from_holder)
                else:
                    inner = cls._walk_filter(value, depth + 1, leaf_count, from_holder)
                if inner is None:
                    return None
                return BinaryOpNode(operator="=", left=inner, right=LiteralNode(value=False))

            if not isinstance(value, list):
                raise BasesParseError(f"'{key_l}:' must be a list of leaves")

            operator = "AND" if key_l == "and" else "OR"
            return cls._combine(value, operator, depth, leaf_count, from_holder)

        raise BasesParseError(
            f"Filter node must be a string or and/or/not mapping, got {type(node).__name__}"
        )

    @classmethod
    def _combine(
        cls,
        items: list,
        operator: str,
        depth: int,
        leaf_count: list[int],
        from_holder: dict[str, str],
    ) -> ExpressionNode | None:
        children: list[ExpressionNode] = []
        for item in items:
            child = cls._walk_filter(item, depth + 1, leaf_count, from_holder)
            if child is not None:
                children.append(child)
        if not children:
            return None
        combined = children[0]
        for child in children[1:]:
            combined = BinaryOpNode(operator=operator, left=combined, right=child)
        return combined

    @classmethod
    def _compile_leaf(
        cls,
        leaf: str,
        leaf_count: list[int],
        from_holder: dict[str, str],
    ) -> ExpressionNode | None:
        leaf_count[0] += 1
        if leaf_count[0] > MAX_FILTER_LEAVES:
            raise BasesLimitError(f"Filter tree exceeds {MAX_FILTER_LEAVES} leaves")

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
            r"\s*" + re.escape(_INFOLDER) + r'\(\s*(["\'])(.*?)\1\s*\)\s*',
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
            raise BasesLimitError(f"YAML structure exceeds {MAX_YAML_NODES} nodes")

    @classmethod
    def _count_nodes(cls, obj: Any) -> int:
        if isinstance(obj, dict):
            return 1 + sum(cls._count_nodes(v) for v in obj.values())
        if isinstance(obj, list):
            return 1 + sum(cls._count_nodes(v) for v in obj)
        return 1
