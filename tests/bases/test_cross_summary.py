"""US-d — Cross-summary arithmetic + conditional aggregates + aggregate-only view.

Maps the 7 Test IDs of US-d (BP3-D-01..07), themselves mapped to the Gherkin
scenarios of the story:

  BP3-D-01 [unit]        Conditional aggregate: count filtered on a predicate
                         (``status == "Done"``)                                  (Scenario "Agrégat conditionnel")
  BP3-D-02 [unit]        Post-agg formula ``round(Done/Total*100)`` combines two
                         summaries                                               (Scenario "Pourcentage de progression")
  BP3-D-03 [unit]        Division by zero (``Total == 0``) -> None, never raises (Scenario "Division par zéro gardée")
  BP3-D-04 [integration] Aggregate-only view: 1 row per group, zero row-items    (Scenario "Vue agrégat-only")
  BP3-D-05 [integration] A converted Progression block renders the per-milestone
                         table                                                   (Scenario "Bloc Progression converti rend")
  BP3-D-06 [unit]        Aggregate-only group cardinality over the bound -> block
                         inert (limit)
  BP3-D-07 [unit]        Post-agg arithmetic depth over the bound -> block inert

Security invariant (ADR-005 §Axe 4/5): predicates and post-agg formulas are
evaluated in the CLOSED Phase 2 sandbox (``safe_evaluate_formula``) — never
eval/exec/getattr. The adversarial cases (div-by-zero, cardinality bomb, depth
bomb) live in ``test_cross_summary_adversarial.py``.
"""

import pytest

from basic_memory.bases.errors import BasesLimitError
from basic_memory.bases.executor import BasesExecutor
from basic_memory.bases.parser import BasesParser
from basic_memory.bases.schema import MAX_AGG_ONLY_GROUPS


def _note(title, **fm):
    folder = fm.pop("folder", "notes")
    return {
        "title": title,
        "file": {"path": f"{folder}/{title}.md", "name": title, "folder": folder},
        "frontmatter": dict(fm),
    }


# ---------------------------------------------------------------------------
# BP3-D-01 — Conditional aggregate: count filtered on a predicate
# ---------------------------------------------------------------------------
class TestConditionalAggregate:
    def test_count_predicate_counts_only_matching_rows(self):
        """count(status == "Done") counts only the Done rows of each group."""
        notes = [
            _note("A", milestone="M1", status="Done"),
            _note("B", milestone="M1", status="Todo"),
            _note("C", milestone="M1", status="Done"),
            _note("D", milestone="M2", status="Todo"),
        ]
        query = BasesParser.parse(
            "views:\n"
            "  - type: table\n"
            "    order: [title]\n"
            "    groupBy: milestone\n"
            "    summaries:\n"
            '      Done: count(status == "Done")\n'
            "      Total: count(title)\n"
        )
        groups = BasesExecutor(notes).aggregate_only(query)
        by_key = {g.key: g.summary for g in groups}
        assert by_key["M1"]["Done"] == 2
        assert by_key["M1"]["Total"] == 3
        assert by_key["M2"]["Done"] == 0
        assert by_key["M2"]["Total"] == 1

    def test_count_predicate_with_or_combinator(self):
        """A predicate may combine clauses (status Done OR Completed)."""
        notes = [
            _note("A", milestone="M1", status="Done"),
            _note("B", milestone="M1", status="Completed"),
            _note("C", milestone="M1", status="Todo"),
        ]
        query = BasesParser.parse(
            "views:\n"
            "  - type: table\n"
            "    order: [title]\n"
            "    groupBy: milestone\n"
            "    summaries:\n"
            '      Done: count(status == "Done" || status == "Completed")\n'
        )
        groups = BasesExecutor(notes).aggregate_only(query)
        assert groups[0].summary["Done"] == 2

    def test_sum_predicate_conditional(self):
        """sum over a predicate sums 1 per matching row (like count)."""
        notes = [
            _note("A", milestone="M1", status="Done"),
            _note("B", milestone="M1", status="Done"),
            _note("C", milestone="M1", status="Todo"),
        ]
        query = BasesParser.parse(
            "views:\n"
            "  - type: table\n"
            "    order: [title]\n"
            "    groupBy: milestone\n"
            "    summaries:\n"
            '      Done: sum(status == "Done")\n'
        )
        groups = BasesExecutor(notes).aggregate_only(query)
        assert groups[0].summary["Done"] == 2

    def test_plain_field_summary_still_works(self):
        """Non-regression: the existing fn(field) form is unchanged."""
        notes = [
            _note("A", milestone="M1", v=2),
            _note("B", milestone="M1", v=3),
        ]
        query = BasesParser.parse(
            "views:\n"
            "  - type: table\n"
            "    order: [title]\n"
            "    groupBy: milestone\n"
            "    summaries:\n"
            "      Total: sum(v)\n"
        )
        groups = BasesExecutor(notes).aggregate_only(query)
        assert groups[0].summary["Total"] == 5


# ---------------------------------------------------------------------------
# BP3-D-02 — Post-agg formula round(Done/Total*100) combines two summaries
# ---------------------------------------------------------------------------
class TestCrossSummaryArithmetic:
    def test_progress_percentage_combines_two_summaries(self):
        notes = [
            _note("A", milestone="M1", status="Done"),
            _note("B", milestone="M1", status="Done"),
            _note("C", milestone="M1", status="Todo"),
            _note("D", milestone="M1", status="Todo"),
        ]
        query = BasesParser.parse(
            "views:\n"
            "  - type: table\n"
            "    order: [title]\n"
            "    groupBy: milestone\n"
            "    summaries:\n"
            '      Done: count(status == "Done")\n'
            "      Total: count(title)\n"
            "    aggFormulas:\n"
            "      Progress: round(Done / Total * 100)\n"
        )
        groups = BasesExecutor(notes).aggregate_only(query)
        # 2 Done / 4 Total = 50%
        assert groups[0].summary["Progress"] == 50

    def test_post_agg_formula_can_reference_a_single_summary(self):
        notes = [
            _note("A", milestone="M1", status="Done"),
            _note("B", milestone="M1", status="Done"),
        ]
        query = BasesParser.parse(
            "views:\n"
            "  - type: table\n"
            "    order: [title]\n"
            "    groupBy: milestone\n"
            "    summaries:\n"
            "      Total: count(title)\n"
            "    aggFormulas:\n"
            "      Doubled: Total * 2\n"
        )
        groups = BasesExecutor(notes).aggregate_only(query)
        assert groups[0].summary["Doubled"] == 4


# ---------------------------------------------------------------------------
# BP3-D-03 — Division by zero guarded (Total == 0 -> None, never raises)
# ---------------------------------------------------------------------------
class TestDivisionByZeroGuarded:
    def test_zero_total_yields_none_not_exception(self):
        # A group with zero rows matching the count source would still have
        # Total == 0 only if there are no rows at all; build a group where the
        # Total summary evaluates to 0 by counting a never-present field.
        notes = [_note("A", milestone="M1", status="Todo")]
        query = BasesParser.parse(
            "views:\n"
            "  - type: table\n"
            "    order: [title]\n"
            "    groupBy: milestone\n"
            "    summaries:\n"
            '      Done: count(status == "Done")\n'
            "      Total: count(missing_field)\n"
            "    aggFormulas:\n"
            "      Progress: round(Done / Total * 100)\n"
        )
        # No exception, block renders; Progress degrades to None on div/0.
        groups = BasesExecutor(notes).aggregate_only(query)
        assert groups[0].summary["Total"] == 0
        assert groups[0].summary["Progress"] is None

    def test_zero_total_block_still_renders(self):
        notes = [_note("A", milestone="M1", status="Todo")]
        query = BasesParser.parse(
            "views:\n"
            "  - type: table\n"
            "    order: [title]\n"
            "    groupBy: milestone\n"
            "    aggregate: true\n"
            "    summaries:\n"
            '      Done: count(status == "Done")\n'
            "      Total: count(missing_field)\n"
            "    aggFormulas:\n"
            "      Progress: round(Done / Total * 100)\n"
        )
        markdown, rows = BasesExecutor(notes).render(query)
        assert markdown  # non-empty, block rendered
        assert "M1" in markdown


# ---------------------------------------------------------------------------
# BP3-D-04 — Aggregate-only view: 1 row per group, zero row-items
# ---------------------------------------------------------------------------
class TestAggregateOnlyView:
    def test_one_row_per_group_no_items(self):
        notes = [
            _note("A", milestone="M1", status="Done"),
            _note("B", milestone="M1", status="Todo"),
            _note("C", milestone="M2", status="Done"),
        ]
        query = BasesParser.parse(
            "views:\n"
            "  - type: table\n"
            "    order: [milestone, Total]\n"
            "    groupBy: milestone\n"
            "    aggregate: true\n"
            "    summaries:\n"
            "      Total: count(title)\n"
        )
        markdown, rows = BasesExecutor(notes).render(query)
        # exactly one row per group (2 groups) — not one per note (3)
        assert len(rows) == 2
        # the row-item titles must NOT appear as table rows
        assert "M1" in markdown and "M2" in markdown

    def test_aggregate_only_columns_are_summaries(self):
        notes = [
            _note("A", milestone="M1", status="Done"),
            _note("B", milestone="M1", status="Todo"),
        ]
        query = BasesParser.parse(
            "views:\n"
            "  - type: table\n"
            "    order: [milestone, Done, Total]\n"
            "    groupBy: milestone\n"
            "    aggregate: true\n"
            "    summaries:\n"
            '      Done: count(status == "Done")\n'
            "      Total: count(title)\n"
        )
        markdown, rows = BasesExecutor(notes).render(query)
        assert len(rows) == 1
        row = rows[0]
        assert row["Done"] == 1
        assert row["Total"] == 2
        # the group key is projected too
        assert row["milestone"] == "M1"

    def test_aggregate_only_applies_column_aliases(self):
        """A ``properties:`` displayName aliases the rendered header AND the cell
        resolves (alias-aware row keying)."""
        notes = [
            _note("A", milestone="M1", status="Done"),
            _note("B", milestone="M1", status="Todo"),
        ]
        query = BasesParser.parse(
            "views:\n"
            "  - type: table\n"
            "    order: [milestone, Total]\n"
            "    groupBy: milestone\n"
            "    aggregate: true\n"
            "    summaries:\n"
            "      Total: count(title)\n"
            "properties:\n"
            "  milestone:\n"
            "    displayName: Milestone\n"
            "  Total:\n"
            "    displayName: Count\n"
        )
        markdown, rows = BasesExecutor(notes).render(query)
        # header uses the aliases (properties: is a top-level key, per schema)
        assert "Milestone" in markdown and "Count" in markdown
        # and the cell value resolves (2) — not an empty column
        assert "| M1 | 2 |" in markdown

    def test_grouped_non_aggregate_view_still_renders_items(self):
        """Non-regression: without aggregate: true, items are still rendered."""
        notes = [
            _note("A", milestone="M1", status="Done"),
            _note("B", milestone="M1", status="Todo"),
        ]
        query = BasesParser.parse(
            "views:\n"
            "  - type: table\n"
            "    order: [title]\n"
            "    groupBy: milestone\n"
            "    summaries:\n"
            "      Total: count(title)\n"
        )
        markdown, rows = BasesExecutor(notes).render(query)
        # both item titles present (grouped sections render items)
        assert "A" in markdown and "B" in markdown
        assert len(rows) == 2


# ---------------------------------------------------------------------------
# BP3-D-05 — A converted Progression block renders the per-milestone table
# ---------------------------------------------------------------------------
class TestProgressionBlockRenders:
    def test_progression_table_per_milestone(self):
        notes = [
            _note("US-1", milestone="M-Bases-P3", type="user-story", status="Done"),
            _note("US-2", milestone="M-Bases-P3", type="user-story", status="Done"),
            _note("US-3", milestone="M-Bases-P3", type="user-story", status="Todo"),
            _note("US-4", milestone="M-Bases-P2", type="user-story", status="Done"),
        ]
        query = BasesParser.parse(
            "views:\n"
            "  - type: table\n"
            "    order: [milestone, Total, Done, Progress]\n"
            "    groupBy: milestone\n"
            "    aggregate: true\n"
            "    summaries:\n"
            "      Total: count(title)\n"
            '      Done: count(status == "Done" || status == "Completed")\n'
            "    aggFormulas:\n"
            "      Progress: round(Done / Total * 100)\n"
        )
        markdown, rows = BasesExecutor(notes).render(query)
        by_key = {r["milestone"]: r for r in rows}
        assert by_key["M-Bases-P3"]["Total"] == 3
        assert by_key["M-Bases-P3"]["Done"] == 2
        assert by_key["M-Bases-P3"]["Progress"] == 67  # round(2/3*100)
        assert by_key["M-Bases-P2"]["Progress"] == 100
        # the rendered markdown is a table with the percent values
        assert "67" in markdown and "100" in markdown


# ---------------------------------------------------------------------------
# BP3-D-06 — Aggregate-only cardinality over the bound -> block inert (limit)
# ---------------------------------------------------------------------------
class TestAggregateOnlyCardinalityBound:
    def test_over_bound_block_inert(self):
        notes = [
            _note(f"n{i}", milestone=f"m{i}") for i in range(MAX_AGG_ONLY_GROUPS + 1)
        ]
        query = BasesParser.parse(
            "views:\n"
            "  - type: table\n"
            "    order: [milestone]\n"
            "    groupBy: milestone\n"
            "    aggregate: true\n"
            "    summaries:\n"
            "      Total: count(title)\n"
        )
        # block inert: aggregate-only does NOT truncate (unlike grouped view) —
        # over the bound it raises BasesLimitError (error_type "limit").
        with pytest.raises(BasesLimitError):
            BasesExecutor(notes).render(query)

    def test_at_bound_renders(self):
        notes = [_note(f"n{i}", milestone=f"m{i}") for i in range(MAX_AGG_ONLY_GROUPS)]
        query = BasesParser.parse(
            "views:\n"
            "  - type: table\n"
            "    order: [milestone]\n"
            "    groupBy: milestone\n"
            "    aggregate: true\n"
            "    summaries:\n"
            "      Total: count(title)\n"
        )
        markdown, rows = BasesExecutor(notes).render(query)
        assert len(rows) == MAX_AGG_ONLY_GROUPS


# ---------------------------------------------------------------------------
# BP3-D-07 — Post-agg arithmetic depth over the bound -> block inert
# ---------------------------------------------------------------------------
class TestPostAggDepthBound:
    def test_deep_post_agg_formula_block_inert(self):
        # A deeply nested arithmetic expression over the AST depth bound makes
        # the block inert at PARSE time (parse_formula enforces MAX_AST_DEPTH).
        deep = "Total" + " + 1" * 400  # nesting far over MAX_AST_DEPTH
        yaml = (
            "views:\n"
            "  - type: table\n"
            "    order: [milestone]\n"
            "    groupBy: milestone\n"
            "    aggregate: true\n"
            "    summaries:\n"
            "      Total: count(title)\n"
            "    aggFormulas:\n"
            f"      X: {deep}\n"
        )
        with pytest.raises(BasesLimitError):
            BasesParser.parse(yaml)
