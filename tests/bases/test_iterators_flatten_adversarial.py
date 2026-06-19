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
