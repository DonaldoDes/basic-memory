"""US-d adversarial — cross-summary arithmetic + conditional aggregates.

Zone sensible (ADR-005 §Axe 4/5 invariants): predicates and post-agg formulas
are evaluated in the CLOSED Phase 2 sandbox. These tests assert the negative
invariants ("ce qui ne doit JAMAIS arriver"):

  ADV-1  No eval/exec/getattr path: a hostile predicate/post-agg expression is
         refused at parse (block inert), never executed.
  ADV-2  Division by zero in a post-agg formula NEVER raises — degrades to None.
  ADV-3  Aggregate-only cardinality bomb -> block inert (limit), never a partial
         materialisation of unbounded groups.
  ADV-4  Post-agg arithmetic depth bomb -> block inert (limit).
  ADV-5  A post-agg formula referencing an undeclared summary degrades to None,
         never resolves to a host symbol / builtin.
  ADV-6  A hostile predicate (dunder / import / lambda body) is refused — the
         sandbox whitelist holds on the predicate path exactly as on the
         column-formula path.
  ADV-7  Aggregate-only never reaches the filesystem: a crafted milestone value
         containing a path traversal is treated as an opaque group key only.
"""

import pytest

from basic_memory.bases.errors import (
    BasesLimitError,
    BasesParseError,
    BasesUnsupportedError,
)
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


# ADV-1 / ADV-6 — hostile predicate refused at parse (no eval/exec/getattr).
class TestHostilePredicateRefused:
    @pytest.mark.parametrize(
        "predicate",
        [
            "__import__('os')",
            "status.__class__",
            "eval('1')",
            "exec('x=1')",
            "open('/etc/passwd')",
            "().__class__.__bases__",
            "globals()",
        ],
    )
    def test_hostile_predicate_block_inert(self, predicate):
        yaml = (
            "views:\n"
            "  - type: table\n"
            "    order: [title]\n"
            "    groupBy: milestone\n"
            "    summaries:\n"
            f"      Bad: count({predicate})\n"
        )
        with pytest.raises((BasesParseError, BasesUnsupportedError, BasesLimitError)):
            BasesParser.parse(yaml)

    @pytest.mark.parametrize(
        "expr",
        [
            "__import__('os')",
            "Total.__class__",
            "eval('2')",
            "open('x')",
        ],
    )
    def test_hostile_post_agg_formula_block_inert(self, expr):
        yaml = (
            "views:\n"
            "  - type: table\n"
            "    order: [milestone]\n"
            "    groupBy: milestone\n"
            "    aggregate: true\n"
            "    summaries:\n"
            "      Total: count(title)\n"
            "    aggFormulas:\n"
            f"      Bad: {expr}\n"
        )
        with pytest.raises((BasesParseError, BasesUnsupportedError, BasesLimitError)):
            BasesParser.parse(yaml)


# ADV-2 — div by zero degrades to None, never raises.
class TestDivisionByZeroNeverRaises:
    def test_div_zero_degrades_to_none(self):
        notes = [_note("A", milestone="M1", status="Todo")]
        query = BasesParser.parse(
            "views:\n"
            "  - type: table\n"
            "    order: [milestone]\n"
            "    groupBy: milestone\n"
            "    aggregate: true\n"
            "    summaries:\n"
            '      Done: count(status == "Done")\n'
            "      Total: count(missing)\n"
            "    aggFormulas:\n"
            "      Progress: round(Done / Total * 100)\n"
        )
        groups = BasesExecutor(notes).aggregate_only(query)
        assert groups[0].summary["Progress"] is None

    def test_div_zero_via_integration_envelope_is_success(self):
        # End-to-end: a div-by-zero block is NOT an error envelope — it renders
        # with a degraded (None) cell, status success.
        from basic_memory.bases.integration import BasesIntegration

        notes = [_note("A", milestone="M1", status="Todo")]
        integ = BasesIntegration(notes_provider=lambda: notes)
        block = (
            "views:\n"
            "  - type: table\n"
            "    order: [milestone]\n"
            "    groupBy: milestone\n"
            "    aggregate: true\n"
            "    summaries:\n"
            '      Done: count(status == "Done")\n'
            "      Total: count(missing)\n"
            "    aggFormulas:\n"
            "      Progress: round(Done / Total * 100)\n"
        )
        result = integ.execute_raw_block(block)
        assert result["status"] == "success"


# ADV-3 — aggregate-only cardinality bomb -> inert (limit).
class TestCardinalityBomb:
    def test_cardinality_bomb_inert(self):
        notes = [
            _note(f"n{i}", milestone=f"m{i}")
            for i in range(MAX_AGG_ONLY_GROUPS + 50)
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
        with pytest.raises(BasesLimitError):
            BasesExecutor(notes).render(query)

    def test_cardinality_bomb_integration_limit_envelope(self):
        from basic_memory.bases.integration import BasesIntegration

        notes = [
            _note(f"n{i}", milestone=f"m{i}")
            for i in range(MAX_AGG_ONLY_GROUPS + 50)
        ]
        integ = BasesIntegration(notes_provider=lambda: notes)
        block = (
            "views:\n"
            "  - type: table\n"
            "    order: [milestone]\n"
            "    groupBy: milestone\n"
            "    aggregate: true\n"
            "    summaries:\n"
            "      Total: count(title)\n"
        )
        result = integ.execute_raw_block(block)
        assert result["status"] == "error"
        assert result["error_type"] == "limit"


# ADV-4 — post-agg depth bomb -> inert (limit).
class TestDepthBomb:
    def test_depth_bomb_inert_at_parse(self):
        deep = "Total" + " + 1" * 500
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


# ADV-5 — undeclared summary in a post-agg formula degrades to None.
class TestUndeclaredSummaryReference:
    def test_undeclared_summary_is_none_not_host_symbol(self):
        notes = [_note("A", milestone="M1", status="Done")]
        query = BasesParser.parse(
            "views:\n"
            "  - type: table\n"
            "    order: [milestone]\n"
            "    groupBy: milestone\n"
            "    aggregate: true\n"
            "    summaries:\n"
            "      Total: count(title)\n"
            "    aggFormulas:\n"
            "      X: NotASummary + 1\n"
        )
        groups = BasesExecutor(notes).aggregate_only(query)
        # NotASummary resolves to None; None + 1 degrades the cell to None
        # (never a host builtin/global), block still renders.
        assert groups[0].summary["X"] is None


# ADV-7 — aggregate-only never reaches the filesystem.
class TestNoFilesystemAccess:
    def test_path_traversal_group_key_is_opaque(self):
        notes = [
            _note("A", milestone="../../etc/passwd"),
            _note("B", milestone="../../etc/passwd"),
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
        groups = BasesExecutor(notes).aggregate_only(query)
        # the traversal string is an opaque group key — counted, never opened.
        assert len(groups) == 1
        assert groups[0].key == "../../etc/passwd"
        assert groups[0].summary["Total"] == 2
