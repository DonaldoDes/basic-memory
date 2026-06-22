"""BUG-019 (M-Bases-P4) — LIVE rendering of grouped summary CELLS.

The normative lesson of US-f / P3 (and the reason this file exists): an
aggregate value's PRESENCE in the group footer line is NOT proof it reaches the
rendered table cell. BUG-019 observed a conditional count aggregate
(``Done: count(status == "Done")``) that CALCULATES correctly (``Total: 4`` /
``Done: 1`` observed in the italic group footer) but renders EMPTY cells in the
grouped table when the summary names are referenced in ``order:``.

Root cause (reproduced on the real MCP path before the fix): in the grouped
(NON-aggregate-only) render path, the per-group item rows are projected by
``_project``, which only knows ``formula.*`` calculated columns and STATIC
frontmatter columns. A summary/aggFormula NAME (``Done`` / ``Total`` /
``Progress``) is a GROUP-level aggregate, absent from any item's frontmatter →
``FieldResolver`` resolves it to ``None`` → empty cell. The computed value only
ever appeared in the italic footer (``_Total: 4 · Done: 1_``), never in a cell.
The ``aggregate: true`` path already injects summaries into cells; the
grouped-with-items path did not — this file pins the parity.

Fidelity (AC-2): the dataset is built by the canonical ``build_dataview_dataset``
(the SAME provider-faithful builder as ``test_live_path_integration`` /
``test_live_path_reconciliation``) — a hand-fabricated dict bypassing the
provider does NOT count. Every assertion is on ``process_note``'s
``result_markdown`` / ``results`` envelope (the real MCP render path), never on a
synthetic in-memory dataset.
"""

from __future__ import annotations

from basic_memory.bases.integration import create_bases_integration
from tests.bases.test_live_path_integration import _Entity, build_dataview_dataset

HOST = {"permalink": "products/x/milestones/m40", "path": "products/X/milestones/M40/M40.md", "title": "M40"}


def _block(body: str) -> str:
    return f"```base\n{body}```"


def _run(body: str, entities: list[_Entity]) -> dict:
    integ = create_bases_integration(notes_provider=lambda: build_dataview_dataset(entities))
    results = integ.process_note(_block(body), note_metadata=HOST)
    assert len(results) == 1
    return results[0]


def _progression_entities() -> list[_Entity]:
    """4 stories over 2 priorities; per group: Total=2, Done=1 (the BUG-019 shape)."""
    return [
        _Entity(
            title="US-1",
            file_path="products/X/milestones/M40/stories/US-1.md",
            permalink="products/x/milestones/m40/stories/us-1",
            note_type="user-story",
            metadata={"status": "Done", "priority": "P1"},
        ),
        _Entity(
            title="US-2",
            file_path="products/X/milestones/M40/stories/US-2.md",
            permalink="products/x/milestones/m40/stories/us-2",
            note_type="user-story",
            metadata={"status": "In Progress", "priority": "P1"},
        ),
        _Entity(
            title="US-3",
            file_path="products/X/milestones/M40/stories/US-3.md",
            permalink="products/x/milestones/m40/stories/us-3",
            note_type="user-story",
            metadata={"status": "Done", "priority": "P2"},
        ),
        _Entity(
            title="US-4",
            file_path="products/X/milestones/M40/stories/US-4.md",
            permalink="products/x/milestones/m40/stories/us-4",
            note_type="user-story",
            metadata={"status": "Todo", "priority": "P2"},
        ),
    ]


def _table_cells(markdown: str) -> list[list[str]]:
    """Extract DATA-row cells from every markdown table in the rendered output.

    Skips the header row and the ``| --- |`` separator, returns the trimmed cell
    text per data row so a test can assert a cell is NON-EMPTY. A table's header
    is the pipe-row immediately preceding its separator row; every pipe-row after
    the separator (until a non-pipe line breaks the table) is a data row.
    """
    rows: list[list[str]] = []
    in_table_body = False
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            in_table_body = False
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if all(c and set(c) <= {"-", ":"} for c in cells):
            in_table_body = True  # separator → body starts on the next pipe-row
            continue
        if in_table_body:
            rows.append(cells)
        # else: header row (pipe-row before the separator) — skipped
    return rows


# ===========================================================================
# BUG-019-01 — conditional count summary renders a NON-EMPTY cell on the real path
# ===========================================================================
class TestConditionalCountRendersCell:
    """BUG-019-01: ``Done: count(status == "Done")`` referenced in ``order:``
    must render the computed value in the table CELL, not just the footer."""

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

    def test_conditional_count_cell_is_not_empty(self):
        r = _run(self.BODY, _progression_entities())
        assert r["status"] == "success", r.get("error")
        # Every item row in every group must carry its group's Done value (1),
        # NOT an empty cell. Before the fix every `Done` cell was empty.
        done_values = [row.get("Done") for row in r["results"]]
        assert all(v == 1 for v in done_values), f"Done cells: {done_values}"
        # And the rendered markdown table must show it (no empty Done column).
        cells = _table_cells(r["result_markdown"])
        assert cells, "no table rendered"
        # Last column is `Done`; assert no rendered Done cell is blank.
        done_col = [row[-1] for row in cells]
        assert all(c == "1" for c in done_col), f"rendered Done column: {done_col}"


# ===========================================================================
# BUG-019-02 — cross-check simple count vs conditional count in the SAME block
# ===========================================================================
class TestSimpleAndConditionalCountBothRenderCells:
    """BUG-019-02: ``Total: count(title)`` (simple) AND
    ``Done: count(status == "Done")`` (conditional) — BOTH cells render the
    group value (isolates any divergent handling of the conditional)."""

    BODY = """filters:
  type == "user-story"
views:
  - type: table
    name: Progression
    groupBy: priority
    summaries:
      Total: count(title)
      Done: count(status == "Done")
    order:
      - priority
      - Total
      - Done
"""

    def test_both_summary_columns_render_group_values(self):
        r = _run(self.BODY, _progression_entities())
        assert r["status"] == "success", r.get("error")
        for row in r["results"]:
            assert row.get("Total") == 2, f"Total cell empty/wrong: {row}"
            assert row.get("Done") == 1, f"Done cell empty/wrong: {row}"
        cells = _table_cells(r["result_markdown"])
        # columns: priority | Total | Done — none of Total/Done may be blank.
        for row in cells:
            assert row[1] == "2", f"rendered Total cell blank: {row}"
            assert row[2] == "1", f"rendered Done cell blank: {row}"


# ===========================================================================
# BUG-019-03 — cross-summary aggFormula (round(Done/Total*100)) renders a CELL
# ===========================================================================
class TestAggFormulaRendersCell:
    """BUG-019-03: a post-aggregation cross-summary formula
    ``Progress: round(Done / Total * 100)`` referenced in ``order:`` renders the
    computed value in the cell on the real MCP path."""

    BODY = """filters:
  type == "user-story"
views:
  - type: table
    name: Progression
    groupBy: priority
    summaries:
      Total: count(title)
      Done: count(status == "Done")
    aggFormulas:
      Progress: round(Done / Total * 100)
    order:
      - priority
      - Done
      - Total
      - Progress
"""

    def test_progress_aggformula_cell_is_not_empty(self):
        r = _run(self.BODY, _progression_entities())
        assert r["status"] == "success", r.get("error")
        for row in r["results"]:
            assert row.get("Progress") == 50.0, f"Progress cell empty/wrong: {row}"
        cells = _table_cells(r["result_markdown"])
        # columns: priority | Done | Total | Progress
        for row in cells:
            assert row[1] == "1", f"Done blank: {row}"
            assert row[2] == "2", f"Total blank: {row}"
            assert row[3] == "50.0", f"Progress blank: {row}"


# ===========================================================================
# BUG-019 — aliased summary column still resolves to the group value
# ===========================================================================
class TestAliasedSummaryColumnRendersCell:
    """A summary column renamed via ``properties:`` (displayName) must still
    carry the group value in its (aliased) cell — mirrors _column_key parity."""

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
properties:
  Done:
    displayName: Réalisé
"""

    def test_aliased_summary_cell_is_not_empty(self):
        r = _run(self.BODY, _progression_entities())
        assert r["status"] == "success", r.get("error")
        # The aliased header "Réalisé" carries the value.
        for row in r["results"]:
            assert row.get("Réalisé") == 1, f"aliased Done cell empty: {row}"
        assert "Réalisé" in r["result_markdown"]
