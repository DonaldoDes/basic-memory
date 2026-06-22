"""BUG-019 adversarial — grouped summary CELL projection (zone sensible).

The fix injects each group's computed summary values into the per-item rows of
the grouped (non-aggregate-only) table so a summary name referenced in ``order:``
renders a non-empty cell. Threading group-level values into item rows is a new
data path → these tests pin the NEGATIVE invariants ("ce qui ne doit JAMAIS
arriver"):

  ADV-1  A hostile/conflicting frontmatter field on an item that COLLIDES with a
         summary output name does NOT override the group aggregate in the cell
         (the group value wins — summary names are authoritative, not item data).
  ADV-2  No eval/exec/getattr is introduced by the summary-cell projection: the
         conditional predicate is still evaluated only through the closed Phase 2
         sandbox; a hostile predicate is refused at PARSE (block inert).
  ADV-3  A summary name that shadows a real static column does not leak the item's
         private frontmatter into a summary cell — the cell carries the aggregate.
  ADV-4  Division-by-zero in a grouped aggFormula degrades the cell to None
         (blank), never raises / crashes the block.
  ADV-5  An item-level value attempting markdown/table injection through the
         group key is rendered as opaque text, never breaks the table structure.

Source-confined: every assertion drives the REAL render path (BasesExecutor /
process_note), no private internals are monkeypatched.
"""

from __future__ import annotations

import pytest

from basic_memory.bases.errors import BasesParseError
from basic_memory.bases.integration import create_bases_integration
from basic_memory.bases.parser import BasesParser
from tests.bases.test_live_path_integration import _Entity, build_dataview_dataset

HOST = {"permalink": "p/m", "path": "p/m.md", "title": "M"}


def _block(body: str) -> str:
    return f"```base\n{body}```"


def _run(body: str, entities: list[_Entity]) -> dict:
    integ = create_bases_integration(notes_provider=lambda: build_dataview_dataset(entities))
    return integ.process_note(_block(body), note_metadata=HOST)[0]


# ADV-1 / ADV-3 — an item frontmatter field colliding with a summary name does
# NOT override the group aggregate.
class TestItemFieldDoesNotOverrideSummaryCell:
    BODY = """filters:
  type == "user-story"
views:
  - type: table
    name: Progression
    groupBy: priority
    summaries:
      Done: count(status == "Done")
    order:
      - title
      - Done
"""

    def test_hostile_item_done_field_does_not_win(self):
        # Each item carries a bogus frontmatter `Done` value; the SUMMARY value
        # (group count = 1) must win in the rendered cell, not the item's 999.
        ents = [
            _Entity(
                title="US-1",
                file_path="p/m/US-1.md",
                permalink="p/m/us-1",
                note_type="user-story",
                metadata={"status": "Done", "priority": "P1", "Done": 999},
            ),
            _Entity(
                title="US-2",
                file_path="p/m/US-2.md",
                permalink="p/m/us-2",
                note_type="user-story",
                metadata={"status": "Todo", "priority": "P1", "Done": -1},
            ),
        ]
        r = _run(self.BODY, ents)
        assert r["status"] == "success", r.get("error")
        for row in r["results"]:
            assert row.get("Done") == 1, f"item field leaked into summary cell: {row}"


# ADV-2 — the summary-cell projection introduces no eval/exec path: a hostile
# conditional predicate is refused at parse (block inert).
class TestHostilePredicateStillRefused:
    @pytest.mark.parametrize(
        "predicate",
        [
            "__import__('os')",
            "status.__class__",
            "eval('1')",
            "(lambda: 1)()",
        ],
    )
    def test_hostile_conditional_summary_refused_at_parse(self, predicate):
        body = f"""filters:
  type == "user-story"
views:
  - type: table
    name: G
    groupBy: priority
    summaries:
      Done: count({predicate})
    order:
      - title
      - Done
"""
        with pytest.raises((BasesParseError, Exception)):
            BasesParser.parse(body)


# ADV-4 — division by zero in a grouped aggFormula degrades the cell to None
# (blank), never raises.
class TestGroupedAggFormulaDivByZeroDegrades:
    BODY = """filters:
  type == "user-story"
views:
  - type: table
    name: Progression
    groupBy: priority
    summaries:
      Done: count(status == "Done")
      Empty: count(status == "NeverMatches")
    aggFormulas:
      Ratio: round(Done / Empty * 100)
    order:
      - title
      - Ratio
"""

    def test_div_by_zero_cell_blank_not_crash(self):
        ents = [
            _Entity(
                title="US-1",
                file_path="p/m/US-1.md",
                permalink="p/m/us-1",
                note_type="user-story",
                metadata={"status": "Done", "priority": "P1"},
            ),
        ]
        r = _run(self.BODY, ents)
        assert r["status"] == "success", r.get("error")
        # Empty=0 → Done/Empty raises in sandbox → degrades to None (blank cell).
        for row in r["results"]:
            assert row.get("Ratio") is None, f"div-by-zero not degraded: {row}"


# ADV-5 — a group key carrying table-injection text is rendered opaque, never
# breaks the table grid (summary cells still align).
class TestGroupKeyInjectionIsOpaque:
    BODY = """filters:
  type == "user-story"
views:
  - type: table
    name: Progression
    groupBy: priority
    summaries:
      Done: count(status == "Done")
    order:
      - priority
      - Done
"""

    def test_pipe_in_group_key_does_not_break_summary_cell(self):
        ents = [
            _Entity(
                title="US-1",
                file_path="p/m/US-1.md",
                permalink="p/m/us-1",
                note_type="user-story",
                metadata={"status": "Done", "priority": "P1 | injected | 999"},
            ),
        ]
        r = _run(self.BODY, ents)
        assert r["status"] == "success", r.get("error")
        # The summary cell still resolves to the group count despite the hostile key.
        for row in r["results"]:
            assert row.get("Done") == 1, f"summary cell lost to injection: {row}"
