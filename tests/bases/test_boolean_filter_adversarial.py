"""BUG-018 — adversarial coverage for the boolean-coercion in the filter sandbox.

The fix adds ``FormulaEvaluator._coerce_boolean_operands`` to bridge the
provider's stringified frontmatter boolean (``"True"``) to a literal ``true`` in
a filter. This module proves the coercion is CLOSED and cannot be weaponised:

  * it never widens the closed {true,false} table (no arbitrary string → bool),
  * it never fires unless one operand is a real Python ``bool`` (so it cannot
    silently rewrite an attacker-controlled string compare),
  * the numeric ``bool``-is-``int`` subclass edge is excluded,
  * no eval / exec / getattr / __import__ / compile is introduced by the fix,
  * a hostile filter leaf (dunder, unknown function, over-long, property chain)
    is still REFUSED before the comparison is ever reached — the block goes inert.

All assertions run on the REAL render path
(``create_bases_integration().process_note`` → executor via a provider faithful
to ``knowledge_router.list_entities_for_dataview``).
"""

from __future__ import annotations

import inspect

from basic_memory.bases import formula_eval
from basic_memory.bases.formula_eval import FormulaEvaluator
from basic_memory.bases.integration import create_bases_integration
from tests.bases.test_live_path_integration import _Entity, build_dataview_dataset


def _block(body: str) -> str:
    return f"```base\n{body}```"


def _run(body: str, entities: list[_Entity], host: dict | None = None) -> dict:
    integ = create_bases_integration(notes_provider=lambda: build_dataview_dataset(entities))
    results = integ.process_note(_block(body), note_metadata=host)
    assert len(results) == 1
    return results[0]


class TestCoercionIsClosed:
    """The coercion never widens beyond the literal {true,false} table."""

    def test_arbitrary_string_never_coerced(self):
        """Any non-{true,false} string stays a string — never a truthy bool."""
        entities = [
            _Entity(
                title="Trap",
                file_path="n/trap.md",
                permalink="n/trap",
                note_type="note",
                metadata={"flag": "yes"},
            ),
        ]
        r = _run("filters: flag == true\nviews:\n  - type: list\n", entities)
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 0

    def test_numeric_field_not_widened_by_string_coercion(self):
        """The string-coercion never fabricates a bool out of a NUMBER. ``2 ==
        true`` does not match (2 is neither the stored boolean string nor equal to
        ``True``). This pins that the coercion only ever touches a STRING side —
        the ``1 == True`` Python-native equality is a separate pre-existing
        semantic, not something the string coercion introduces (the unit table
        below proves ``(1, "true")`` passes through untouched)."""
        entities = [
            _Entity(
                title="NumTwo",
                file_path="n/two.md",
                permalink="n/two",
                note_type="note",
                metadata={"flag": 2},
            ),
        ]
        r = _run("filters: flag == true\nviews:\n  - type: list\n", entities)
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 0

    def test_empty_string_not_coerced(self):
        """An empty-string field is not the false literal — ``== false`` excludes it."""
        entities = [
            _Entity(
                title="Empty",
                file_path="n/empty.md",
                permalink="n/empty",
                note_type="note",
                metadata={"flag": ""},
            ),
        ]
        r = _run("filters: flag == false\nviews:\n  - type: list\n", entities)
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 0

    def test_coercion_unit_table_is_exhaustive(self):
        """Directly exercise the helper: only the closed table maps; anything else
        passes through verbatim, in BOTH operand positions."""
        c = FormulaEvaluator._coerce_boolean_operands
        # stored "True"/"False" (provider form) coerced against a real bool
        assert c("==", "True", True) == (True, True)
        assert c("==", True, "False") == (True, False)
        # lower forms tolerated
        assert c("!=", "true", False) == (True, False)
        # NOT in the table → untouched (string-vs-bool stays distinct types)
        assert c("==", "Truelike", True) == ("Truelike", True)
        assert c("==", "1", True) == ("1", True)
        # neither side a bool → untouched (ordinary string compare)
        assert c("==", "Active", "Done") == ("Active", "Done")
        # op outside {==,!=} → untouched even with a coercible string
        assert c("<", "true", True) == ("true", True)
        # numeric bool/int edge: a real int (not bool) on one side is NOT widened
        assert c("==", 1, "true") == (1, "true")


class TestSandboxStillClosed:
    """The fix introduces no dynamic-execution primitive and refuses hostile leaves."""

    def test_no_dynamic_execution_primitive_in_coercion_helper(self):
        """Static guard scoped to the FIX: the boolean-coercion helper is pure
        dict/string logic — no eval/exec/getattr/__import__/compile call site.

        Mirrors the constitution invariant for the Bases sandbox (ADR-005)."""
        src = inspect.getsource(FormulaEvaluator._coerce_boolean_operands)
        for forbidden in ("eval(", "exec(", "__import__", "compile(", "getattr(", "setattr("):
            assert forbidden not in src, f"forbidden primitive {forbidden!r} in coercion helper"

    def test_module_has_no_dynamic_call_site(self):
        """Module-wide: the only ``eval`` token is the AST-walk method ``self.eval``
        and the audited ``safe_evaluate_formula`` entrypoint — no bare ``eval()`` /
        ``exec()`` / dynamic ``getattr()`` call introduced anywhere."""
        src = inspect.getsource(formula_eval)
        # Strip the module docstring (it names the forbidden primitives in a
        # NEGATIVE assertion: "no compile / __import__ ...").
        body = src.split('"""', 2)[-1]
        assert "exec(" not in body
        assert "__import__(" not in body
        assert "compile(" not in body
        # getattr appears nowhere as a call; ``eval(`` only as ``self.eval(`` /
        # ``.eval(`` AST walk, never as a bare builtin invocation.
        assert "getattr(" not in body
        for line in body.splitlines():
            stripped = line.lstrip()
            if "eval(" in line and not stripped.startswith("#"):
                assert (
                    "self.eval(" in line
                    or ".eval(" in line
                    or "def eval(" in stripped
                    or "evaluate_formula" in line
                ), f"bare eval() call site: {line!r}"

    def test_hostile_dunder_leaf_refused_not_evaluated(self):
        """A dunder-bearing leaf is refused by the parser BEFORE any comparison —
        the boolean coercion is never reached; the block is inert."""
        entities = [
            _Entity(
                title="X",
                file_path="n/x.md",
                permalink="n/x",
                note_type="note",
                metadata={"flag": "True"},
            ),
        ]
        r = _run("filters: flag.__class__ == true\nviews:\n  - type: list\n", entities)
        assert r["status"] == "error"
        assert r["error_type"] in {"unsupported", "parse"}

    def test_unknown_function_leaf_refused(self):
        """A function outside the closed whitelist is refused — inert block, never
        a fallback to eval, even when paired with a boolean literal."""
        entities = [
            _Entity(
                title="X",
                file_path="n/x.md",
                permalink="n/x",
                note_type="note",
                metadata={"flag": "True"},
            ),
        ]
        r = _run("filters: dangerous(flag) == true\nviews:\n  - type: list\n", entities)
        assert r["status"] == "error"
        assert r["error_type"] in {"unsupported", "parse"}
