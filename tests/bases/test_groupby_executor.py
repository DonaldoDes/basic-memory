"""US-005 — GROUP BY / FLATTEN / summaries end-to-end (parser + executor).

Maps the 5 Gherkin scenarios of US-005:
  1. GROUP BY renders groups (each group a distinct TABLE section)   [@integration @smoke]
  2. FLATTEN bounded by MAX_FLATTEN_CARDINALITY -> block inert        [@unit]
  3. GROUP BY over MAX_GROUP_BY_GROUPS -> truncation + visible marker [@unit]
  4. Aggregate outside whitelist -> block inert (unsupported)         [@unit]
  5. Whitelist covers the GROUP BY/summaries patterns, no crash       [@unit]

These exercise the parser (groupBy:/group_by:/flatten:/summaries: now accepted)
and the executor pipeline filter -> flatten -> group_by -> aggregate -> project
-> sort -> limit (ADR-004 §3).
"""

import pytest

from basic_memory.bases.errors import BasesLimitError, BasesUnsupportedError
from basic_memory.bases.executor import BasesExecutor
from basic_memory.bases.parser import BasesParser
from basic_memory.bases.schema import MAX_FLATTEN_CARDINALITY, MAX_GROUP_BY_GROUPS


def _note(title, **fm):
    folder = fm.pop("folder", "notes")
    return {
        "title": title,
        "file": {"path": f"{folder}/{title}.md", "name": title, "folder": folder},
        "frontmatter": dict(fm),
    }


# ---------------------------------------------------------------------------
# Parser now accepts the Phase 2 keys (AC-4)
# ---------------------------------------------------------------------------
class TestParserAcceptsPhase2Keys:
    def test_group_by_camelcase_accepted(self):
        query = BasesParser.parse(
            "views:\n  - type: table\n    order: [type]\n    groupBy: type\n"
        )
        assert query.view.group_by is not None
        assert query.view.group_by.field == "type"

    def test_group_by_snakecase_accepted(self):
        query = BasesParser.parse(
            "views:\n  - type: table\n    order: [type]\n    group_by: type\n"
        )
        assert query.view.group_by is not None
        assert query.view.group_by.field == "type"

    def test_flatten_accepted(self):
        query = BasesParser.parse(
            "views:\n  - type: table\n    order: [tags]\n    flatten: tags\n"
        )
        assert query.view.flatten == "tags"

    def test_summaries_accepted_at_view_level(self):
        query = BasesParser.parse(
            "views:\n"
            "  - type: table\n"
            "    order: [type]\n"
            "    groupBy: type\n"
            "    summaries:\n"
            "      n: count(type)\n"
        )
        assert query.view.group_by is not None
        # summaries parsed into the view as {name: (fn, field)}
        assert query.view.summaries == {"n": ("count", "type")}

    def test_group_by_must_be_string(self):
        from basic_memory.bases.errors import BasesParseError

        with pytest.raises(BasesParseError):
            BasesParser.parse(
                "views:\n  - type: table\n    order: [type]\n    groupBy: [type, status]\n"
            )

    def test_summaries_without_group_by_rejected(self):
        # summaries only make sense with a group_by; standalone is unsupported
        with pytest.raises((BasesUnsupportedError, Exception)):
            BasesParser.parse(
                "views:\n  - type: table\n    order: [type]\n    summaries:\n      n: count(type)\n"
            )


# ---------------------------------------------------------------------------
# Scenario 1: GROUP BY renders the groups (integration smoke)
# ---------------------------------------------------------------------------
class TestGroupByRendersGroups:
    def test_group_by_type_renders_distinct_sections(self):
        notes = [
            _note("A", type="project", status="Active"),
            _note("B", type="area", status="Active"),
            _note("C", type="project", status="Archived"),
        ]
        query = BasesParser.parse(
            "views:\n  - type: table\n    order: [title]\n    groupBy: type\n"
        )
        markdown, rows = BasesExecutor(notes).render(query)
        # each group value appears as a visible section heading/label
        assert "project" in markdown
        assert "area" in markdown
        # rows from both groups present
        assert "A" in markdown and "B" in markdown and "C" in markdown

    def test_group_by_with_count_summary(self):
        notes = [
            _note("A", type="project"),
            _note("B", type="project"),
            _note("C", type="area"),
        ]
        query = BasesParser.parse(
            "views:\n"
            "  - type: table\n"
            "    order: [title]\n"
            "    groupBy: type\n"
            "    summaries:\n"
            "      total: count(title)\n"
        )
        markdown, rows = BasesExecutor(notes).render(query)
        # the count for the project group (2) must be visible
        assert "2" in markdown
        assert "project" in markdown and "area" in markdown


# ---------------------------------------------------------------------------
# Scenario 2: FLATTEN over the bound -> block inert (limit)
# ---------------------------------------------------------------------------
class TestFlattenBound:
    def test_flatten_expands_rows(self):
        notes = [_note("A", tags=["x", "y"]), _note("B", tags=["z"])]
        query = BasesParser.parse(
            "views:\n  - type: table\n    order: [tags]\n    flatten: tags\n"
        )
        rows = BasesExecutor(notes).select(query)
        # 2 + 1 expanded rows
        assert len(rows) == 3

    def test_flatten_over_cardinality_block_inert(self):
        notes = [_note("A", tags=list(range(MAX_FLATTEN_CARDINALITY + 1)))]
        query = BasesParser.parse(
            "views:\n  - type: table\n    order: [tags]\n    flatten: tags\n"
        )
        # block inert: the executor raises BasesLimitError, caught upstream by
        # the integration envelope -> error_type "limit".
        with pytest.raises(BasesLimitError):
            BasesExecutor(notes).select(query)


# ---------------------------------------------------------------------------
# Scenario 3: GROUP BY over the bound -> truncation + visible marker
# ---------------------------------------------------------------------------
class TestGroupByBound:
    def test_group_by_over_bound_truncates_with_marker(self):
        notes = [_note(f"n{i}", type=f"t{i}") for i in range(MAX_GROUP_BY_GROUPS + 7)]
        query = BasesParser.parse(
            "views:\n  - type: table\n    order: [title]\n    groupBy: type\n"
        )
        markdown, rows = BasesExecutor(notes).render(query)
        # a visible truncation marker mentioning the omitted count
        assert "7" in markdown
        assert "non affichés" in markdown or "not shown" in markdown


# ---------------------------------------------------------------------------
# Scenario 4: aggregate outside whitelist -> block inert (unsupported)
# ---------------------------------------------------------------------------
class TestAggregateWhitelistEndToEnd:
    def test_unknown_aggregate_refused_at_parse(self):
        # an aggregate function outside the whitelist must make the block inert.
        with pytest.raises(BasesUnsupportedError):
            BasesParser.parse(
                "views:\n"
                "  - type: table\n"
                "    order: [type]\n"
                "    groupBy: type\n"
                "    summaries:\n"
                "      bad: eval(type)\n"
            )

    def test_exec_aggregate_refused_at_parse(self):
        with pytest.raises(BasesUnsupportedError):
            BasesParser.parse(
                "views:\n"
                "  - type: table\n"
                "    order: [type]\n"
                "    groupBy: type\n"
                "    summaries:\n"
                "      bad: exec(type)\n"
            )


# ---------------------------------------------------------------------------
# Scenario 5: whitelist covers the GROUP BY/summaries patterns, no crash
# ---------------------------------------------------------------------------
class TestWhitelistCoverage:
    @pytest.mark.parametrize("fn", ["count", "sum", "average", "min", "max", "list"])
    def test_each_whitelisted_aggregate_executes(self, fn):
        notes = [
            _note("A", type="p", v=1),
            _note("B", type="p", v=2),
        ]
        query = BasesParser.parse(
            "views:\n"
            "  - type: table\n"
            "    order: [title]\n"
            "    groupBy: type\n"
            "    summaries:\n"
            f"      s: {fn}(v)\n"
        )
        # no crash, renders successfully
        markdown, rows = BasesExecutor(notes).render(query)
        assert markdown  # non-empty render
