"""Regression tests — two live-path Bases executor bugs (fix/bases-count-or-and-sort).

Both bugs were found by REAL MCP render verification, not by synthetic unit data.
These tests therefore drive the exact production path
(``BasesIntegration.process_note`` over the faithful provider dataset built by
``build_dataview_dataset``) so a fix that only patches a synthetic helper would
not turn them green.

BUG 1 — ``count(a or b)`` in an aggregate counts only the LEFT operand.
    A ``count(status == "Done" or status == "Completed")`` written under
    ``aggFormulas:`` rendered ``1`` instead of ``2`` on a group holding one
    ``Done`` note and one ``Completed`` note: the whole predicate was treated as
    a post-aggregation formula and ``count`` was applied to a single evaluated
    boolean (``len([bool]) == 1``), dropping the ``or`` branch. The conditional
    aggregate must evaluate the COMPLETE predicate (``or``/``and``/``not``) per
    group row — identical to the ``summaries:`` predicate path.

BUG 2 — ``sort:`` not applied on the table path.
    After projection the row is keyed by the column's *displayName* alias
    (``Date``), but ``_apply_sort`` looked up the raw ``clause.field`` (``date``)
    → ``row.get("date")`` was ``None`` for every row → the stable sort preserved
    discovery order. ``sort: date DESC`` must really order ``06-15`` before
    ``06-10`` — and must work ASC/DESC and on a column NOT present in ``order``.

Security continuity (ADR-004/005 §Axe 5): the predicate is evaluated inside the
closed Phase 2 sandbox (``safe_evaluate_formula``) — no ``eval``/``exec``/
``getattr`` is introduced; the sort fix is a pure dict/field-resolution change.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Reuse the canonical, I/O-free provider reproduction from the live-path suite.
sys.path.insert(0, str(Path(__file__).parent))
from test_live_path_integration import (  # noqa: E402
    _Entity,
    _Rel,
    build_dataview_dataset,
)

from basic_memory.bases.integration import create_bases_integration  # noqa: E402


def _block(body: str) -> str:
    return f"```base\n{body}```"


def _run(provider, body: str, host: dict | None = None) -> dict:
    integ = create_bases_integration(notes_provider=provider)
    results = integ.process_note(_block(body), note_metadata=host)
    assert len(results) == 1
    return results[0]


# ===========================================================================
# BUG 1 — count(a or b) / and / not in an aggregate counts the full predicate
# ===========================================================================
class TestCountOrAggregate:
    def _provider(self):
        # One Done note + one Completed note in the same group (type == note).
        return lambda: build_dataview_dataset(
            [
                _Entity(
                    title="A",
                    file_path="_system/tmp/a.md",
                    permalink="t/a",
                    note_type="note",
                    metadata={"status": "Done"},
                ),
                _Entity(
                    title="B",
                    file_path="_system/tmp/b.md",
                    permalink="t/b",
                    note_type="note",
                    metadata={"status": "Completed"},
                ),
            ]
        )

    def test_count_or_in_aggformulas_counts_both_operands(self):
        """AC1: count(Done OR Completed) under aggFormulas == 2 (not 1)."""
        body = (
            'filters: file.inFolder("_system/tmp")\n'
            "views:\n"
            "  - type: table\n"
            "    groupBy: type\n"
            "    aggFormulas:\n"
            '      done_or_completed: count(status == "Done" or status == "Completed")\n'
            "    aggregate: true\n"
            "    order:\n"
            "      - type\n"
            "      - done_or_completed\n"
        )
        r = _run(self._provider(), body)
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 1
        assert r["results"][0]["done_or_completed"] == 2

    def test_count_or_in_summaries_counts_both_operands(self):
        """AC3: the same predicate under summaries: also yields 2."""
        body = (
            'filters: file.inFolder("_system/tmp")\n'
            "views:\n"
            "  - type: table\n"
            "    groupBy: type\n"
            "    summaries:\n"
            '      done_or_completed: count(status == "Done" or status == "Completed")\n'
            "    aggregate: true\n"
            "    order:\n"
            "      - type\n"
            "      - done_or_completed\n"
        )
        r = _run(self._provider(), body)
        assert r["status"] == "success", r.get("error")
        assert r["results"][0]["done_or_completed"] == 2

    def test_count_and_predicate_in_aggformulas(self):
        """AC3: ``and`` inside a count() aggregate evaluates the full predicate."""
        body = (
            'filters: file.inFolder("_system/tmp")\n'
            "views:\n"
            "  - type: table\n"
            "    groupBy: type\n"
            "    aggFormulas:\n"
            '      done_and_a: count(status == "Done" and title == "A")\n'
            "    aggregate: true\n"
            "    order:\n"
            "      - type\n"
            "      - done_and_a\n"
        )
        r = _run(self._provider(), body)
        assert r["status"] == "success", r.get("error")
        # Only note A is Done AND titled "A".
        assert r["results"][0]["done_and_a"] == 1

    def test_count_not_predicate_in_aggformulas(self):
        """AC3: ``not`` inside a count() aggregate evaluates the full predicate."""
        body = (
            'filters: file.inFolder("_system/tmp")\n'
            "views:\n"
            "  - type: table\n"
            "    groupBy: type\n"
            "    aggFormulas:\n"
            '      not_done: count(not (status == "Done"))\n'
            "    aggregate: true\n"
            "    order:\n"
            "      - type\n"
            "      - not_done\n"
        )
        r = _run(self._provider(), body)
        assert r["status"] == "success", r.get("error")
        # Only note B (Completed) is NOT Done.
        assert r["results"][0]["not_done"] == 1

    def test_aggformula_arithmetic_still_works(self):
        """Non-regression: a genuine post-aggregation cross-summary formula
        (``round(Done / Total * 100)``) keeps working alongside the count fix."""
        body = (
            'filters: file.inFolder("_system/tmp")\n'
            "views:\n"
            "  - type: table\n"
            "    groupBy: type\n"
            "    summaries:\n"
            "      Total: count(title)\n"
            '      Done: count(status == "Done" or status == "Completed")\n'
            "    aggFormulas:\n"
            "      Pct: round(Done / Total * 100)\n"
            "    aggregate: true\n"
            "    order:\n"
            "      - type\n"
            "      - Done\n"
            "      - Total\n"
            "      - Pct\n"
        )
        r = _run(self._provider(), body)
        assert r["status"] == "success", r.get("error")
        grp = r["results"][0]
        assert grp["Done"] == 2
        assert grp["Total"] == 2
        assert grp["Pct"] == 100  # round(2/2*100)


# ===========================================================================
# BUG 2 — sort: applied on the table path (aliased column + ASC/DESC + off-order)
# ===========================================================================
class TestSortAppliedTablePath:
    def _provider(self):
        # Two backlinking notes with distinct dates; discovery order is
        # Older-then-Newer so a no-op sort would leave 06-10 before 06-15.
        return lambda: build_dataview_dataset(
            [
                _Entity(
                    title="Older",
                    file_path="p/older.md",
                    permalink="p/older",
                    note_type="note",
                    metadata={"date": "2026-06-10"},
                    outgoing_relations=[_Rel(to_name="HOST", to_permalink="host/h")],
                ),
                _Entity(
                    title="Newer",
                    file_path="p/newer.md",
                    permalink="p/newer",
                    note_type="note",
                    metadata={"date": "2026-06-15"},
                    outgoing_relations=[_Rel(to_name="HOST", to_permalink="host/h")],
                ),
            ]
        )

    _HOST = {"permalink": "host/h", "path": "host/H.md", "title": "HOST"}

    def test_sort_date_desc_aliased_column(self):
        """AC2: sort date DESC orders 06-15 before 06-10 with a displayName alias."""
        body = (
            "filters:\n"
            "  and:\n"
            "    - file.hasLink(this.file)\n"
            "properties:\n"
            "  file.name: {displayName: Note}\n"
            "  date: {displayName: Date}\n"
            "views:\n"
            "  - type: table\n"
            "    order: [file.name, date]\n"
            "    sort:\n"
            "      - property: date\n"
            "        direction: DESC\n"
            "    limit: 20\n"
        )
        r = _run(self._provider(), body, host=self._HOST)
        assert r["status"] == "success", r.get("error")
        assert [row["title"] for row in r["results"]] == ["Newer", "Older"]

    def test_sort_date_asc_aliased_column(self):
        """AC4: the same sort ASC orders 06-10 before 06-15."""
        body = (
            "filters:\n"
            "  and:\n"
            "    - file.hasLink(this.file)\n"
            "properties:\n"
            "  date: {displayName: Date}\n"
            "views:\n"
            "  - type: table\n"
            "    order: [file.name, date]\n"
            "    sort:\n"
            "      - property: date\n"
            "        direction: ASC\n"
        )
        r = _run(self._provider(), body, host=self._HOST)
        assert r["status"] == "success", r.get("error")
        assert [row["title"] for row in r["results"]] == ["Older", "Newer"]

    def test_sort_on_column_absent_from_order(self):
        """AC4: sort works on a column that is NOT in ``order`` (date hidden)."""
        body = (
            "filters:\n"
            "  and:\n"
            "    - file.hasLink(this.file)\n"
            "views:\n"
            "  - type: table\n"
            "    order: [file.name]\n"  # date NOT projected
            "    sort:\n"
            "      - property: date\n"
            "        direction: DESC\n"
        )
        r = _run(self._provider(), body, host=self._HOST)
        assert r["status"] == "success", r.get("error")
        assert [row["title"] for row in r["results"]] == ["Newer", "Older"]

    def test_sort_without_alias_raw_field(self):
        """AC4: sort works with no ``properties`` block (raw field key)."""
        body = (
            "filters:\n"
            "  and:\n"
            "    - file.hasLink(this.file)\n"
            "views:\n"
            "  - type: table\n"
            "    order: [file.name, date]\n"
            "    sort:\n"
            "      - property: date\n"
            "        direction: DESC\n"
        )
        r = _run(self._provider(), body, host=self._HOST)
        assert r["status"] == "success", r.get("error")
        assert [row["title"] for row in r["results"]] == ["Newer", "Older"]
