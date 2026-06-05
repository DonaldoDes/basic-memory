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
async def test_sync_entity_vectors_batch_task_registered_and_maps_to_search_service(tmp_path):
    """The batch task is registered in get_task_scheduler and maps to
    SearchService.sync_entity_vectors_batch (an unregistered name raises)."""
    import asyncio
    from pathlib import Path
    from typing import cast

    from basic_memory.config import BasicMemoryConfig, ProjectConfig

    class StubSyncService:
        async def sync(self, home: Path, name: str, force_full: bool = False) -> None:
            raise AssertionError("sync_project should not run")  # pragma: no cover

    class StubSearchService:
        def __init__(self) -> None:
            self.batches: list[list[int]] = []

        async def sync_entity_vectors_batch(self, entity_ids: list[int]) -> None:
            self.batches.append(entity_ids)

    search_service = StubSearchService()
    app_config = BasicMemoryConfig(
        env="test",
        projects={"test-project": str(tmp_path)},
        default_project="test-project",
        semantic_search_enabled=True,
    )
    scheduler = await get_task_scheduler(
        sync_service=cast(Any, StubSyncService()),
        search_service=cast(Any, search_service),
        project_config=ProjectConfig(name="test-project", home=tmp_path),
        app_config=app_config,
    )
    # Enable background tasks for this test — uses stubs, no real DB race risk
    cast(Any, scheduler)._test_mode = False
    scheduler.schedule("sync_entity_vectors_batch", entity_ids=[1, 2, 3], project_id="x")
    await asyncio.sleep(0.05)

    assert search_service.batches == [[1, 2, 3]]

    # Fail-fast invariant preserved: unknown tasks are never silently dropped.
    with pytest.raises(ValueError, match="Unknown task name"):
        scheduler.schedule("not-a-registered-task")
