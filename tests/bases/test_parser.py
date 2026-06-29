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
from basic_memory.bases.filter_leaf import FormulaLeafNode
from basic_memory.bases.formula_ast import FBinOp, FCall, FField, FLiteral, FPropChain
from basic_memory.bases.ast import (
    BinaryOpNode,
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
# BASES-05 / BASES-06 — leaf grammar + compilation
#
# US-a (M-Bases-P3, ADR-005 §Axe 1) CONTRACT CHANGE: a filter leaf is no longer
# compiled to a Dataview AST node by the deprecated leaf_parser. It is routed to
# the Phase 2 formula sandbox (formula_parser) and wrapped in a FormulaLeafNode
# carrying a typed *formula* AST. The FROM/WHERE/and/or/not STRUCTURE is
# unchanged: AND/OR/NOT combinators stay Dataview BinaryOpNode; only the leaves
# are FormulaLeafNode. These tests assert the NEW contract.
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

    def test_leaf_is_formula_leaf_node(self):
        node = self._where_of('status == "Active"')
        assert isinstance(node, FormulaLeafNode)
        # The wrapped formula AST is an equality binary op (==, not normalised).
        assert isinstance(node.formula, FBinOp)
        assert node.formula.op == "=="
        assert isinstance(node.formula.left, FField)
        assert node.formula.left.name == "status"
        assert isinstance(node.formula.right, FLiteral)
        assert node.formula.right.value == "Active"

    def test_not_equal(self):
        node = self._where_of('status != "Done"')
        assert isinstance(node, FormulaLeafNode)
        assert node.formula.op == "!="

    def test_comparison_operators(self):
        for op_in in ["<", ">", "<=", ">="]:
            node = self._where_of(f"priority {op_in} 3")
            assert isinstance(node, FormulaLeafNode)
            assert node.formula.op == op_in
            assert node.formula.right.value == 3

    def test_and_keeps_dataview_binop_structure(self):
        # Combination via the `and:` mapping keeps the Dataview BinaryOpNode
        # structure; the operands are FormulaLeafNode leaves.
        query = BasesParser.parse(
            'filters:\n  and:\n    - status == "Active"\n    - priority < 3\nviews:\n  - type: list\n'
        )
        assert isinstance(query.where, BinaryOpNode)
        assert query.where.operator == "AND"
        assert isinstance(query.where.left, FormulaLeafNode)
        assert isinstance(query.where.right, FormulaLeafNode)

    def test_or_keeps_dataview_binop_structure(self):
        query = BasesParser.parse(
            'filters:\n  or:\n    - status == "A"\n    - status == "B"\nviews:\n  - type: list\n'
        )
        assert isinstance(query.where, BinaryOpNode)
        assert query.where.operator == "OR"

    def test_not_wraps_leaf_in_equality_false(self):
        # `not:` normalises to (leaf = False): a Dataview `=` BinaryOpNode whose
        # left is the FormulaLeafNode and right is LiteralNode(False).
        query = BasesParser.parse(
            'filters:\n  not:\n    - status == "Active"\nviews:\n  - type: list\n'
        )
        assert isinstance(query.where, BinaryOpNode)
        assert query.where.operator == "="
        assert isinstance(query.where.left, FormulaLeafNode)
        assert isinstance(query.where.right, LiteralNode)
        assert query.where.right.value is False

    def test_method_form_function(self):
        # status.contains("dev") -> FormulaLeafNode wrapping a property-chain.
        node = self._where_of('status.contains("dev")')
        assert isinstance(node, FormulaLeafNode)
        assert isinstance(node.formula, FPropChain)
        assert node.formula.member == "contains"

    def test_global_form_function(self):
        node = self._where_of('contains(status, "dev")')
        assert isinstance(node, FormulaLeafNode)
        assert isinstance(node.formula, FCall)
        assert node.formula.fn == "contains"

    def test_boolean_and_null_literals(self):
        node = self._where_of("archived == true")
        assert node.formula.right.value is True
        node2 = self._where_of("archived == null")
        assert node2.formula.right.value is None

    def test_dotted_field_ref(self):
        node = self._where_of('file.name == "x"')
        assert isinstance(node, FormulaLeafNode)
        assert node.formula.left.name == "file.name"


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

    def test_property_chain_in_filter_now_accepted(self):
        # CONTRACT CHANGE (US-a, ADR-005 §Axe 1): property chains in filters were
        # rejected in Phase 1 (4-function leaf_parser); they are now ROUTED to
        # the formula sandbox and resolved through the closed whitelisted member
        # dispatch (asFile/path/name/...). A valid chain compiles, no longer
        # rejected.
        query = BasesParser.parse(
            """
filters: asFile(file.path).path == "x"
views:
  - type: list
"""
        )
        assert query.where is not None

    def test_link_function_now_accepted_in_filter(self):
        # CONTRACT CHANGE (US-a): ``link`` is one of the 18 whitelisted formula
        # functions and is now available in filter position (parity filter =
        # formula = Obsidian).
        query = BasesParser.parse(
            """
filters: link(file.name) == "x"
views:
  - type: list
"""
        )
        assert query.where is not None

    def test_genuinely_unknown_function_still_rejected(self):
        # A function outside the closed 18-function whitelist remains rejected
        # (block inert) — the security boundary is preserved.
        with pytest.raises(BasesUnsupportedError):
            BasesParser.parse(
                """
filters: frobnicate(file.name) == "x"
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

    def test_group_by_accepted(self):
        # Contract change (US-005): ``groupBy:`` is now parsed into a
        # BasesGroupBy clause (simple-property key, NC-4) and wired into the
        # executor pipeline. It is no longer rejected as unsupported.
        query = BasesParser.parse(
            """
views:
  - type: table
    order: [file.name]
    groupBy: status
"""
        )
        assert query.view.group_by is not None
        assert query.view.group_by.field == "status"


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
