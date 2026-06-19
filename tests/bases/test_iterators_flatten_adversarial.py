"""[ADVERSARIAL] Security tests for US-3 — lambda iterators + FLATTEN on expression.

M-Bases-P4 / ADR-006 §Gap #5. These tests do NOT validate the nominal behaviour
(that is ``test_iterators_flatten.py`` / the live-path file). They validate the
*resistance* of the new surface to hostile / limit inputs, per the constitution
"Zones sensibles" (Bases executor) and ADR-006 §Invariants:

  - No ``eval`` / ``exec`` / ``getattr`` / ``__import__`` is introduced by the
    ``any`` / ``filter`` iterators or by FLATTEN-on-expression — dispatch stays a
    closed whitelist (BP4-E-02 class).
  - A predicate (lambda) that errors during evaluation degrades to a typed
    BasesError → inert block, the MCP handler never crashes (ADR-006 §Invariants,
    BP4-E-05 class).
  - An iterator / FLATTEN over a list whose cardinality exceeds the anti-DoS
    bound makes the WHOLE block inert (``error_type: limit``), with NO partial
    evaluation, and the rest of the note still renders (BP4-E-04 class).
  - The iterators read the row's already-resolved dataset list — never the
    filesystem (continuity with the asFile/outlinks invariants).

The marker ``[ADVERSARIAL]`` in this module docstring + the test names is the
required evidence for a sensitive-zone task (skill ``adversarial-testing``).
"""

from __future__ import annotations

import builtins

import pytest

from basic_memory.bases.errors import (
    BasesError,
    BasesLimitError,
    BasesUnsupportedError,
)
from basic_memory.bases.formula_ast import FCall, FField, FLambda, FLiteral, FPropChain
from basic_memory.bases.formula_eval import (
    evaluate_formula,
    safe_evaluate_formula,
)
from basic_memory.bases.formula_parser import parse_formula
from basic_memory.bases.integration import create_bases_integration
from basic_memory.bases.parser import BasesParser
from basic_memory.bases.schema import MAX_FLATTEN_CARDINALITY
from tests.bases.test_live_path_integration import _Entity, _Rel, build_dataview_dataset


@pytest.fixture
def row() -> dict:
    return {
        "title": "Note A",
        "file.path": "areas/Area/Note A.md",
        "frontmatter": {"items": ["a", "b", "c"]},
        "items": ["a", "b", "c"],
    }


# ---------------------------------------------------------------------------
# [ADVERSARIAL] No dynamic evaluation path is opened by the iterators
# ---------------------------------------------------------------------------
class TestNoDynamicEvalIntroduced:
    def test_any_with_hostile_function_name_in_lambda_body_refused(self):
        """[ADVERSARIAL] A lambda body calling an out-of-whitelist function is
        refused at PARSE time — never dispatched, never getattr'd on builtins."""
        # any(items, x => __import__("os"))  — the dunder/hostile call must be
        # refused by the closed parser whitelist before any evaluation.
        with pytest.raises(BasesUnsupportedError):
            parse_formula('any(items, x => __import__("os"))')

    def test_filter_lambda_referencing_builtins_resolves_to_none(self, row):
        """[ADVERSARIAL] A predicate referencing a host module name (``builtins``)
        resolves to None in the closed lambda env — the sandbox never exposes it.

        filter(items, x => builtins) keeps zero elements (every predicate falsy),
        proving ``builtins`` is NOT reachable from the lambda body.
        """
        node = FCall(
            "filter",
            [FField("items"), FLambda(params=["x"], body=FField("builtins"))],
        )
        result = evaluate_formula(node, row)
        assert result == []

    def test_iterators_do_not_touch_filesystem(self, row, monkeypatch):
        """[ADVERSARIAL] any()/filter() over a list never open a file nor stat."""
        calls = {"open": 0, "stat": 0}
        import os

        monkeypatch.setattr(
            builtins, "open", lambda *a, **k: calls.__setitem__("open", calls["open"] + 1)
        )
        monkeypatch.setattr(
            os, "stat", lambda *a, **k: calls.__setitem__("stat", calls["stat"] + 1)
        )

        # any(items, x => x.startsWith("a"))  → True, no FS access.
        node = FCall(
            "any",
            [
                FField("items"),
                FLambda(
                    params=["x"],
                    body=FPropChain(
                        receiver=FField("x"), member="startsWith", args=[FLiteral("a")]
                    ),
                ),
            ],
        )
        result = evaluate_formula(node, row)
        assert result is True
        assert calls["open"] == 0
        assert calls["stat"] == 0


# ---------------------------------------------------------------------------
# [ADVERSARIAL] Predicate error → inert block, handler never crashes
# ---------------------------------------------------------------------------
class TestPredicateErrorDegradesToInert:
    def test_lambda_division_by_zero_degrades_to_typed_error(self, row):
        """[ADVERSARIAL] A predicate that divides by zero is a typed BasesError
        (inert block), never a raw Python ZeroDivisionError reaching the host."""
        # filter(items, x => 1 / 0)  — the body raises a typed error on first elem.
        node = FCall(
            "filter",
            [
                FField("items"),
                FLambda(params=["x"], body=FCall("if", [FLiteral(True), FLiteral(1), FLiteral(0)])),
            ],
        )
        # Replace body with a real div-by-zero via parse (clearer intent).
        node = parse_formula("filter(items, x => 1 / 0)")
        with pytest.raises(BasesError):
            evaluate_formula(node, row)

    def test_safe_evaluate_maps_predicate_error_to_error_type(self, row):
        """[ADVERSARIAL] safe_evaluate_formula maps a predicate error to
        ``(None, error_type)`` — the MCP handler boundary never sees an exception."""
        node = parse_formula("any(items, x => 1 / 0)")
        value, error_type = safe_evaluate_formula(node, row)
        assert value is None
        assert error_type in {"execution", "unexpected"}


# ---------------------------------------------------------------------------
# [ADVERSARIAL] Anti-DoS cardinality bound → block inert, no partial eval
# ---------------------------------------------------------------------------
class TestIteratorCardinalityBound:
    def test_any_over_bound_raises_limit(self):
        """[ADVERSARIAL] any() over a list above MAX_ITERATOR_CARDINALITY raises
        BasesLimitError (block inert, error_type limit) — NO partial evaluation."""
        from basic_memory.bases.schema import MAX_ITERATOR_CARDINALITY

        big = list(range(MAX_ITERATOR_CARDINALITY + 1))
        row = {"big": big, "frontmatter": {"big": big}}
        node = parse_formula("any(big, x => x > 0)")
        with pytest.raises(BasesLimitError):
            evaluate_formula(node, row)

    def test_filter_over_bound_raises_limit(self):
        """[ADVERSARIAL] filter() over a list above the bound is inert, not
        truncated — a hostile note must not silently drop data."""
        from basic_memory.bases.schema import MAX_ITERATOR_CARDINALITY

        big = list(range(MAX_ITERATOR_CARDINALITY + 1))
        row = {"big": big, "frontmatter": {"big": big}}
        node = parse_formula("filter(big, x => x > 0)")
        with pytest.raises(BasesLimitError):
            evaluate_formula(node, row)

    def test_bound_not_tripped_at_the_limit(self):
        """[ADVERSARIAL] Exactly MAX_ITERATOR_CARDINALITY elements is allowed
        (the guard fires strictly ABOVE the bound, no off-by-one inert block)."""
        from basic_memory.bases.schema import MAX_ITERATOR_CARDINALITY

        ok = list(range(MAX_ITERATOR_CARDINALITY))
        row = {"ok": ok, "frontmatter": {"ok": ok}}
        node = parse_formula("any(ok, x => x > 0)")
        # Evaluates without raising; the predicate is true for some element.
        assert evaluate_formula(node, row) is True


# ---------------------------------------------------------------------------
# [ADVERSARIAL] FLATTEN-on-expression — closed whitelist + anti-DoS cardinality
# ---------------------------------------------------------------------------
class TestFlattenExpressionHostileInput:
    """[ADVERSARIAL] The ``flatten: {expression: ..., as: ...}`` surface
    (ADR-006 §Gap #5) must (a) refuse any expression outside the CLOSED
    ``_FLATTEN_FILE_EXPRESSIONS`` whitelist at PARSE time — no dynamic dispatch
    on ``file.*`` — and (b) make the WHOLE block inert (``error_type: limit``)
    when an expression list over-expands a row, with NO partial evaluation and
    the rest of the note still rendering.
    """

    def test_flatten_unknown_expression_refused(self):
        """[ADVERSARIAL] ``flatten: {expression: file.evil, as: x}`` — an
        expression outside the closed ``_FLATTEN_FILE_EXPRESSIONS`` whitelist is
        refused at PARSE time with ``BasesUnsupportedError``: it is never resolved,
        never dispatched against ``file.*``, the block is inert before execution.
        """
        block = (
            "views:\n"
            "  - type: table\n"
            "    flatten:\n"
            "      expression: file.evil\n"
            "      as: x\n"
            "    order:\n"
            "      - x\n"
        )
        with pytest.raises(BasesUnsupportedError):
            BasesParser.parse(block)

    def test_flatten_expression_over_cardinality_inert(self):
        """[ADVERSARIAL] A FLATTEN over a computed list EXPRESSION
        (``file.outlinks``) whose length exceeds ``MAX_FLATTEN_CARDINALITY``
        makes the WHOLE block inert (``status: error`` / ``error_type: limit``) —
        NO partial expansion, no truncated row dump — while a SECOND, benign block
        in the same note still renders (``status: success``). A hostile note must
        neither silently drop data nor poison the rest of the note.
        """
        # One entity whose file.outlinks over-expands a single row past the bound.
        # Each _Rel with to_name-only contributes exactly one outlink, so
        # MAX_FLATTEN_CARDINALITY + 1 relations => one element over the bound.
        over = MAX_FLATTEN_CARDINALITY + 1
        hostile = _Entity(
            title="Hostile Hub",
            file_path="notes/hostile.md",
            permalink="notes/hostile",
            outgoing_relations=[
                _Rel(to_name=f"Target {i}", to_permalink=None) for i in range(over)
            ],
        )
        entities = [hostile]
        integ = create_bases_integration(
            notes_provider=lambda: build_dataview_dataset(entities)
        )

        flatten_block = (
            "views:\n"
            "  - type: table\n"
            "    flatten:\n"
            "      expression: file.outlinks\n"
            "      as: link\n"
            "    order:\n"
            "      - link\n"
        )
        benign_block = "views:\n  - type: list\n"
        note = f"```base\n{flatten_block}```\n\n```base\n{benign_block}```"

        results = integ.process_note(note)
        assert len(results) == 2

        flatten_result, benign_result = results

        # (a) The FLATTEN block is INERT — a typed limit error, not a crash, and
        #     NOT a partial render (zero rows emitted).
        assert flatten_result["status"] == "error"
        assert flatten_result["error_type"] == "limit"
        assert flatten_result["result_count"] == 0

        # (b) The rest of the note renders: the benign block is unaffected by the
        #     hostile sibling and still succeeds.
        assert benign_result["status"] == "success", benign_result.get("error")
