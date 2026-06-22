"""Live-path integration tests for ``file.folder`` filtering (US-8 / BP4-H-03,06).

These tests go through the REAL MCP render path (``process_note`` →
``BasesExecutor`` via the provider), with a fixture faithful to the production
provider ``knowledge_router.list_entities_for_dataview`` — including the nested
``file.folder`` field the provider derives from ``file_path``. Per ADR-006
§"Stratégie de vérification" (normative), no synthetic dataset divergent from the
provider is used.

The silent-empty trap (ticket caveat): a member that PARSES but reads a row where
``folder`` is unpopulated renders 0. BP4-H-03 asserts the provider populates
``folder`` AND the filter yields non-empty rows; BP4-H-06 asserts the column
renders the real folder for each row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from basic_memory.bases.integration import create_bases_integration


@dataclass
class _Entity:
    title: str
    file_path: str
    permalink: str | None = None
    note_type: str = "note"
    metadata: dict = field(default_factory=dict)


def build_dataset(entities: list[_Entity]) -> list[dict]:
    """Mirror list_entities_for_dataview (knowledge_router) — nested file.folder."""
    notes: list[dict] = []
    for e in entities:
        note: dict = {
            "file": {
                "path": e.file_path,
                "name": PurePosixPath(e.file_path).name,
                "folder": str(PurePosixPath(e.file_path).parent),
                "outlinks": [],
            },
            "title": e.title,
            "type": e.note_type,
        }
        if e.permalink:
            note["permalink"] = e.permalink
        for k, v in e.metadata.items():
            note.setdefault(k, v)
        notes.append(note)
    return notes


def _entities() -> list[_Entity]:
    return [
        _Entity("Ref A", "resources/references/a.md", "resources/references/a", "note"),
        _Entity("Ref B", "resources/references/b.md", "resources/references/b", "note"),
        _Entity("Project X", "projects/x.md", "projects/x", "project"),
        _Entity("Product Note", "products/bm/m1/stories/us-8.md", "products/bm/m1/stories/us-8"),
    ]


def _provider():
    return build_dataset(_entities())


def _block(body: str) -> str:
    return f"```base\n{body}```"


def _run(body: str) -> dict:
    integ = create_bases_integration(notes_provider=_provider)
    results = integ.process_note(_block(body))
    assert len(results) == 1
    return results[0]


class TestFolderFilterLivePath:
    def test_startswith_filters_by_folder_prefix(self):
        """BP4-H-03: startsWith(file.folder, "resources") → only the resources notes."""
        body = 'filters: startsWith(file.folder, "resources")\nviews:\n  - type: list\n'
        r = _run(body)
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 2  # both resources/references notes

    def test_method_chain_filters_by_folder_prefix(self):
        """BP4-H-03: file.folder.startsWith("products") method-chain form."""
        body = 'filters: file.folder.startsWith("products")\nviews:\n  - type: list\n'
        r = _run(body)
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 1

    def test_endswith_folder_segment(self):
        """BP4-H-03: file.folder.endsWith("stories") on the deep product note."""
        body = 'filters: file.folder.endsWith("stories")\nviews:\n  - type: list\n'
        r = _run(body)
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 1

    def test_folder_not_prefix_excludes(self):
        """A folder prefix that matches nothing renders 0 cleanly (no error)."""
        body = 'filters: startsWith(file.folder, "areas")\nviews:\n  - type: list\n'
        r = _run(body)
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 0


class TestFolderColumnLivePath:
    def test_folder_column_renders_real_value_non_empty(self):
        """BP4-H-06: file.folder column renders the real folder for every row (non-empty)."""
        body = (
            'filters: startsWith(file.folder, "resources")\n'
            "views:\n"
            "  - type: table\n"
            "    order:\n"
            "      - file.name\n"
            "      - file.folder\n"
        )
        r = _run(body)
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 2
        folders = {row.get("file.folder") for row in r["results"]}
        # Real folder value, cells non-empty (the silent-empty-cell trap).
        assert folders == {"resources/references"}
        assert all(v for v in folders)
