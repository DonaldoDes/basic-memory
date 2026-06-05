"""Integration tests for the bulk_edit_notes MCP tool.

Covers spec test IDs (specs/bulk-edit-notes/spec.md):
- BULK-06: FTS consistency after a batch — search finds the new content once
  background tasks have flushed (C-2 indexing path).
- BULK-07: vector sync is scheduled exactly once per batch via the
  sync_entity_vectors_batch task. The local scheduler is a no-op in test env
  (deps/services.py), so the assertion targets the schedule() call via a spy.
"""

from typing import Any

import pytest
from fastmcp import Client

from basic_memory.deps.services import get_task_scheduler


@pytest.mark.asyncio
async def test_bulk_edit_fts_consistency_after_batch(mcp_server, app, test_project):
    """BULK-06: 20 notes edited in one batch are searchable on the new content."""
    async with Client(mcp_server) as client:
        for i in range(20):
            await client.call_tool(
                "write_note",
                {
                    "project": test_project.name,
                    "title": f"FTS Batch Note {i}",
                    "directory": "fts-batch",
                    "content": f"# FTS Batch Note {i}\n\nbulkfts-old-token body {i}",
                },
            )

        edits = [
            {
                "identifier": f"fts-batch/fts-batch-note-{i}",
                "operation": "find_replace",
                "content": "bulkfts-new-token",
                "find_text": "bulkfts-old-token",
            }
            for i in range(20)
        ]

        bulk_result = await client.call_tool(
            "bulk_edit_notes",
            {"edits": edits, "project": test_project.name},
        )
        report = bulk_result.content[0].text
        assert "20/20" in report

        # ASGITransport awaits Starlette background tasks before returning,
        # so the C-2 FTS indexing has flushed by the time the tool replies.
        search_result = await client.call_tool(
            "search_notes",
            {
                "project": test_project.name,
                "query": "bulkfts-new-token",
                "page_size": 30,
            },
        )
        result_text = search_result.content[0].text
        for i in range(20):
            assert f"fts-batch/fts-batch-note-{i}" in result_text


@pytest.mark.asyncio
async def test_bulk_edit_schedules_vector_sync_batch_exactly_once(
    mcp_server, app, app_config, test_project
):
    """BULK-07: one sync_entity_vectors_batch schedule per batch, never per note."""
    scheduled: list[dict[str, Any]] = []

    class SchedulerSpy:
        def schedule(self, task_name: str, **payload: Any) -> None:
            scheduled.append({"task_name": task_name, "payload": payload})

    app_config.semantic_search_enabled = True
    app.dependency_overrides[get_task_scheduler] = lambda: SchedulerSpy()
    try:
        async with Client(mcp_server) as client:
            for i in range(3):
                await client.call_tool(
                    "write_note",
                    {
                        "project": test_project.name,
                        "title": f"Vector Batch Note {i}",
                        "directory": "vector-batch",
                        "content": f"# Vector Batch Note {i}\n\nbody {i}",
                    },
                )

            scheduled.clear()  # ignore tasks scheduled by note creation

            edits = [
                {
                    "identifier": f"vector-batch/vector-batch-note-{i}",
                    "operation": "append",
                    "content": "vector sync payload",
                }
                for i in range(3)
            ]
            result = await client.call_tool(
                "bulk_edit_notes",
                {"edits": edits, "project": test_project.name},
            )
            report = result.content[0].text
            assert "3/3" in report

        batch_tasks = [t for t in scheduled if t["task_name"] == "sync_entity_vectors_batch"]
        per_note_tasks = [t for t in scheduled if t["task_name"] == "sync_entity_vectors"]

        # Exactly one batch task, zero per-note tasks.
        assert len(batch_tasks) == 1
        assert len(per_note_tasks) == 0
        assert len(batch_tasks[0]["payload"]["entity_ids"]) == 3
    finally:
        app.dependency_overrides.pop(get_task_scheduler, None)
        app_config.semantic_search_enabled = False


@pytest.mark.asyncio
async def test_bulk_edit_registered_task_name_is_known_to_scheduler():
    """The batch task name must be registered: schedule() fails fast otherwise."""
    from basic_memory.deps.services import LocalTaskScheduler

    captured: list[list[int]] = []

    async def handler(entity_ids: list[int], **_: Any) -> None:  # pragma: no cover
        captured.append(entity_ids)

    scheduler = LocalTaskScheduler(
        {"sync_entity_vectors_batch": handler},
        test_mode=True,
    )
    # Known task: no-op in test mode but does not raise.
    scheduler.schedule("sync_entity_vectors_batch", entity_ids=[1, 2], project_id="x")

    with pytest.raises(ValueError, match="Unknown task name"):
        scheduler.schedule("not-a-registered-task")
