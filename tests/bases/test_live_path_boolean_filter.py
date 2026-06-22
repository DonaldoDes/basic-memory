"""BUG-018 (M-Bases-P4) — LIVE boolean filter ``field == true`` on the real path.

ROOT CAUSE (recorded faithfully, confirmed against the live DB):
    A YAML frontmatter boolean ``agent_context: true`` is NOT stored as a Python
    ``bool`` in the dataset. The core parser
    ``entity_parser.normalize_frontmatter_value(True)`` returns ``str(True)`` ==
    ``"True"`` (entity_parser.py:73-74), so ``entity.entity_metadata`` — and thus
    the row projected by ``knowledge_router.list_entities_for_dataview`` — carries
    the STRING ``"True"`` (capitalised), never Python ``True``. In the formula
    sandbox ``formula_eval._eval_binop`` the equality ``field == true`` then
    evaluates ``"True" == True`` → ``False`` for EVERY note → 0 items.

    Verified on the real DB (``~/.basic-memory/memory.db``): every note with
    ``agent_context: true`` (Coordinator Workflow/Gates, ADR-PKM-002, …) stores
    ``entity_metadata["agent_context"] == "True"`` (a ``str``).

Why BUG-017 "passed" while this recurs: BUG-017's reconciliation used a synthetic
dataset carrying a real Python ``True``, so its comparison happened to work. The
LIVE provider path stringifies the boolean — hence this ticket's requirement of a
FAITHFUL fixture. These tests therefore feed the dataset the EXACT stored form
(the string ``"True"`` / ``"False"``), reproducing the live 0-item bug.

Fidelity: the dataset is built by the canonical ``build_dataview_dataset`` (the
SAME builder as ``test_live_path_integration`` / ``test_live_path_reconciliation``)
— a hand-fabricated dict bypassing the provider does NOT count for AC. The only
deliberate fidelity detail is that the boolean metadata value is the STRINGIFIED
form the provider actually stores (``"True"`` / ``"False"``), not a Python bool.

Run path: ``create_bases_integration().process_note`` → ``BasesExecutor`` via a
provider faithful to ``knowledge_router.list_entities_for_dataview``.
"""

from __future__ import annotations

from basic_memory.bases.integration import create_bases_integration
from tests.bases.test_live_path_integration import (
    _Entity,
    build_dataview_dataset,
)


def _block(body: str) -> str:
    return f"```base\n{body}```"


def _run(body: str, entities: list[_Entity], host: dict | None = None) -> dict:
    integ = create_bases_integration(notes_provider=lambda: build_dataview_dataset(entities))
    results = integ.process_note(_block(body), note_metadata=host)
    assert len(results) == 1
    return results[0]


# The stored form of a YAML boolean, EXACTLY as the provider projects it (the
# string "True"/"False", produced by normalize_frontmatter_value). This is the
# fidelity crux of BUG-018 — using a Python bool here would hide the bug.
STORED_TRUE = "True"
STORED_FALSE = "False"


def _agent_context_entities() -> list[_Entity]:
    """A faithful slice of the real vault: two notes flagged ``agent_context: true``
    (stored as the string ``"True"``), one flagged false (``"False"``), one with
    no ``agent_context`` field at all."""
    return [
        _Entity(
            title="Coordinator Workflow",
            file_path="_system/policies/Coordinator Workflow.md",
            permalink="system/policies/coordinator-workflow",
            note_type="note",
            metadata={"agent_context": STORED_TRUE, "agent_context_role": "workflow"},
        ),
        _Entity(
            title="Coordinator Gates",
            file_path="_system/policies/Coordinator Gates.md",
            permalink="system/policies/coordinator-gates",
            note_type="note",
            metadata={"agent_context": STORED_TRUE, "agent_context_role": "gates"},
        ),
        _Entity(
            title="Retired Policy",
            file_path="_system/policies/Retired.md",
            permalink="system/policies/retired",
            note_type="note",
            metadata={"agent_context": STORED_FALSE},
        ),
        _Entity(
            title="Ordinary Note",
            file_path="notes/ordinary.md",
            permalink="notes/ordinary",
            note_type="note",
            metadata={"status": "Active"},
        ),
    ]


class TestBooleanFilterLivePath:
    """BUG-018: ``field == true`` matches notes whose stored value is ``"True"``."""

    def test_agent_context_equals_true_matches_stringified_booleans(self):
        """The core repro: ``agent_context == true`` over a vault-wide scan (no
        ``file.inFolder``) returns the two flagged notes, NOT 0 items."""
        body = "filters: agent_context == true\nviews:\n  - type: list\n"
        r = _run(body, _agent_context_entities())
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 2
        assert {row.get("title") for row in r["results"]} == {
            "Coordinator Workflow",
            "Coordinator Gates",
        }

    def test_field_equals_false_matches_stringified_false(self):
        """``agent_context == false`` matches only the note stored as ``"False"``,
        never the ``"True"`` ones and never the field-absent note."""
        body = "filters: agent_context == false\nviews:\n  - type: list\n"
        r = _run(body, _agent_context_entities())
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 1
        assert {row.get("title") for row in r["results"]} == {"Retired Policy"}

    def test_field_equals_true_excludes_absent_field(self):
        """A note WITHOUT the field is never matched by ``== true`` (absent ≠ true)."""
        body = "filters: agent_context == true\nviews:\n  - type: list\n"
        r = _run(body, _agent_context_entities())
        titles = {row.get("title") for row in r["results"]}
        assert "Ordinary Note" not in titles
        assert "Retired Policy" not in titles

    def test_not_equals_true_complements_equals_true(self):
        """``agent_context != true`` is the faithful complement: the false note and
        the absent-field note, never the two ``"True"`` notes."""
        body = "filters: agent_context != true\nviews:\n  - type: list\n"
        r = _run(body, _agent_context_entities())
        assert r["status"] == "success", r.get("error")
        titles = {row.get("title") for row in r["results"]}
        assert "Coordinator Workflow" not in titles
        assert "Coordinator Gates" not in titles
        assert {"Retired Policy", "Ordinary Note"} <= titles

    def test_lowercase_stored_boolean_also_matches(self):
        """Defensive parity: should a provider/import path ever store the lower
        ``"true"`` form, ``== true`` must still match (the coercion is
        case-insensitive over the closed {true,false} table)."""
        entities = [
            _Entity(
                title="LowerTrue",
                file_path="notes/lower.md",
                permalink="notes/lower",
                note_type="note",
                metadata={"agent_context": "true"},
            ),
        ]
        body = "filters: agent_context == true\nviews:\n  - type: list\n"
        r = _run(body, entities)
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 1


class TestBooleanFilterNonRegression:
    """The coercion must not perturb ordinary string / arbitrary-value compares."""

    def test_real_python_bool_still_matches(self):
        """Non-regression for BUG-017's path: a dataset carrying a REAL Python
        ``True`` (synthetic/override shape) still matches ``== true``."""
        entities = [
            _Entity(
                title="RealBool",
                file_path="notes/real.md",
                permalink="notes/real",
                note_type="note",
                metadata={"flag": True},
            ),
        ]
        body = "filters: flag == true\nviews:\n  - type: list\n"
        r = _run(body, entities)
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 1

    def test_string_equality_unaffected_by_bool_coercion(self):
        """A string field compared to a string literal is unchanged — the boolean
        coercion only fires when one operand is a Python ``bool``."""
        entities = [
            _Entity(
                title="Active",
                file_path="p/a.md",
                permalink="p/a",
                note_type="project",
                metadata={"status": "Active"},
            ),
            _Entity(
                title="Done",
                file_path="p/d.md",
                permalink="p/d",
                note_type="project",
                metadata={"status": "Done"},
            ),
        ]
        body = 'filters: status == "Active"\nviews:\n  - type: list\n'
        r = _run(body, entities)
        assert r["status"] == "success", r.get("error")
        assert {row.get("title") for row in r["results"]} == {"Active"}

    def test_arbitrary_string_not_coerced_to_bool(self):
        """A non-boolean string (``"Truelike"``) is NEVER coerced — only the exact
        closed table {true,false} (any case) maps to a bool. ``"Truelike" == true``
        stays False, so the row is excluded."""
        entities = [
            _Entity(
                title="Trap",
                file_path="notes/trap.md",
                permalink="notes/trap",
                note_type="note",
                metadata={"agent_context": "Truelike"},
            ),
        ]
        body = "filters: agent_context == true\nviews:\n  - type: list\n"
        r = _run(body, entities)
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 0
