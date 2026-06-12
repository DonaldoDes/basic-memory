"""US-003 — Bases filter evaluator (FROM/WHERE/SORT/LIMIT).

Covers BASES-07 (parity functions), BASES-09 (field resolution), BASES-10
(and/or/not + sort + limit). Rendering (BASES-11/12) is US-004 but the executor
returns a structured result here.
"""

from basic_memory.bases.executor import BasesExecutor
from basic_memory.bases.parser import BasesParser


def _titles(rows):
    return [r["title"] for r in rows]


class TestFromSource:
    def test_from_prefix_filters(self, sample_notes):
        query = BasesParser.parse(
            'filters: file.inFolder("projects")\nviews:\n  - type: list\n'
        )
        rows = BasesExecutor(sample_notes).select(query)
        assert set(_titles(rows)) == {"Project Alpha", "Project Beta"}

    def test_no_from_returns_all(self, sample_notes):
        query = BasesParser.parse("views:\n  - type: list\n")
        rows = BasesExecutor(sample_notes).select(query)
        assert len(rows) == 3


class TestWhereOperators:
    def test_equality_on_frontmatter(self, sample_notes):
        query = BasesParser.parse(
            'filters: status == "Active"\nviews:\n  - type: list\n'
        )
        rows = BasesExecutor(sample_notes).select(query)
        assert set(_titles(rows)) == {"Project Alpha", "Area Dev"}

    def test_and_with_comparison(self, sample_notes):
        query = BasesParser.parse(
            'filters:\n  and:\n    - status == "Active"\n    - priority < 3\nviews:\n  - type: list\n'
        )
        rows = BasesExecutor(sample_notes).select(query)
        assert _titles(rows) == ["Project Alpha"]

    def test_or(self, sample_notes):
        query = BasesParser.parse(
            'filters:\n  or:\n    - status == "Archived"\n    - priority >= 3\nviews:\n  - type: list\n'
        )
        rows = BasesExecutor(sample_notes).select(query)
        assert set(_titles(rows)) == {"Project Beta", "Area Dev"}

    def test_not(self, sample_notes):
        query = BasesParser.parse(
            'filters:\n  not:\n    - status == "Active"\nviews:\n  - type: list\n'
        )
        rows = BasesExecutor(sample_notes).select(query)
        assert _titles(rows) == ["Project Beta"]

    def test_not_equal(self, sample_notes):
        query = BasesParser.parse(
            'filters: status != "Active"\nviews:\n  - type: list\n'
        )
        rows = BasesExecutor(sample_notes).select(query)
        assert _titles(rows) == ["Project Beta"]


class TestParityFunctions:
    def test_contains_method_form(self, sample_notes):
        query = BasesParser.parse(
            'filters: tags.contains("dev")\nviews:\n  - type: list\n'
        )
        rows = BasesExecutor(sample_notes).select(query)
        assert set(_titles(rows)) == {"Project Alpha", "Area Dev"}

    def test_contains_global_form(self, sample_notes):
        query = BasesParser.parse(
            'filters: contains(tags, "dev")\nviews:\n  - type: list\n'
        )
        rows = BasesExecutor(sample_notes).select(query)
        assert set(_titles(rows)) == {"Project Alpha", "Area Dev"}

    def test_lower(self, sample_notes):
        query = BasesParser.parse(
            'filters: lower(status) == "active"\nviews:\n  - type: list\n'
        )
        rows = BasesExecutor(sample_notes).select(query)
        assert set(_titles(rows)) == {"Project Alpha", "Area Dev"}

    def test_upper(self, sample_notes):
        query = BasesParser.parse(
            'filters: upper(status) == "ARCHIVED"\nviews:\n  - type: list\n'
        )
        rows = BasesExecutor(sample_notes).select(query)
        assert _titles(rows) == ["Project Beta"]

    def test_length(self, sample_notes):
        # tags lists: Alpha=2, Beta=1, Dev=2 -> length 2
        query = BasesParser.parse(
            "filters: length(tags) == 2\nviews:\n  - type: list\n"
        )
        rows = BasesExecutor(sample_notes).select(query)
        assert set(_titles(rows)) == {"Project Alpha", "Area Dev"}


class TestFieldResolution:
    def test_file_name(self, sample_notes):
        query = BasesParser.parse(
            'filters: file.name == "Area Dev"\nviews:\n  - type: list\n'
        )
        rows = BasesExecutor(sample_notes).select(query)
        assert _titles(rows) == ["Area Dev"]

    def test_file_folder(self, sample_notes):
        query = BasesParser.parse(
            'filters: file.folder == "areas"\nviews:\n  - type: list\n'
        )
        rows = BasesExecutor(sample_notes).select(query)
        assert _titles(rows) == ["Area Dev"]


class TestSortLimit:
    def test_sort_asc(self, sample_notes):
        query = BasesParser.parse(
            "views:\n  - type: table\n    order: [priority]\n    sort:\n      - property: priority\n        direction: ASC\n"
        )
        rows = BasesExecutor(sample_notes).select(query)
        assert [r["priority"] for r in rows] == [1, 2, 3]

    def test_sort_desc(self, sample_notes):
        query = BasesParser.parse(
            "views:\n  - type: table\n    order: [priority]\n    sort:\n      - property: priority\n        direction: DESC\n"
        )
        rows = BasesExecutor(sample_notes).select(query)
        assert [r["priority"] for r in rows] == [3, 2, 1]

    def test_limit(self, sample_notes):
        query = BasesParser.parse(
            "views:\n  - type: table\n    order: [priority]\n    sort:\n      - property: priority\n        direction: ASC\n    limit: 2\n"
        )
        rows = BasesExecutor(sample_notes).select(query)
        assert [r["priority"] for r in rows] == [1, 2]

    def test_multi_key_sort_none_last(self, sample_notes):
        # Add a note with missing priority to verify None sorts last.
        notes = sample_notes + [
            {
                "title": "No Priority",
                "file": {"path": "projects/np.md", "name": "No Priority", "folder": "projects"},
                "created_at": "2026-01-01",
                "updated_at": "2026-01-01",
                "frontmatter": {"type": "project", "status": "Active"},
            }
        ]
        query = BasesParser.parse(
            "views:\n  - type: table\n    order: [priority]\n    sort:\n      - property: priority\n        direction: ASC\n"
        )
        rows = BasesExecutor(notes).select(query)
        assert rows[-1]["title"] == "No Priority"


class TestAdversarialEvaluation:
    def test_unknown_function_refused_at_parse(self, sample_notes):
        import pytest
        from basic_memory.bases.errors import BasesUnsupportedError

        with pytest.raises(BasesUnsupportedError):
            BasesParser.parse(
                'filters: round(priority) == 1\nviews:\n  - type: list\n'
            )

    def test_infolder_traversal_stays_in_dataset(self, sample_notes):
        # file.inFolder("../../etc") is a prefix match on entity paths only;
        # no entity path starts with that, and no filesystem access occurs.
        query = BasesParser.parse(
            'filters: file.inFolder("../../etc")\nviews:\n  - type: list\n'
        )
        rows = BasesExecutor(sample_notes).select(query)
        assert rows == []

    def test_infolder_absolute_path_stays_in_dataset(self, sample_notes):
        query = BasesParser.parse(
            'filters: file.inFolder("/etc/passwd")\nviews:\n  - type: list\n'
        )
        rows = BasesExecutor(sample_notes).select(query)
        assert rows == []


class TestEvaluationDegradation:
    def test_note_raising_during_eval_is_skipped(self, sample_notes):
        # comparing a string status to a number must not crash; the note is
        # simply excluded (mirror DataviewExecutor._filter_by_where).
        query = BasesParser.parse(
            "filters: status < 5\nviews:\n  - type: list\n"
        )
        rows = BasesExecutor(sample_notes).select(query)
        # str < int raises TypeError -> all excluded, no crash
        assert rows == []
