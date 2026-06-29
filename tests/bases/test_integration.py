"""US-004 — Orchestration + rendering + result envelope (BasesIntegration).

Covers BASES-11 (TABLE render = ResultFormatter.format_table), BASES-12 (LIST),
BASES-13 (multi-block, inert block degradation), and the error envelope policy
(status:error, 5 error_types, never crashes).
"""

from basic_memory.bases.integration import (
    BasesIntegration,
    create_bases_integration,
)
from basic_memory.bases.result_formatter import ResultFormatter


def _note(title, folder, status, priority):
    return {
        "title": title,
        "file": {"path": f"{folder}/{title}.md", "name": title, "folder": folder},
        "created_at": "2026-01-01",
        "updated_at": "2026-01-10",
        "frontmatter": {"status": status, "priority": priority},
    }


def _notes():
    return [
        _note("Alpha", "projects", "Active", 1),
        _note("Beta", "projects", "Archived", 2),
    ]


TABLE_BLOCK = """```base
filters: file.inFolder("projects")
views:
  - type: table
    order: [status]
    sort:
      - property: status
        direction: ASC
```"""

LIST_BLOCK = """```base
filters: status == "Active"
views:
  - type: list
```"""


class TestRendering:
    def test_table_render_matches_formatter(self):
        integ = create_bases_integration(notes_provider=_notes)
        results = integ.process_note(TABLE_BLOCK)
        assert len(results) == 1
        r = results[0]
        assert r["status"] == "success"
        assert r["query_type"] == "table"
        # Byte-identical to ResultFormatter on the same projected rows.
        expected = ResultFormatter.format_table(
            [
                {"status": "Active"},
                {"status": "Archived"},
            ],
            ["status"],
        )
        assert r["result_markdown"] == expected

    def test_list_render_matches_formatter(self):
        integ = create_bases_integration(notes_provider=_notes)
        results = integ.process_note(LIST_BLOCK)
        assert len(results) == 1
        r = results[0]
        assert r["status"] == "success"
        assert r["query_type"] == "list"
        expected = ResultFormatter.format_list([{"file.link": "[[Alpha]]"}])
        assert r["result_markdown"] == expected

    def test_result_count(self):
        integ = create_bases_integration(notes_provider=_notes)
        results = integ.process_note(TABLE_BLOCK)
        assert results[0]["result_count"] == 2

    def test_discovered_links(self):
        integ = create_bases_integration(notes_provider=_notes)
        results = integ.process_note(TABLE_BLOCK)
        links = results[0]["discovered_links"]
        assert any(link["target"] for link in links)


class TestErrorEnvelope:
    def test_malformed_yaml_inert(self):
        integ = create_bases_integration(notes_provider=_notes)
        block = "```base\nviews: [unclosed\n```"
        results = integ.process_note(block)
        assert results[0]["status"] == "error"
        assert results[0]["error_type"] == "parse"

    def test_task_view_unsupported(self):
        integ = create_bases_integration(notes_provider=_notes)
        block = "```base\nviews:\n  - type: task\n```"
        results = integ.process_note(block)
        assert results[0]["status"] == "error"
        assert results[0]["error_type"] == "unsupported"

    def test_no_crash_on_any_block(self):
        integ = create_bases_integration(notes_provider=_notes)
        # garbage that triggers an unexpected path must still produce an envelope
        block = "```base\nfilters: 12345 @@@ !!!\nviews:\n  - type: list\n```"
        results = integ.process_note(block)
        assert results[0]["status"] == "error"
        # error_type is one of the 5 categories
        assert results[0]["error_type"] in {
            "parse",
            "unsupported",
            "limit",
            "execution",
            "unexpected",
        }


class TestMultiBlock:
    def test_multiple_blocks_rendered_independently(self):
        integ = create_bases_integration(notes_provider=_notes)
        content = TABLE_BLOCK + "\n\nsome text\n\n" + LIST_BLOCK
        results = integ.process_note(content)
        assert len(results) == 2
        assert results[0]["status"] == "success"
        assert results[1]["status"] == "success"

    def test_inert_block_does_not_break_valid_block(self):
        integ = create_bases_integration(notes_provider=_notes)
        bad = "```base\nviews: [unclosed\n```"
        content = bad + "\n\n" + LIST_BLOCK
        results = integ.process_note(content)
        assert len(results) == 2
        assert results[0]["status"] == "error"
        assert results[1]["status"] == "success"
        assert results[1]["result_markdown"]

    def test_no_blocks_returns_empty(self):
        integ = create_bases_integration(notes_provider=_notes)
        assert integ.process_note("# Just a note, no base blocks") == []


class TestProcessNoteIsolation:
    def test_process_note_does_not_raise(self):
        integ = BasesIntegration(notes_provider=None)
        # No notes provider -> empty dataset, still no crash.
        results = integ.process_note(LIST_BLOCK)
        assert results[0]["status"] == "success"
        assert results[0]["result_count"] == 0


# ---------------------------------------------------------------------------
# US-005 — Phase 2 aggregation through the full envelope (AC-3 / inert blocks)
# ---------------------------------------------------------------------------
def _flatten_notes():
    # one note whose tags list exceeds MAX_FLATTEN_CARDINALITY (100)
    return [
        {
            "title": "Huge",
            "file": {"path": "projects/Huge.md", "name": "Huge", "folder": "projects"},
            "frontmatter": {"tags": list(range(150))},
        }
    ]


def _group_notes():
    return [
        {
            "title": "Alpha",
            "file": {"path": "projects/Alpha.md", "name": "Alpha", "folder": "projects"},
            "frontmatter": {"type": "project"},
        },
        {
            "title": "Beta",
            "file": {"path": "areas/Beta.md", "name": "Beta", "folder": "areas"},
            "frontmatter": {"type": "area"},
        },
    ]


class TestPhase2Envelope:
    def test_group_by_block_renders_success_with_sections(self):
        block = (
            "```base\n"
            "views:\n"
            "  - type: table\n"
            "    order: [title]\n"
            "    groupBy: type\n"
            "```"
        )
        integ = create_bases_integration(notes_provider=_group_notes)
        r = integ.process_note(block)[0]
        assert r["status"] == "success"
        assert "### project" in r["result_markdown"]
        assert "### area" in r["result_markdown"]

    def test_flatten_over_bound_is_inert_limit(self):
        # ADR-004 §2.(d): FLATTEN over MAX_FLATTEN_CARDINALITY -> block inert,
        # error_type "limit", no crash of the handler.
        block = (
            "```base\n"
            "views:\n"
            "  - type: table\n"
            "    order: [tags]\n"
            "    flatten: tags\n"
            "```"
        )
        integ = create_bases_integration(notes_provider=_flatten_notes)
        r = integ.process_note(block)[0]
        assert r["status"] == "error"
        assert r["error_type"] == "limit"

    def test_aggregate_outside_whitelist_is_inert_unsupported(self):
        # An aggregate outside the closed whitelist -> block inert, error_type
        # "unsupported" (refused at parse, never evaluated).
        block = (
            "```base\n"
            "views:\n"
            "  - type: table\n"
            "    order: [title]\n"
            "    groupBy: type\n"
            "    summaries:\n"
            "      x: eval(title)\n"
            "```"
        )
        integ = create_bases_integration(notes_provider=_group_notes)
        r = integ.process_note(block)[0]
        assert r["status"] == "error"
        assert r["error_type"] == "unsupported"
