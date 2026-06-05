"""Benchmark for bulk_edit_notes vs sequential edit_note calls (BULK-10).

Measures wall-clock time for editing 50 notes:
- baseline: 50 individual edit_note MCP calls
- batch: 1 bulk_edit_notes MCP call

The batch path benefits from C-1 (no per-item resolve round-trip),
C-2 (FTS indexing deferred to background tasks) and a single batched
vector sync at the end of the batch.

Run with: BASIC_MEMORY_ENV=test uv run pytest --no-cov -m benchmark test-int/test_bulk_edit_benchmark.py
"""

import time

import pytest
from fastmcp import Client

NOTE_COUNT = 50


async def _create_notes(client, project_name: str, prefix: str) -> None:
    for i in range(NOTE_COUNT):
        await client.call_tool(
            "write_note",
            {
                "project": project_name,
                "title": f"{prefix} Note {i}",
                "directory": f"bench-{prefix}",
                "content": f"# {prefix} Note {i}\n\nbench-old-token body {i}",
            },
        )


@pytest.mark.benchmark
@pytest.mark.asyncio
async def test_bulk_edit_vs_sequential_edit_benchmark(mcp_server, app, test_project):
    """50 notes via 1 bulk_edit_notes vs 50x edit_note: measure and report the gain."""
    async with Client(mcp_server) as client:
        await _create_notes(client, test_project.name, "single")
        await _create_notes(client, test_project.name, "bulk")

        # --- baseline: 50 sequential edit_note calls ---
        start_single = time.perf_counter()
        for i in range(NOTE_COUNT):
            await client.call_tool(
                "edit_note",
                {
                    "project": test_project.name,
                    "identifier": f"bench-single/single-note-{i}",
                    "operation": "find_replace",
                    "content": "bench-new-token",
                    "find_text": "bench-old-token",
                },
            )
        single_duration = time.perf_counter() - start_single

        # --- batch: 1 bulk_edit_notes call ---
        edits = [
            {
                "identifier": f"bench-bulk/bulk-note-{i}",
                "operation": "find_replace",
                "content": "bench-new-token",
                "find_text": "bench-old-token",
            }
            for i in range(NOTE_COUNT)
        ]
        start_bulk = time.perf_counter()
        bulk_result = await client.call_tool(
            "bulk_edit_notes",
            {"edits": edits, "project": test_project.name},
        )
        bulk_duration = time.perf_counter() - start_bulk

        report = bulk_result.content[0].text
        assert f"{NOTE_COUNT}/{NOTE_COUNT}" in report

        speedup = single_duration / bulk_duration if bulk_duration > 0 else float("inf")
        print(
            f"\n[BULK-10] {NOTE_COUNT} notes — "
            f"sequential edit_note: {single_duration:.3f}s, "
            f"bulk_edit_notes: {bulk_duration:.3f}s, "
            f"speedup: {speedup:.1f}x"
        )

        # The batch must beat N sequential MCP round-trips.
        assert bulk_duration < single_duration
