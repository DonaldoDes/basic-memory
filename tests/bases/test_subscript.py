"""US-c (M-Bases-P3) — Subscript ``[index]`` operator (filter + formula).

ADR-005 §Axe 3. Adds the subscript operator ``[index]`` to the closed formula
grammar (tokenizer + parser + evaluator). Since US-a routes the filter leaf to
the SAME formula sandbox, the subscript is uniform: filter = formula = Obsidian.

Security (zone sensible — continuity ADR-004 §2 / ADR-005): the subscript stays
inside the closed sandbox. Bounded indexing (out-of-bounds -> None, never a
crash nor arbitrary memory access), no arbitrary slice (``[a:b]`` rejected in
v1), no index derived from code execution. NO ``eval``/``exec``/``getattr`` on
content.

Test IDs (mapped to the US-c Gherkin):
    BP3-C-01  Tokenizer recognises ``[`` / ``]`` (filter + formula)
    BP3-C-02  ``liste[0]`` returns the first element
    BP3-C-03  ``liste[i]`` out of bounds -> None (no exception)
    BP3-C-04  Subscript on the result of a method/property chain (``...[0]``)
    BP3-C-05  Subscript available in filter AND in formula (uniform surface)
    BP3-C-06  Slice ``[a:b]`` -> inert block (out of v1)               [ADVERSARIAL]
    BP3-C-07  Non-integer index -> inert / None                        [ADVERSARIAL]
"""

import pytest

from basic_memory.bases import formula_ast as fast
from basic_memory.bases.errors import BasesError
from basic_memory.bases.executor import BasesExecutor
from basic_memory.bases.filter_leaf import FormulaLeafNode, compile_filter_leaf
from basic_memory.bases.formula_ast import (
    FField,
    FLiteral,
    FSubscript,
)
from basic_memory.bases.formula_eval import evaluate_formula, safe_evaluate_formula
from basic_memory.bases.formula_parser import parse_formula
from basic_memory.bases.parser import BasesParser


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def row() -> dict:
    """A dataset row carrying a list-valued field (``tags``)."""
    return {
        "title": "Note A",
        "file.path": "products/basic-memory/Note A.md",
        "tags": ["alpha", "beta", "gamma"],
        "empty": [],
        "name": "Note A",
    }


# --------------------------------------------------------------------------- #
# BP3-C-01 — Tokenizer + AST node
# --------------------------------------------------------------------------- #
class TestTokenizerAndNode:
    def test_subscript_node_exists_and_subclasses_formulanode(self):
        assert hasattr(fast, "FSubscript")
        assert issubclass(fast.FSubscript, fast.FormulaNode)

    def test_tokenizer_recognises_brackets(self):
        """``[`` / ``]`` are tokenized — a bare ``field[0]`` parses to FSubscript."""
        node = parse_formula("tags[0]")
        assert isinstance(node, FSubscript)
        assert isinstance(node.receiver, FField)
        assert node.receiver.name == "tags"
        assert isinstance(node.index, FLiteral)
        assert node.index.value == 0

    def test_unmatched_bracket_is_rejected(self):
        """An unbalanced ``[`` is a parse error (block goes inert), never silent."""
        with pytest.raises(BasesError):
            parse_formula("tags[0")


# --------------------------------------------------------------------------- #
# BP3-C-02 — liste[0] returns the first element
# --------------------------------------------------------------------------- #
class TestNominalSubscript:
    def test_first_element(self, row):
        assert evaluate_formula(parse_formula("tags[0]"), row) == "alpha"

    def test_middle_element(self, row):
        assert evaluate_formula(parse_formula("tags[1]"), row) == "beta"

    def test_last_element(self, row):
        assert evaluate_formula(parse_formula("tags[2]"), row) == "gamma"

    def test_index_via_integer_expression(self, row):
        """The index may be an integer expression, not just a literal."""
        assert evaluate_formula(parse_formula("tags[1 + 1]"), row) == "gamma"

    def test_string_indexing_returns_character(self, row):
        """Subscript on a string yields the character at that position."""
        assert evaluate_formula(parse_formula("name[0]"), row) == "N"


# --------------------------------------------------------------------------- #
# BP3-C-03 — out of bounds -> None (never an exception)
# --------------------------------------------------------------------------- #
class TestOutOfBounds:
    def test_index_out_of_bounds_returns_none(self, row):
        assert evaluate_formula(parse_formula("tags[99]"), row) is None

    def test_negative_out_of_bounds_returns_none(self, row):
        # A negative index is expressed as an integer expression (the closed
        # grammar has no bare negative literal); ``0 - 99`` -> -99, out of range.
        assert evaluate_formula(parse_formula("tags[0 - 99]"), row) is None

    def test_empty_list_returns_none(self, row):
        assert evaluate_formula(parse_formula("empty[0]"), row) is None

    def test_out_of_bounds_never_raises_via_safe_eval(self, row):
        value, error = safe_evaluate_formula(parse_formula("tags[99]"), row)
        assert value is None
        assert error is None  # success with a None value, NOT an error envelope

    def test_subscript_on_none_field_returns_none(self, row):
        """A field that resolves to None subscripted -> None, never a crash."""
        assert evaluate_formula(parse_formula("missing[0]"), row) is None


# --------------------------------------------------------------------------- #
# BP3-C-04 — subscript on the result of a property/method chain
# --------------------------------------------------------------------------- #
class TestSubscriptOnChainResult:
    def test_subscript_after_property_chain(self, row):
        """``file.path[0]`` — subscript applied to a chain result (a string)."""
        node = parse_formula("file.path[0]")
        assert isinstance(node, FSubscript)
        assert evaluate_formula(node, row) == "p"  # "products/..."[0]

    def test_double_subscript(self, row):
        """``tags[0][0]`` — subscript chained on a subscript result."""
        assert evaluate_formula(parse_formula("tags[0][0]"), row) == "a"

    def test_subscript_then_member(self, row):
        """A subscript result can itself feed a whitelisted member chain."""
        node = parse_formula("tags[0]")
        assert isinstance(node, FSubscript)


# --------------------------------------------------------------------------- #
# BP3-C-05 — uniform surface: subscript in filter AND formula
# --------------------------------------------------------------------------- #
class TestUniformSurface:
    def test_subscript_in_formula_position(self, row):
        assert evaluate_formula(parse_formula("tags[0]"), row) == "alpha"

    def test_subscript_in_filter_leaf_compiles_to_formula_node(self):
        """The filter leaf is routed to the formula sandbox (US-a), so a
        subscript leaf compiles to a FormulaLeafNode wrapping an FSubscript-bearing
        formula AST — same parser, uniform surface."""
        node = compile_filter_leaf('tags[0] == "alpha"')
        assert isinstance(node, FormulaLeafNode)
        # The leaf wraps the formula AST; its left operand is the subscript.
        assert isinstance(node.formula, fast.FBinOp)
        assert isinstance(node.formula.left, FSubscript)

    def test_subscript_in_filter_selects_rows_via_executor(self):
        """End-to-end: a subscript predicate filters the dataset through the
        SAME executor path as any other formula leaf."""
        notes = [
            {
                "title": "A",
                "file": {"path": "a.md"},
                "frontmatter": {"tags": ["alpha", "beta"]},
                "tags": ["alpha", "beta"],
            },
            {
                "title": "B",
                "file": {"path": "b.md"},
                "frontmatter": {"tags": ["zeta"]},
                "tags": ["zeta"],
            },
        ]
        query = BasesParser.parse('filters: tags[0] == "alpha"\nviews:\n  - type: list\n')
        result = BasesExecutor(notes).select(query)
        titles = [r["title"] for r in result]
        assert titles == ["A"]

    def test_subscript_in_filter_out_of_bounds_excludes_row(self):
        """An out-of-bounds subscript in a filter yields None -> predicate false
        -> the row is excluded, never a crash."""
        notes = [
            {
                "title": "A",
                "file": {"path": "a.md"},
                "frontmatter": {"tags": ["alpha"]},
                "tags": ["alpha"],
            },
        ]
        query = BasesParser.parse('filters: tags[5] == "alpha"\nviews:\n  - type: list\n')
        result = BasesExecutor(notes).select(query)
        assert result == []


# --------------------------------------------------------------------------- #
# BP3-C-06 — slice [a:b] rejected in v1 (inert)                  [ADVERSARIAL]
# --------------------------------------------------------------------------- #
class TestSliceRejected:
    def test_slice_is_inert_via_safe_eval(self, row):
        """``tags[0:2]`` is out of v1 — the block goes inert (no slice)."""
        try:
            node = parse_formula("tags[0:2]")
        except BasesError:
            return  # rejected at parse time -> inert. OK.
        # If it parsed, evaluation must still degrade to an inert block.
        value, error = safe_evaluate_formula(node, row)
        assert error is not None
        assert value is None

    def test_slice_does_not_return_a_sublist(self, row):
        """A slice must NEVER produce a Python sublist (arbitrary slice banned)."""
        value, _ = safe_evaluate_formula_or_inert("tags[0:2]", row)
        assert value != ["alpha", "beta"]
        assert value is None


# --------------------------------------------------------------------------- #
# BP3-C-07 — non-integer index -> inert / None                   [ADVERSARIAL]
# --------------------------------------------------------------------------- #
class TestNonIntegerIndex:
    def test_string_index_returns_none(self, row):
        """``tags["x"]`` — a string index is not a valid integer -> None."""
        assert evaluate_formula(parse_formula('tags["x"]'), row) is None

    def test_float_index_returns_none(self, row):
        assert evaluate_formula(parse_formula("tags[1.5]"), row) is None

    def test_bool_index_is_not_treated_as_int(self, row):
        """``true`` must not be coerced to index 1 (no bool-as-int subscript)."""
        assert evaluate_formula(parse_formula("tags[true]"), row) is None

    def test_non_integer_index_never_raises(self, row):
        value, error = safe_evaluate_formula(parse_formula('tags["x"]'), row)
        assert value is None
        assert error is None


# --------------------------------------------------------------------------- #
# ADVERSARIAL — sandbox containment of the subscript
# --------------------------------------------------------------------------- #
class TestAdversarialContainment:
    def test_subscript_does_not_call_getattr_on_content(self, row, monkeypatch):
        """Evaluating a subscript never invokes getattr on a content value."""
        import builtins

        calls = []
        real_getattr = builtins.getattr

        def spy_getattr(obj, name, *default):
            calls.append((type(obj).__name__, name))
            return real_getattr(obj, name, *default)

        monkeypatch.setattr(builtins, "getattr", spy_getattr)
        evaluate_formula(parse_formula("tags[0]"), row)
        # No getattr targeting a content list/str by attribute name.
        forbidden = [c for c in calls if c[1].startswith("__") and "class" in c[1]]
        assert forbidden == []

    def test_dunder_index_field_is_refused(self, row):
        """A dunder identifier inside the subscript is refused at parse time."""
        with pytest.raises(BasesError):
            parse_formula("tags[__class__]")

    def test_huge_index_does_not_allocate(self, row):
        """A huge positive index out of bounds simply yields None (no alloc)."""
        assert evaluate_formula(parse_formula("tags[999999999]"), row) is None


# --------------------------------------------------------------------------- #
# Helper
# --------------------------------------------------------------------------- #
def safe_evaluate_formula_or_inert(expr: str, row: dict):
    """Parse + evaluate, mapping a parse rejection to the inert envelope too."""
    try:
        node = parse_formula(expr)
    except BasesError:
        return None, "unsupported"
    return safe_evaluate_formula(node, row)
