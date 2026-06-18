"""US-005 — Bases aggregation layer (FLATTEN / GROUP BY / summaries).

Unit tests for ``bases/aggregate.py``: the closed aggregate whitelist, the
flatten/group_by/aggregate primitives, and the anti-DoS bounds (ADR-004 §2.(d),
US-001 NC-1/NC-3/NC-4).

Security invariant (ADR-004): aggregates are pure Python functions over lists —
never eval/exec/getattr/import. Any aggregate outside the closed whitelist makes
the block inert (BasesUnsupportedError).
"""

import pytest

from basic_memory.bases.aggregate import (
    AGGREGATE_WHITELIST,
    GroupResult,
    aggregate,
    flatten,
    group_by,
)
from basic_memory.bases.errors import BasesLimitError, BasesUnsupportedError
from basic_memory.bases.schema import (
    MAX_FLATTEN_CARDINALITY,
    MAX_GROUP_BY_GROUPS,
)


def _note(title, **fm):
    """Build a nested-shape note (list_entities_for_dataview shape)."""
    folder = fm.pop("folder", "notes")
    return {
        "title": title,
        "file": {"path": f"{folder}/{title}.md", "name": title, "folder": folder},
        "frontmatter": dict(fm),
    }


# ---------------------------------------------------------------------------
# FLATTEN
# ---------------------------------------------------------------------------
class TestFlatten:
    def test_flatten_expands_list_field_into_one_row_each(self):
        rows = [_note("A", tags=["x", "y", "z"])]
        out = flatten(rows, "tags", MAX_FLATTEN_CARDINALITY)
        # 3 rows, each carrying a scalar tag in frontmatter.tags
        assert len(out) == 3
        assert [r["frontmatter"]["tags"] for r in out] == ["x", "y", "z"]
        # other rows fields preserved
        assert all(r["title"] == "A" for r in out)

    def test_flatten_scalar_field_yields_single_row(self):
        rows = [_note("A", status="Active")]
        out = flatten(rows, "status", MAX_FLATTEN_CARDINALITY)
        assert len(out) == 1
        assert out[0]["frontmatter"]["status"] == "Active"

    def test_flatten_missing_field_yields_single_row_unchanged(self):
        rows = [_note("A", status="Active")]
        out = flatten(rows, "nonexistent", MAX_FLATTEN_CARDINALITY)
        assert len(out) == 1
        assert out[0]["title"] == "A"

    def test_flatten_empty_list_yields_zero_rows(self):
        rows = [_note("A", tags=[])]
        out = flatten(rows, "tags", MAX_FLATTEN_CARDINALITY)
        assert out == []

    def test_flatten_does_not_mutate_input_rows(self):
        rows = [_note("A", tags=["x", "y"])]
        flatten(rows, "tags", MAX_FLATTEN_CARDINALITY)
        # original row's list must be intact (no aliasing / mutation)
        assert rows[0]["frontmatter"]["tags"] == ["x", "y"]

    def test_flatten_multiple_rows_independent(self):
        rows = [_note("A", tags=["x", "y"]), _note("B", tags=["z"])]
        out = flatten(rows, "tags", MAX_FLATTEN_CARDINALITY)
        assert len(out) == 3
        assert [r["title"] for r in out] == ["A", "A", "B"]

    # -- ADVERSARIAL: cardinality bound -> block inert -----------------------
    def test_flatten_over_cardinality_raises_limit(self):
        # one row whose expansion exceeds the per-row bound -> inert (limit)
        rows = [_note("A", tags=list(range(MAX_FLATTEN_CARDINALITY + 1)))]
        with pytest.raises(BasesLimitError):
            flatten(rows, "tags", MAX_FLATTEN_CARDINALITY)

    def test_flatten_at_cardinality_bound_is_allowed(self):
        rows = [_note("A", tags=list(range(MAX_FLATTEN_CARDINALITY)))]
        out = flatten(rows, "tags", MAX_FLATTEN_CARDINALITY)
        assert len(out) == MAX_FLATTEN_CARDINALITY

    def test_flatten_one_bad_row_makes_whole_block_inert(self):
        # ADR-004 §2.(d): FLATTEN over bound = block inert (not partial render).
        # Even if other rows are fine, the limit is raised for the block.
        rows = [
            _note("ok", tags=["a"]),
            _note("bad", tags=list(range(MAX_FLATTEN_CARDINALITY + 5))),
        ]
        with pytest.raises(BasesLimitError):
            flatten(rows, "tags", MAX_FLATTEN_CARDINALITY)


# ---------------------------------------------------------------------------
# GROUP BY
# ---------------------------------------------------------------------------
class TestGroupBy:
    def test_group_by_frontmatter_field(self):
        rows = [
            _note("A", type="project"),
            _note("B", type="area"),
            _note("C", type="project"),
        ]
        result = group_by(rows, "type")
        assert isinstance(result, GroupResult)
        assert result.truncated is False
        keys = [k for k, _ in result.groups]
        assert keys == ["project", "area"]  # insertion order preserved
        proj_rows = dict(result.groups)["project"]
        assert [r["title"] for r in proj_rows] == ["A", "C"]

    def test_group_by_file_star_property(self):
        rows = [_note("A", folder="projects"), _note("B", folder="areas")]
        result = group_by(rows, "file.folder")
        keys = sorted(k for k, _ in result.groups)
        assert keys == ["areas", "projects"]

    def test_group_by_missing_field_groups_under_none(self):
        rows = [_note("A", type="project"), _note("B")]
        result = group_by(rows, "type")
        keys = [k for k, _ in result.groups]
        assert "project" in keys
        # missing -> grouped under a None key
        assert None in keys

    def test_group_by_unhashable_list_value_uses_stable_key(self):
        # a frontmatter list value must not crash group_by (TypeError unhashable)
        rows = [_note("A", type=["a", "b"]), _note("B", type=["a", "b"])]
        result = group_by(rows, "type")
        # both rows land in the same group (same list value)
        assert len(result.groups) == 1
        _, grp = result.groups[0]
        assert len(grp) == 2

    # -- ADVERSARIAL: group count bound -> truncation + marker ---------------
    def test_group_by_over_bound_truncates_and_flags(self):
        # MAX_GROUP_BY_GROUPS + N distinct groups -> truncate, mark truncated
        rows = [_note(f"n{i}", type=f"t{i}") for i in range(MAX_GROUP_BY_GROUPS + 10)]
        result = group_by(rows, "type")
        assert len(result.groups) == MAX_GROUP_BY_GROUPS
        assert result.truncated is True
        assert result.total_groups == MAX_GROUP_BY_GROUPS + 10
        assert result.omitted == 10

    def test_group_by_at_bound_not_truncated(self):
        rows = [_note(f"n{i}", type=f"t{i}") for i in range(MAX_GROUP_BY_GROUPS)]
        result = group_by(rows, "type")
        assert len(result.groups) == MAX_GROUP_BY_GROUPS
        assert result.truncated is False
        assert result.omitted == 0


# ---------------------------------------------------------------------------
# AGGREGATE / summaries
# ---------------------------------------------------------------------------
class TestAggregateWhitelist:
    def test_whitelist_is_a_closed_dict(self):
        assert isinstance(AGGREGATE_WHITELIST, dict)
        # US-001 NC-1 + US-005 DoD: count/sum/average/min/max/list
        for fn in ("count", "sum", "average", "min", "max", "list"):
            assert fn in AGGREGATE_WHITELIST
            assert callable(AGGREGATE_WHITELIST[fn])

    def test_count(self):
        result = group_by(
            [_note("A", type="p", v=1), _note("B", type="p", v=2)], "type"
        )
        out = aggregate(result, {"n": ("count", "v")}, AGGREGATE_WHITELIST)
        assert out[0].summary["n"] == 2

    def test_sum(self):
        result = group_by(
            [_note("A", type="p", v=10), _note("B", type="p", v=5)], "type"
        )
        out = aggregate(result, {"total": ("sum", "v")}, AGGREGATE_WHITELIST)
        assert out[0].summary["total"] == 15

    def test_average(self):
        result = group_by(
            [_note("A", type="p", v=10), _note("B", type="p", v=20)], "type"
        )
        out = aggregate(result, {"avg": ("average", "v")}, AGGREGATE_WHITELIST)
        assert out[0].summary["avg"] == 15

    def test_min_max(self):
        result = group_by(
            [_note("A", type="p", v=3), _note("B", type="p", v=9)], "type"
        )
        out = aggregate(
            result, {"lo": ("min", "v"), "hi": ("max", "v")}, AGGREGATE_WHITELIST
        )
        assert out[0].summary["lo"] == 3
        assert out[0].summary["hi"] == 9

    def test_list(self):
        result = group_by(
            [_note("A", type="p", v="x"), _note("B", type="p", v="y")], "type"
        )
        out = aggregate(result, {"vals": ("list", "v")}, AGGREGATE_WHITELIST)
        assert out[0].summary["vals"] == ["x", "y"]

    def test_aggregate_ignores_missing_values(self):
        # sum/average skip rows lacking the field, never crash
        result = group_by(
            [_note("A", type="p", v=10), _note("B", type="p")], "type"
        )
        out = aggregate(result, {"total": ("sum", "v")}, AGGREGATE_WHITELIST)
        assert out[0].summary["total"] == 10

    # -- ADVERSARIAL: aggregate outside whitelist -> inert ------------------
    def test_aggregate_outside_whitelist_raises_unsupported(self):
        result = group_by([_note("A", type="p", v=1)], "type")
        with pytest.raises(BasesUnsupportedError):
            aggregate(result, {"bad": ("eval", "v")}, AGGREGATE_WHITELIST)

    def test_aggregate_exec_attempt_raises_unsupported(self):
        result = group_by([_note("A", type="p", v=1)], "type")
        with pytest.raises(BasesUnsupportedError):
            aggregate(result, {"bad": ("exec", "v")}, AGGREGATE_WHITELIST)

    def test_aggregate_dunder_attempt_raises_unsupported(self):
        result = group_by([_note("A", type="p", v=1)], "type")
        with pytest.raises(BasesUnsupportedError):
            aggregate(result, {"bad": ("__import__", "v")}, AGGREGATE_WHITELIST)

    def test_aggregate_getattr_attempt_raises_unsupported(self):
        result = group_by([_note("A", type="p", v=1)], "type")
        with pytest.raises(BasesUnsupportedError):
            aggregate(result, {"bad": ("getattr", "v")}, AGGREGATE_WHITELIST)

    def test_whitelist_dispatch_does_not_use_getattr_on_builtins(self):
        # Even a name that IS a real builtin (e.g. 'open', 'compile') must be
        # refused: dispatch is a closed dict lookup, never getattr(builtins,..).
        result = group_by([_note("A", type="p", v=1)], "type")
        for hostile in ("open", "compile", "__builtins__", "globals"):
            with pytest.raises(BasesUnsupportedError):
                aggregate(result, {"x": (hostile, "v")}, AGGREGATE_WHITELIST)
