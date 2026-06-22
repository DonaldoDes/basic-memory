"""Unit tests for US-7 (M-Bases-P4): negation of a function-call filter leaf.

Ground-truth gap (ticket US-7): a *negated* function-call leaf in a filter —
``not file.inFolder(...)`` — failed with ``Trailing tokens in formula: '('``.
``not file.inFolder(...)`` never matched ``_extract_infolder`` (a ``fullmatch``
that ignores the ``not ``/``!`` prefix), so the whole leaf (prefix included) was
handed to the formula parser, where ``inFolder`` is NOT in the closed function
whitelist → the parser stopped at ``file`` (a field ref) and reported the
trailing ``(``.

The other negated function leaves (``!contains``, ``not startsWith`` …) already
parsed: ``inFolder`` is the only function extracted as a FROM source, so it is
the only one whose negation had no handling path. US-7 routes a negated
``file.inFolder(...)`` leaf to a per-row subtree-exclusion predicate evaluated by
the SAME closed Phase 2 sandbox — never re-admitting ``inFolder`` to the formula
whitelist, never a filesystem access.

Test IDs (ticket): BP4-G-01 / -02 / -03 / -04 / -06.
"""

from __future__ import annotations

import pytest

from basic_memory.bases.errors import BasesParseError, BasesUnsupportedError
from basic_memory.bases.filter_leaf import FormulaLeafNode
from basic_memory.bases.formula_ast import FBinOp, FLiteral
from basic_memory.bases.parser import BasesParser


def _assert_negation_leaf(node):
    """Assert ``node`` is a negated function/field leaf.

    For a function-call (or field) leaf, the negation is carried INSIDE the
    FormulaLeafNode by the closed formula grammar (``_parse_not`` normalises
    ``not x`` to ``(x == False)``) — the SAME shape the existing ``!contains`` /
    ``!part_of`` path already produces. (The parser-walk ``=``/Dataview
    ``BinaryOpNode`` shape is reserved for the ``not:`` conjunction KEY.)
    """
    assert isinstance(node, FormulaLeafNode)
    formula = node.formula
    assert isinstance(formula, FBinOp)
    assert formula.op == "=="
    assert isinstance(formula.right, FLiteral)
    assert formula.right.value is False


def _block(leaf: str, positive_from: str = "products") -> str:
    """A base block whose WHERE is a single ``leaf`` under a positive FROM.

    The positive ``file.inFolder("products")`` anchors a FROM source so the
    negated leaf under test is the only WHERE expression we compile.
    """
    return (
        "filters:\n"
        "  and:\n"
        f'    - file.inFolder("{positive_from}")\n'
        f"    - '{leaf}'\n"
        "views:\n"
        "  - type: table\n"
        "    order:\n"
        "      - file.name\n"
    )


# ---------------------------------------------------------------------------
# BP4-G-01 — not file.inFolder(...) recognised + compiled to a WHERE predicate
# ---------------------------------------------------------------------------
class TestNegatedInFolderParsing:
    @pytest.mark.parametrize(
        "leaf",
        [
            'not file.inFolder("resources/daily")',
            '!file.inFolder("resources/daily")',
            'not  file.inFolder( "resources/daily" )',
        ],
    )
    def test_negated_infolder_parses_without_trailing_tokens(self, leaf):
        """BP4-G-01: a negated inFolder leaf parses (no 'Trailing tokens')."""
        query = BasesParser.parse(_block(leaf))
        # The negated inFolder must NOT become a FROM source (it is an
        # exclusion predicate, not a positive subtree). The positive
        # file.inFolder("products") in the block is the only FROM.
        assert query.from_source == "products"
        assert query.where is not None

    def test_negated_infolder_is_a_where_negation_node(self):
        """BP4-G-01: the negated inFolder compiles to a closed negation leaf.

        ``not file.inFolder("X")`` is rewritten to a subtree-exclusion predicate
        routed through the SAME formula sandbox as every other negated function
        leaf — a FormulaLeafNode whose inner formula is ``(<subtree-test> ==
        False)`` (the closed ``_parse_not`` shape). The per-row test is a
        path-prefix check on the dataset, never a filesystem lookup.
        """
        query = BasesParser.parse(_block('not file.inFolder("resources/daily")'))
        _assert_negation_leaf(_single_leaf(query.where))

    def test_positive_infolder_still_extracted_as_from(self):
        """BP4-G-01 (non-regression): a positive file.inFolder stays the FROM."""
        body = (
            'filters: file.inFolder("products/X")\n'
            "views:\n"
            "  - type: table\n"
            "    order:\n"
            "      - file.name\n"
        )
        query = BasesParser.parse(body)
        assert query.from_source == "products/X"
        assert query.where is None


# ---------------------------------------------------------------------------
# BP4-G-02 — !contains(file.path, ...) negates without 'Trailing tokens'
# ---------------------------------------------------------------------------
class TestNegatedContainsStartsEnds:
    @pytest.mark.parametrize(
        "leaf",
        [
            'not contains(file.path, "archives")',
            '!contains(file.path, "archives")',
        ],
    )
    def test_negated_contains_parses(self, leaf):
        """BP4-G-02: negated contains parses to a closed negation leaf."""
        query = BasesParser.parse(_block(leaf))
        _assert_negation_leaf(_single_leaf(query.where))

    # -----------------------------------------------------------------------
    # BP4-G-03 — !startsWith / !endsWith negated correctly
    # -----------------------------------------------------------------------
    @pytest.mark.parametrize(
        "leaf",
        [
            'not startsWith(file.name, "_")',
            '!startsWith(file.name, "_")',
            'not endsWith(file.name, ".md")',
            '!endsWith(file.name, ".md")',
        ],
    )
    def test_negated_startswith_endswith_parses(self, leaf):
        """BP4-G-03: negated startsWith/endsWith parse to a closed negation leaf."""
        query = BasesParser.parse(_block(leaf))
        _assert_negation_leaf(_single_leaf(query.where))


# ---------------------------------------------------------------------------
# BP4-G-04 — field negation (!part_of) unchanged (non-regression P3)
# ---------------------------------------------------------------------------
class TestFieldNegationNonRegression:
    @pytest.mark.parametrize("leaf", ["!part_of", "not part_of"])
    def test_field_negation_still_parses(self, leaf):
        """BP4-G-04: a negated FIELD leaf is unchanged (P3 behaviour)."""
        query = BasesParser.parse(_block(leaf))
        _assert_negation_leaf(_single_leaf(query.where))

    def test_not_conjunction_key_unchanged(self):
        """BP4-G-04: the ``not:`` conjunction key (mapping form) is unchanged.

        The mapping ``not:`` key is handled by ``_walk_filter`` and DOES produce
        a Dataview ``BinaryOpNode(operator="=", right=False)`` — distinct from a
        negated string leaf. This shape must stay intact (non-regression).
        """
        from basic_memory.dataview.ast import BinaryOpNode, LiteralNode

        body = (
            "filters:\n"
            "  and:\n"
            '    - file.inFolder("products")\n'
            "    - not:\n"
            '        - contains(file.path, "archives")\n'
            "views:\n"
            "  - type: table\n"
            "    order:\n"
            "      - file.name\n"
        )
        query = BasesParser.parse(body)
        node = _single_leaf(query.where)
        assert isinstance(node, BinaryOpNode)
        assert node.operator == "="
        assert isinstance(node.right, LiteralNode)
        assert node.right.value is False


# ---------------------------------------------------------------------------
# BP4-G-06 — security: no new whitelisted function; inFolder never admitted
# ---------------------------------------------------------------------------
class TestDispatchStaysClosed:
    def test_infolder_not_added_to_formula_whitelist(self):
        """BP4-G-06: ``inFolder`` is still NOT a formula function (closed dispatch).

        The negated-inFolder path must NOT re-admit ``inFolder`` to the formula
        grammar. A bare ``inFolder(...)`` leaf (not extracted as FROM, not a
        recognised negation) must still be refused by the closed parser.
        """
        from basic_memory.bases.formula_parser import ALLOWED_FUNCTIONS

        assert "inFolder" not in ALLOWED_FUNCTIONS

    def test_bare_infolder_function_in_where_still_unsupported(self):
        """BP4-G-06: a bare ``inFolder(...)`` (no ``file.``) reaching the formula
        path stays inert — the negation fix does not admit ``inFolder`` to the
        whitelist via any route.
        """
        body = 'filters: inFolder("a")\nviews:\n  - type: table\n    order:\n      - file.name\n'
        with pytest.raises((BasesParseError, BasesUnsupportedError)):
            BasesParser.parse(body)

    def test_negated_unknown_function_leaf_refused(self):
        """BP4-G-06: ``not evilFunc(...)`` is refused (closed whitelist)."""
        with pytest.raises((BasesParseError, BasesUnsupportedError)):
            BasesParser.parse(_block('not evilFunc(file.path, "x")'))

    def test_negated_infolder_dunder_argument_refused(self):
        """BP4-G-06: a dunder smuggled into a negated inFolder is refused.

        The folder literal is a plain string; a hostile attempt to inject a
        property walk / dunder instead of a quoted path must make the block
        inert, never be evaluated.
        """
        with pytest.raises((BasesParseError, BasesUnsupportedError)):
            BasesParser.parse(_block("not file.inFolder(__class__)"))


# ---------------------------------------------------------------------------
# helper
# ---------------------------------------------------------------------------
def _single_leaf(where):
    """Extract the single remaining WHERE leaf from an AND that dropped FROM.

    The block wraps the negated leaf in ``and: [file.inFolder("products"),
    <leaf>]``. The positive inFolder is extracted as the FROM source (returns
    None from the walk), so the AND collapses to just ``<leaf>``. When the block
    has no positive FROM wrapper, ``where`` IS the leaf.
    """
    return where
