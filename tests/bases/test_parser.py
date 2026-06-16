"""US-002 — YAML parsing → BasesQuery AST + leaf-expression compilation.

Covers BASES-02 (nominal), BASES-03 (malformed YAML), BASES-04 (unsupported
constructs), BASES-05 (leaf grammar + normalization), BASES-06 (compilation to
Dataview AST nodes).
"""

import pytest

from basic_memory.bases.parser import BasesParser
from basic_memory.bases.schema import BasesQuery, ViewType
from basic_memory.bases.errors import (
    BasesParseError,
    BasesUnsupportedError,
    BasesLimitError,
)
from basic_memory.dataview.ast import (
    BinaryOpNode,
    FieldNode,
    FunctionCallNode,
    LiteralNode,
)


# ---------------------------------------------------------------------------
# BASES-02 — nominal parsing
# ---------------------------------------------------------------------------
class TestNominalParsing:
    def test_minimal_table_view(self):
        query = BasesParser.parse(
            """
views:
  - type: table
    order: [file.name, status]
"""
        )
        assert isinstance(query, BasesQuery)
        assert query.view.view_type == ViewType.TABLE
        assert query.view.order == ["file.name", "status"]

    def test_list_view(self):
        query = BasesParser.parse(
            """
views:
  - type: list
"""
        )
        assert query.view.view_type == ViewType.LIST

    def test_filters_sort_limit(self):
        query = BasesParser.parse(
            """
filters:
  and:
    - status == "Active"
views:
  - type: table
    order: [file.name]
    sort:
      - property: file.mtime
        direction: DESC
    limit: 20
"""
        )
        assert query.where is not None
        assert query.view.limit == 20
        assert len(query.view.sort) == 1
        assert query.view.sort[0].field == "file.mtime"
        assert query.view.sort[0].direction.value == "DESC"

    def test_from_source_via_infolder(self):
        query = BasesParser.parse(
            """
filters:
  and:
    - file.inFolder("projects/Basic Memory")
views:
  - type: list
"""
        )
        assert query.from_source == "projects/Basic Memory"

    def test_displayname_alias(self):
        query = BasesParser.parse(
            """
views:
  - type: table
    order: [status]
properties:
  status:
    displayName: Status
"""
        )
        assert query.aliases.get("status") == "Status"

    def test_limit_accepted_at_root_level(self):
        # NC-3: limit accepted at view level AND root level
        query = BasesParser.parse(
            """
limit: 5
views:
  - type: table
    order: [file.name]
"""
        )
        assert query.view.limit == 5

    def test_single_string_filter(self):
        query = BasesParser.parse(
            """
filters: status == "Done"
views:
  - type: list
"""
        )
        assert query.where is not None

    def test_only_first_view_rendered(self):
        query = BasesParser.parse(
            """
views:
  - type: table
    order: [file.name]
  - type: list
"""
        )
        assert query.view.view_type == ViewType.TABLE


# ---------------------------------------------------------------------------
# BASES-05 / BASES-06 — leaf grammar + compilation to Dataview AST
# ---------------------------------------------------------------------------
class TestLeafCompilation:
    def _where_of(self, leaf: str):
        query = BasesParser.parse(
            f"""
filters: {leaf}
views:
  - type: list
"""
        )
        return query.where

    def test_binary_eq_normalized(self):
        node = self._where_of('status == "Active"')
        assert isinstance(node, BinaryOpNode)
        assert node.operator == "="  # == normalized to =
        assert isinstance(node.left, FieldNode)
        assert node.left.field_name == "status"
        assert isinstance(node.right, LiteralNode)
        assert node.right.value == "Active"

    def test_not_equal(self):
        node = self._where_of('status != "Done"')
        assert node.operator == "!="

    def test_comparison_operators(self):
        for op_in in ["<", ">", "<=", ">="]:
            node = self._where_of(f"priority {op_in} 3")
            assert node.operator == op_in
            assert node.right.value == 3

    def test_and_normalized(self):
        node = self._where_of('status == "Active" && priority < 3')
        assert isinstance(node, BinaryOpNode)
        assert node.operator == "AND"

    def test_or_normalized(self):
        node = self._where_of('status == "A" || status == "B"')
        assert node.operator == "OR"

    def test_not_unary(self):
        # !contains(...) — negation compiles around the comparison.
        # Quoted because YAML treats a leading '!' as a tag indicator.
        node = self._where_of('"!status.contains(\\"x\\")"')
        # represented as (contains(...) = False); just ensure no crash + node present
        assert node is not None

    def test_method_form_function(self):
        # status.contains("x") -> FunctionCallNode("contains", [FieldNode(status), Literal("x")])
        node = self._where_of('status.contains("dev")')
        assert isinstance(node, FunctionCallNode)
        assert node.function_name == "contains"
        assert isinstance(node.arguments[0], FieldNode)
        assert node.arguments[0].field_name == "status"
        assert node.arguments[1].value == "dev"

    def test_global_form_function(self):
        node = self._where_of('contains(status, "dev")')
        assert isinstance(node, FunctionCallNode)
        assert node.function_name == "contains"
        assert node.arguments[0].field_name == "status"

    def test_boolean_and_null_literals(self):
        node = self._where_of("archived == true")
        assert node.right.value is True
        node2 = self._where_of("archived == null")
        assert node2.right.value is None

    def test_dotted_field_ref(self):
        node = self._where_of('file.name == "x"')
        assert node.left.field_name == "file.name"


# ---------------------------------------------------------------------------
# BASES-03 — malformed YAML rejected cleanly
# ---------------------------------------------------------------------------
class TestMalformedYaml:
    def test_invalid_yaml_raises_parse_error(self):
        with pytest.raises(BasesParseError):
            BasesParser.parse("views: [unclosed")

    def test_tab_indentation_yaml(self):
        with pytest.raises(BasesParseError):
            BasesParser.parse("views:\n\t- type: table")

    def test_non_mapping_top_level(self):
        with pytest.raises(BasesParseError):
            BasesParser.parse("- just\n- a\n- list")

    def test_empty_yaml_raises(self):
        with pytest.raises(BasesParseError):
            BasesParser.parse("")

    def test_missing_views_raises(self):
        with pytest.raises(BasesParseError):
            BasesParser.parse('filters: status == "x"')

    def test_empty_views_list_raises(self):
        with pytest.raises(BasesParseError):
            BasesParser.parse("views: []")


# ---------------------------------------------------------------------------
# BASES-04 — constructs outside Phase 1 surface rejected explicitly
# ---------------------------------------------------------------------------
class TestUnsupportedConstructs:
    def test_formulas_accepted(self):
        # Contract change (US-004): ``formulas:`` is no longer rejected in
        # Phase 1; it is now compiled to BasesQuery.formulas (Phase 2 calculated
        # columns). Hostile formulas are still rejected by the closed-grammar
        # formula parser (see test_calculated_columns_adversarial.py).
        query = BasesParser.parse(
            """
formulas:
  total: count(rows)
views:
  - type: table
    order: [file.name, formula.total]
"""
        )
        assert "total" in query.formulas

    def test_summaries_rejected(self):
        with pytest.raises(BasesUnsupportedError):
            BasesParser.parse(
                """
summaries:
  - sum
views:
  - type: table
    order: [file.name]
"""
            )

    def test_task_view_rejected(self):
        # NC-2: task is parsed but rejected as unsupported view type
        with pytest.raises(BasesUnsupportedError):
            BasesParser.parse(
                """
views:
  - type: task
"""
            )

    def test_cards_view_rejected(self):
        with pytest.raises(BasesUnsupportedError):
            BasesParser.parse(
                """
views:
  - type: cards
"""
            )

    def test_unknown_view_type_rejected(self):
        with pytest.raises(BasesUnsupportedError):
            BasesParser.parse(
                """
views:
  - type: calendar
"""
            )

    def test_property_chain_in_filter_rejected(self):
        with pytest.raises(BasesUnsupportedError):
            BasesParser.parse(
                """
filters: value.asFile().path == "x"
views:
  - type: list
"""
            )

    def test_unknown_function_rejected(self):
        with pytest.raises(BasesUnsupportedError):
            BasesParser.parse(
                """
filters: link(file.name) == "x"
views:
  - type: list
"""
            )

    def test_formula_field_in_order_accepted(self):
        # Contract change (US-004): ``formula.*`` columns in ``order`` are now
        # accepted and resolved at projection time against BasesQuery.formulas.
        query = BasesParser.parse(
            """
formulas:
  total: count(rows)
views:
  - type: table
    order: [formula.total]
"""
        )
        assert "formula.total" in query.view.order

    def test_group_by_rejected(self):
        with pytest.raises(BasesUnsupportedError):
            BasesParser.parse(
                """
views:
  - type: table
    order: [file.name]
    groupBy: status
"""
            )


# ---------------------------------------------------------------------------
# Bounds at parse-time (subset; full DoS suite in US-006)
# ---------------------------------------------------------------------------
class TestParseBounds:
    def test_filter_depth_exceeded(self):
        # Build a nested and: tree deeper than MAX_FILTER_DEPTH (10).
        indent = "  "
        body = ""
        cur_indent = indent
        for _ in range(12):
            body += cur_indent + "and:\n"
            cur_indent += indent + "  "
        body += cur_indent + '- status == "x"\n'
        deep = "filters:\n" + body + "views:\n  - type: list\n"
        with pytest.raises((BasesLimitError, BasesParseError)):
            BasesParser.parse(deep)

    def test_leaf_expression_too_long(self):
        long_value = "x" * 1100
        with pytest.raises(BasesLimitError):
            BasesParser.parse(f'filters: status == "{long_value}"\nviews:\n  - type: list\n')
