"""Dataview executor deprecation — inertness guarantees.

The Dataview→Bases migration is complete (vault 100% Bases). The fork's
Dataview *executor* is deprecated: the ``enable_dataview`` flag stays accepted
on ``read_note`` / ``build_context`` / ``search_notes`` for backward
compatibility, but it is now a **no-op**. ``​```dataview​`` blocks are left
inert (raw markdown), exactly as ``​```base​`` blocks are when
``enable_bases=False``.

These tests pin that contract:

  AC-1 — ``enable_dataview=True`` no longer executes ``​```dataview​``
          blocks: no ``## Dataview Query Results`` section is appended, the raw
          block survives verbatim, and nothing crashes on any of the three read
          paths.
  AC-2 — the executor entry point (``DataviewIntegration.process_note`` /
          ``execute_raw_query``) is neutralised: even if invoked directly it
          performs no execution and returns an empty / inert result. The
          ``enable_dataview`` parameter is still accepted (no-op, retro-compat).

``enable_bases`` is intentionally *not* covered here — it must remain fully
functional and is exercised by ``tests/bases/``.
"""

import pytest

from basic_memory.mcp.tools import (
    read_note,
    write_note,
    build_context,
    search_notes,
)


_DATAVIEW_NOTE = """# Inert Dataview Note

Intro paragraph that must survive verbatim.

```dataview
TABLE status, priority
FROM "test"
WHERE type = "user-story"
```

Trailing paragraph.
"""


def _assert_block_inert(rendered: str) -> None:
    """The raw dataview block survives and no executor section was appended."""
    assert "```dataview" in rendered, "raw dataview block must be preserved inert"
    assert "## Dataview Query Results" not in rendered, (
        "executor must NOT run — no results section may be appended"
    )


@pytest.mark.asyncio
async def test_read_note_dataview_flag_is_inert(app, test_project):
    """AC-1: read_note(enable_dataview=True) leaves the block inert, no crash."""
    await write_note(
        project=test_project.name,
        title="Inert Dataview Note",
        directory="test",
        content=_DATAVIEW_NOTE,
    )

    rendered = await read_note(
        "Inert Dataview Note",
        project=test_project.name,
        enable_dataview=True,
    )

    _assert_block_inert(rendered)
    assert "Intro paragraph that must survive verbatim." in rendered


@pytest.mark.asyncio
async def test_build_context_dataview_flag_is_inert(app, test_project):
    """AC-1: build_context(enable_dataview=True) leaves the block inert, no crash."""
    await write_note(
        project=test_project.name,
        title="Inert Dataview Context",
        directory="test",
        content=_DATAVIEW_NOTE,
    )

    result = await build_context(
        "memory://test/inert-dataview-context",
        project=test_project.name,
        enable_dataview=True,
    )

    # The rendered context (dict payload) must never carry an executor section.
    blob = str(result)
    assert "## Dataview Query Results" not in blob, (
        "executor must NOT run on the build_context path"
    )


@pytest.mark.asyncio
async def test_search_notes_dataview_flag_is_inert(app, test_project):
    """AC-1: search_notes(enable_dataview=True) never attaches executor output."""
    await write_note(
        project=test_project.name,
        title="Inert Dataview Search",
        directory="test",
        content=_DATAVIEW_NOTE,
    )

    result = await search_notes(
        query="Inert Dataview Search",
        project=test_project.name,
        enable_dataview=True,
    )

    # The rendered search payload must never carry executor output. Note:
    # ``dataview_queries`` (index-time detection metadata from search_service)
    # may be present and is harmless — only the *executed* results
    # (``dataview_results`` / ``dataview_query_count``) prove the engine ran.
    blob = str(result)
    assert "dataview_results" not in blob, (
        "executor must NOT attach dataview_results metadata"
    )
    assert "dataview_query_count" not in blob


def test_integration_process_note_is_neutralised():
    """AC-2: the executor entry point is a no-op even if invoked directly."""
    from basic_memory.dataview.integration import create_dataview_integration

    integration = create_dataview_integration()
    results = integration.process_note(_DATAVIEW_NOTE)
    assert results == [], "process_note must execute nothing (deprecated executor)"


def test_integration_execute_raw_query_is_neutralised():
    """AC-2: execute_raw_query no longer runs the engine, returns inert status."""
    from basic_memory.dataview.integration import create_dataview_integration

    integration = create_dataview_integration()
    result = integration.execute_raw_query('TABLE status FROM "test"')
    assert result.get("status") == "deprecated"
    assert result.get("result_count", 0) == 0
    assert not result.get("result_markdown")
