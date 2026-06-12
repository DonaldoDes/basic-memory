"""US-005 — MCP wiring of enable_bases (BASES-14/15/16).

These tests deliberately avoid invoking the FastMCP tool wrappers through the
broken-on-FastMCP-v3 `.fn` path. They assert:
  - signature presence + symmetric defaults (True/True/False) vs enable_dataview,
  - the _enrich_with_bases helper renders / no-ops / degrades correctly,
  - flag independence (bases enrichment is reachable without dataview),
  - core is untouched outside the 3 flag-bearing tools (asserted in US-005 commit
    + test_core_untouched below via import-surface check).
"""

import inspect

import pytest

from basic_memory.mcp.tools.read_note import read_note, _enrich_with_bases
from basic_memory.mcp.tools.build_context import build_context
from basic_memory.mcp.tools.search import search_notes


# ---------------------------------------------------------------------------
# Signature + symmetric defaults
# ---------------------------------------------------------------------------
class TestSignatures:
    def test_read_note_has_enable_bases_default_true(self):
        sig = inspect.signature(read_note)
        assert "enable_bases" in sig.parameters
        assert sig.parameters["enable_bases"].default is True
        # symmetry with enable_dataview
        assert sig.parameters["enable_dataview"].default is True

    def test_build_context_has_enable_bases_default_true(self):
        sig = inspect.signature(build_context)
        assert "enable_bases" in sig.parameters
        assert sig.parameters["enable_bases"].default is True
        assert sig.parameters["enable_dataview"].default is True

    def test_search_has_enable_bases_default_false(self):
        sig = inspect.signature(search_notes)
        assert "enable_bases" in sig.parameters
        assert sig.parameters["enable_bases"].default is False
        assert sig.parameters["enable_dataview"].default is False


# ---------------------------------------------------------------------------
# _enrich_with_bases helper behavior (no MCP fixtures needed)
# ---------------------------------------------------------------------------
class _FakeKnowledgeClient:
    def __init__(self, notes):
        self._notes = notes

    async def list_entities_for_dataview(self):
        return self._notes


def _notes():
    return [
        {
            "title": "Alpha",
            "file": {"path": "projects/Alpha.md", "name": "Alpha", "folder": "projects"},
            "created_at": "2026-01-01",
            "updated_at": "2026-01-10",
            "frontmatter": {"status": "Active"},
        }
    ]


BASE_NOTE = """# Note

```base
filters: file.inFolder("projects")
views:
  - type: list
```
"""


class TestEnrichWithBases:
    @pytest.mark.asyncio
    async def test_renders_base_block(self):
        kc = _FakeKnowledgeClient(_notes())
        enriched = await _enrich_with_bases(BASE_NOTE, "proj", kc)
        assert "## Bases Query Results" in enriched
        assert "[[Alpha]]" in enriched

    @pytest.mark.asyncio
    async def test_no_base_block_is_noop(self):
        kc = _FakeKnowledgeClient(_notes())
        content = "# Plain note\n\nNo base blocks here.\n"
        enriched = await _enrich_with_bases(content, "proj", kc)
        assert enriched == content

    @pytest.mark.asyncio
    async def test_knowledge_client_failure_returns_original(self):
        class _Boom:
            async def list_entities_for_dataview(self):
                raise RuntimeError("boom")

        enriched = await _enrich_with_bases(BASE_NOTE, "proj", _Boom())
        # Degradation: original content returned, no crash.
        assert enriched == BASE_NOTE

    @pytest.mark.asyncio
    async def test_inert_block_still_renders_section(self):
        kc = _FakeKnowledgeClient(_notes())
        note = "```base\nviews: [unclosed\n```"
        enriched = await _enrich_with_bases(note, "proj", kc)
        assert "## Bases Query Results" in enriched
        assert "error" in enriched.lower()


# ---------------------------------------------------------------------------
# Flag independence: dataview and bases coexist on the same note (BASES-16)
# ---------------------------------------------------------------------------
class TestCoexistence:
    @pytest.mark.asyncio
    async def test_bases_renders_independently_of_dataview_block(self):
        kc = _FakeKnowledgeClient(_notes())
        mixed = """# Mixed

```dataview
LIST FROM "projects"
```

```base
filters: file.inFolder("projects")
views:
  - type: list
```
"""
        enriched = await _enrich_with_bases(mixed, "proj", kc)
        # The bases enricher only touches base blocks; the dataview block text
        # remains in the (unmodified) original content portion.
        assert "## Bases Query Results" in enriched
        assert "```dataview" in enriched  # dataview block left intact by bases


# ---------------------------------------------------------------------------
# Core untouched: only the 3 flag-bearing tools import the bases integration.
# ---------------------------------------------------------------------------
class TestCoreUntouched:
    def test_only_three_tools_import_bases_integration(self):
        import subprocess

        # grep the mcp tools tree for create_bases_integration / _enrich_with_bases
        out = subprocess.run(
            [
                "grep",
                "-rl",
                "--include=*.py",
                "bases",
                "src/basic_memory/mcp/tools/",
            ],
            capture_output=True,
            text=True,
        ).stdout
        touched = {line.split("/")[-1] for line in out.strip().split("\n") if line}
        assert touched <= {"read_note.py", "build_context.py", "search.py"}
