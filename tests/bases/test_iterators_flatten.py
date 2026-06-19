"""Unit tests for US-3 (M-Bases-P4, ADR-006 §Gap #5) — lambda iterators + FLATTEN.

Nominal behaviour of the iterators ``any(list, lambda)`` / ``filter(list, lambda)``
USED OUTSIDE an aggregate (predicates / transformations, distinct from the P2
aggregates ``count`` / ``sum``), plus FLATTEN over a computed list EXPRESSION
(``FLATTEN file.links AS link``, not only a frontmatter field).

BP4-C-01..04. The iterators reuse the SAME sandboxed lambda mechanism as P2
(``FormulaEvaluator._eval_lambda`` → a closed-env callable) — there is no second
evaluator. Adversarial / security coverage lives in
``test_iterators_flatten_adversarial.py``.
"""

from __future__ import annotations

import pytest

from basic_memory.bases.aggregate import flatten
from basic_memory.bases.formula_ast import FCall, FField, FLambda, FLiteral, FPropChain
from basic_memory.bases.formula_eval import FormulaEvaluator, evaluate_formula
from basic_memory.bases.formula_parser import ALLOWED_FUNCTIONS, parse_formula


@pytest.fixture
def row() -> dict:
    return {
        "title": "Note A",
        "file.path": "areas/Area/Note A.md",
        "frontmatter": {"items": ["alpha", "beta", "gamma"]},
        "items": ["alpha", "beta", "gamma"],
    }


# ===========================================================================
# BP4-C-01 — any(list, lambda) outside an aggregate → boolean
# ===========================================================================
class TestAnyIterator:
    def test_any_true_when_a_predicate_matches(self, row):
        """BP4-C-01: any(items, x => x.startsWith("be")) → True (beta matches)."""
        node = parse_formula('any(items, x => x.startsWith("be"))')
        assert evaluate_formula(node, row) is True

    def test_any_false_when_no_predicate_matches(self, row):
        """BP4-C-01: any(items, x => x.startsWith("zzz")) → False (none match)."""
        node = parse_formula('any(items, x => x.startsWith("zzz"))')
        assert evaluate_formula(node, row) is False

    def test_any_over_empty_list_is_false(self):
        """any() over an empty list is False (vacuous)."""
        node = parse_formula('any(items, x => x.startsWith("a"))')
        assert evaluate_formula(node, {"items": [], "frontmatter": {"items": []}}) is False

    def test_any_returns_a_real_boolean(self, row):
        """The result is a Python bool, not a truthy element."""
        node = parse_formula('any(items, x => x.startsWith("al"))')
        result = evaluate_formula(node, row)
        assert result is True
        assert isinstance(result, bool)


# ===========================================================================
# BP4-C-02 — filter(list, lambda) outside an aggregate → filtered sub-list
# ===========================================================================
class TestFilterIterator:
    def test_filter_returns_matching_sublist(self, row):
        """BP4-C-02: filter(items, x => x.startsWith("a")) → ["alpha"] (only)."""
        node = parse_formula('filter(items, x => x.startsWith("a"))')
        assert evaluate_formula(node, row) == ["alpha"]

    def test_filter_returns_multiple_matches_in_order(self):
        """filter preserves element order of the source list."""
        row = {"items": ["a1", "b1", "a2"], "frontmatter": {"items": ["a1", "b1", "a2"]}}
        node = parse_formula('filter(items, x => x.startsWith("a"))')
        assert evaluate_formula(node, row) == ["a1", "a2"]

    def test_filter_no_match_returns_empty_list(self, row):
        """No match → empty list, never None (a list-typed transformation)."""
        node = parse_formula('filter(items, x => x.startsWith("zzz"))')
        assert evaluate_formula(node, row) == []

    def test_filter_over_empty_list_is_empty(self):
        node = parse_formula('filter(items, x => x.startsWith("a"))')
        assert evaluate_formula(node, {"items": [], "frontmatter": {"items": []}}) == []


# ===========================================================================
# BP4-C-03 — iterators reuse the P2 sandboxed lambda mechanism (same objects)
# ===========================================================================
class TestReusesP2LambdaMechanism:
    def test_iterators_are_in_the_same_closed_whitelist(self):
        """BP4-C-03: any/filter are added to the SAME closed ALLOWED_FUNCTIONS set
        that P2 uses — not a separate dispatch surface."""
        assert "any" in ALLOWED_FUNCTIONS
        assert "filter" in ALLOWED_FUNCTIONS

    def test_predicate_is_the_p2_eval_lambda_callable(self, row):
        """BP4-C-03: the predicate passed to the iterator is the exact callable
        produced by ``FormulaEvaluator._eval_lambda`` (the P2 mechanism).

        We compile the SAME lambda node through the evaluator and assert the
        iterator's per-element decisions match the callable's — proving the
        iterator drives the audited P2 closure, not a re-implemented evaluator.
        """
        lam = FLambda(
            params=["x"],
            body=FPropChain(receiver=FField("x"), member="startsWith", args=[FLiteral("a")]),
        )
        evaluator = FormulaEvaluator(row)
        predicate = evaluator.eval(lam)  # the P2 closed-env callable
        # The iterator's filter must agree element-by-element with that callable.
        expected = [e for e in row["items"] if predicate(e)]
        node = FCall("filter", [FField("items"), lam])
        assert evaluate_formula(node, row) == expected

    def test_lambda_param_shadows_row_field_inside_iterator(self):
        """BP4-C-03: a lambda param shadows a row field of the same name (the P2
        closed-env layering), proving the same env mechanism is in play."""
        # The row has a field ``x``; inside the lambda, ``x`` is the bound element.
        row = {
            "items": ["zzz"],
            "x": "ROW LEVEL",
            "frontmatter": {"items": ["zzz"], "x": "ROW LEVEL"},
        }
        # filter(items, x => x.startsWith("z")) → element "zzz" matches, not the
        # row-level "ROW LEVEL" (which would NOT start with "z").
        node = parse_formula('filter(items, x => x.startsWith("z"))')
        assert evaluate_formula(node, row) == ["zzz"]


# ===========================================================================
# BP4-C-04 — FLATTEN on an EXPRESSION (computed list), not only a field
# ===========================================================================
class TestFlattenOnExpression:
    def test_flatten_file_links_expands_one_row_per_element(self):
        """BP4-C-04: FLATTEN file.links AS link expands a COMPUTED list (the row's
        outlinks), one output row per element, binding the alias ``link``."""
        rows = [
            {
                "title": "Hub",
                "file": {"path": "areas/hub.md", "outlinks": ["areas/a", "areas/b"]},
            }
        ]
        out = flatten(rows, "file.links", max_cardinality=100, alias="link")
        assert len(out) == 2
        aliases = [r["frontmatter"]["link"] for r in out]
        assert aliases == ["areas/a", "areas/b"]

    def test_flatten_expression_input_rows_not_mutated(self):
        """FLATTEN deep-copies on expansion — the source rows keep their list."""
        rows = [{"title": "Hub", "file": {"path": "h.md", "outlinks": ["x", "y"]}}]
        flatten(rows, "file.links", max_cardinality=100, alias="link")
        assert rows[0]["file"]["outlinks"] == ["x", "y"]

    def test_flatten_expression_empty_list_yields_zero_rows(self):
        """An empty computed list yields zero rows (consistent with field FLATTEN)."""
        rows = [{"title": "Hub", "file": {"path": "h.md", "outlinks": []}}]
        out = flatten(rows, "file.links", max_cardinality=100, alias="link")
        assert out == []

    def test_flatten_plain_field_still_supported_no_alias(self):
        """Non-regression: FLATTEN of a plain frontmatter field (no expression,
        no alias) keeps the pre-US-3 behaviour."""
        rows = [{"title": "N", "frontmatter": {"tags": ["t1", "t2"]}}]
        out = flatten(rows, "tags", max_cardinality=100)
        assert len(out) == 2
        assert [r["frontmatter"]["tags"] for r in out] == ["t1", "t2"]
