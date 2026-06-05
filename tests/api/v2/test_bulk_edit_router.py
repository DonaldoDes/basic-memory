"""Tests for the V2 bulk edit endpoint.

POST /v2/projects/{project_id}/knowledge/entities/bulk-edit

Covers spec test IDs (specs/bulk-edit-notes/spec.md):
- BULK-02: validate_first is a pure dry-run (no disk/DB/index writes)
- BULK-03: best-effort — a failing item does not interrupt the batch
- BULK-04: stop_on_error marks remaining items skipped, no rollback
- BULK-05: sequential edits on the same note see each other's result
- BULK-08: no auto-creation — unresolved identifier fails NOT_FOUND (I-4)
- BULK-11: path traversal identifiers fail with SECURITY, batch continues (I-1)
- BULK-13 (API part): replacement count mismatch fails the item
- BULK-14: ambiguous identifier fails the item, batch continues

Plus: global 422 leaves all items untouched (I-2/I-3/I-5/I-6),
vector sync batched exactly once (scheduled/disabled).
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest
from httpx import AsyncClient

from basic_memory.api.v2.routers.knowledge_router import _map_bulk_edit_error
from basic_memory.models.knowledge import Entity as EntityModel
from basic_memory.schemas.v2 import BulkEditResponse, EntityResponseV2
from basic_memory.schemas.v2.bulk_edit import MAX_CONTENT_BYTES
from basic_memory.services.exceptions import EntityNotFoundError


async def create_note(
    client: AsyncClient,
    v2_project_url: str,
    title: str,
    directory: str = "bulk",
    content: str = "Original content",
) -> EntityResponseV2:
    """Create a note through the V2 API and return its response model."""
    response = await client.post(
        f"{v2_project_url}/knowledge/entities",
        json={"title": title, "directory": directory, "content": content},
    )
    assert response.status_code == 200
    return EntityResponseV2.model_validate(response.json())


async def bulk_edit(
    client: AsyncClient,
    v2_project_url: str,
    edits: list[dict],
    **options,
):
    """POST a bulk edit request and return the raw response."""
    payload = {"edits": edits, **options}
    return await client.post(f"{v2_project_url}/knowledge/entities/bulk-edit", json=payload)


async def read_note_file(file_service, entity: EntityResponseV2) -> str:
    content, _ = await file_service.read_file(Path(entity.file_path))
    return content


# --- nominal batch behavior ---


@pytest.mark.asyncio
async def test_bulk_edit_append_two_notes(client, file_service, v2_project_url):
    note_a = await create_note(client, v2_project_url, "BulkNoteA")
    note_b = await create_note(client, v2_project_url, "BulkNoteB")

    response = await bulk_edit(
        client,
        v2_project_url,
        [
            {"identifier": note_a.permalink, "operation": "append", "content": "APPENDED-A"},
            {"identifier": note_b.permalink, "operation": "append", "content": "APPENDED-B"},
        ],
    )

    assert response.status_code == 200
    result = BulkEditResponse.model_validate(response.json())
    assert result.total == 2
    assert result.succeeded == 2
    assert result.failed == 0
    assert result.skipped == 0
    assert result.validated == 0
    assert [item.status for item in result.results] == ["success", "success"]
    assert all(item.checksum for item in result.results)
    assert all(item.permalink for item in result.results)
    assert all(item.file_path for item in result.results)

    assert "APPENDED-A" in await read_note_file(file_service, note_a)
    assert "APPENDED-B" in await read_note_file(file_service, note_b)


@pytest.mark.asyncio
async def test_bulk_edit_results_preserve_request_order(client, v2_project_url):
    note_a = await create_note(client, v2_project_url, "OrderNoteA")
    note_b = await create_note(client, v2_project_url, "OrderNoteB")

    response = await bulk_edit(
        client,
        v2_project_url,
        [
            {"identifier": note_b.permalink, "operation": "append", "content": "B-first"},
            {"identifier": "bulk/does-not-exist", "operation": "append", "content": "x"},
            {"identifier": note_a.permalink, "operation": "append", "content": "A-last"},
        ],
    )

    assert response.status_code == 200
    result = BulkEditResponse.model_validate(response.json())
    assert [item.identifier for item in result.results] == [
        note_b.permalink,
        "bulk/does-not-exist",
        note_a.permalink,
    ]


# --- identifier resolution paths ---


@pytest.mark.asyncio
async def test_bulk_edit_resolves_identifier_by_exact_title(client, file_service, v2_project_url):
    note = await create_note(client, v2_project_url, "Unique Bulk Title")

    response = await bulk_edit(
        client,
        v2_project_url,
        [{"identifier": "Unique Bulk Title", "operation": "append", "content": "BY-TITLE"}],
    )

    assert response.status_code == 200
    result = BulkEditResponse.model_validate(response.json())
    assert result.succeeded == 1
    assert "BY-TITLE" in await read_note_file(file_service, note)


@pytest.mark.asyncio
async def test_bulk_edit_resolves_identifier_by_file_path(
    client, file_service, v2_project_url, test_project, entity_repository
):
    """Custom-permalink entities resolve through the file path fallback."""
    file_path = "bulk/Path Resolved Note.md"
    await file_service.write_file(Path(file_path), "# Path Resolved Note\n\nbody text\n")
    now = datetime.now(timezone.utc)
    await entity_repository.add(
        EntityModel(
            title="Path Resolved Note",
            note_type="note",
            content_type="text/markdown",
            file_path=file_path,
            permalink="totally/custom-permalink",
            created_at=now,
            updated_at=now,
            project_id=entity_repository.project_id,
        )
    )

    # Dry-run exercises resolution without engaging the write pipeline:
    # exact file path, then file path with .md appended.
    response = await bulk_edit(
        client,
        v2_project_url,
        [
            {"identifier": file_path, "operation": "append", "content": "x"},
            {"identifier": "bulk/Path Resolved Note", "operation": "append", "content": "x"},
        ],
        validate_first=True,
    )

    assert response.status_code == 200
    result = BulkEditResponse.model_validate(response.json())
    assert [item.status for item in result.results] == ["validated", "validated"]


def test_map_bulk_edit_error_codes():
    """Error mapping covers every spec error code plus the generic fallback."""
    assert _map_bulk_edit_error(EntityNotFoundError("Entity not found: x"))[0] == "NOT_FOUND"
    assert _map_bulk_edit_error(ValueError("Text to replace not found: 'y'"))[0] == "TEXT_NOT_FOUND"
    assert (
        _map_bulk_edit_error(ValueError("Expected 2 occurrences of 'y', but found 1"))[0]
        == "REPLACEMENT_COUNT_MISMATCH"
    )
    assert (
        _map_bulk_edit_error(ValueError("Multiple sections found with header '## A'."))[0]
        == "DUPLICATE_SECTION"
    )
    code, message = _map_bulk_edit_error(RuntimeError("unexpected failure"))
    assert code == "EDIT_ERROR"
    assert message == "unexpected failure"


# --- BULK-02: validate_first pure dry-run ---


@pytest.mark.asyncio
async def test_validate_first_is_pure_dry_run(
    client, file_service, v2_project_url, task_scheduler_spy, app_config
):
    app_config.semantic_search_enabled = True
    note_a = await create_note(client, v2_project_url, "DryRunNoteA", content="Original A")
    note_b = await create_note(client, v2_project_url, "DryRunNoteB", content="Original B")
    scheduler_count_before = len(task_scheduler_spy)

    response = await bulk_edit(
        client,
        v2_project_url,
        [
            {"identifier": note_a.permalink, "operation": "append", "content": "DRY-RUN-A"},
            {
                "identifier": note_b.permalink,
                "operation": "find_replace",
                "content": "Modified B",
                "find_text": "Original B",
            },
        ],
        validate_first=True,
    )

    assert response.status_code == 200
    result = BulkEditResponse.model_validate(response.json())
    assert result.validated == 2
    assert result.succeeded == 0
    assert [item.status for item in result.results] == ["validated", "validated"]

    # No writes: files unchanged on disk, even though every item validated.
    assert "DRY-RUN-A" not in await read_note_file(file_service, note_a)
    content_b = await read_note_file(file_service, note_b)
    assert "Original B" in content_b
    assert "Modified B" not in content_b

    # No vector sync scheduled.
    assert len(task_scheduler_spy) == scheduler_count_before
    assert result.vector_sync == "disabled"


@pytest.mark.asyncio
async def test_validate_first_reports_predictable_failures(client, file_service, v2_project_url):
    note = await create_note(client, v2_project_url, "DryRunFailNote", content="Some text")

    response = await bulk_edit(
        client,
        v2_project_url,
        [
            {
                "identifier": note.permalink,
                "operation": "find_replace",
                "content": "new",
                "find_text": "text-that-does-not-exist",
            },
            {"identifier": "bulk/missing-note", "operation": "append", "content": "x"},
        ],
        validate_first=True,
    )

    assert response.status_code == 200
    result = BulkEditResponse.model_validate(response.json())
    assert result.validated == 0
    assert result.failed == 2
    assert result.results[0].status == "failed"
    assert result.results[0].error_code == "TEXT_NOT_FOUND"
    assert result.results[1].status == "failed"
    assert result.results[1].error_code == "NOT_FOUND"


# --- BULK-03: best-effort semantics ---


@pytest.mark.asyncio
async def test_best_effort_continues_after_failed_item(client, file_service, v2_project_url):
    note_a = await create_note(client, v2_project_url, "EffortNoteA")
    note_c = await create_note(client, v2_project_url, "EffortNoteC")

    response = await bulk_edit(
        client,
        v2_project_url,
        [
            {"identifier": note_a.permalink, "operation": "append", "content": "EFFORT-A"},
            {"identifier": "bulk/missing-note", "operation": "append", "content": "x"},
            {"identifier": note_c.permalink, "operation": "append", "content": "EFFORT-C"},
        ],
    )

    assert response.status_code == 200
    result = BulkEditResponse.model_validate(response.json())
    assert result.succeeded == 2
    assert result.failed == 1
    assert result.skipped == 0
    assert [item.status for item in result.results] == ["success", "failed", "success"]
    assert result.results[1].error_code == "NOT_FOUND"
    assert result.results[1].error

    # Items after the failure were actually written.
    assert "EFFORT-C" in await read_note_file(file_service, note_c)


# --- BULK-04: stop_on_error ---


@pytest.mark.asyncio
async def test_stop_on_error_skips_remaining_items(client, file_service, v2_project_url):
    note_a = await create_note(client, v2_project_url, "StopNoteA")
    note_c = await create_note(client, v2_project_url, "StopNoteC")

    response = await bulk_edit(
        client,
        v2_project_url,
        [
            {"identifier": note_a.permalink, "operation": "append", "content": "STOP-A"},
            {"identifier": "bulk/missing-note", "operation": "append", "content": "x"},
            {"identifier": note_c.permalink, "operation": "append", "content": "STOP-C"},
        ],
        stop_on_error=True,
    )

    assert response.status_code == 200
    result = BulkEditResponse.model_validate(response.json())
    assert result.succeeded == 1
    assert result.failed == 1
    assert result.skipped == 1
    assert [item.status for item in result.results] == ["success", "failed", "skipped"]

    # Already-succeeded items are NOT rolled back.
    assert "STOP-A" in await read_note_file(file_service, note_a)
    # Skipped items were never evaluated nor written.
    assert "STOP-C" not in await read_note_file(file_service, note_c)


# --- BULK-05: sequential edits on the same note ---


@pytest.mark.asyncio
async def test_sequential_edits_same_note_second_sees_first(client, file_service, v2_project_url):
    note = await create_note(client, v2_project_url, "SequentialNote")

    response = await bulk_edit(
        client,
        v2_project_url,
        [
            {"identifier": note.permalink, "operation": "append", "content": "MARKER-ONE"},
            {
                "identifier": note.permalink,
                "operation": "find_replace",
                "content": "MARKER-TWO",
                "find_text": "MARKER-ONE",
            },
        ],
    )

    assert response.status_code == 200
    result = BulkEditResponse.model_validate(response.json())
    assert result.succeeded == 2

    content = await read_note_file(file_service, note)
    assert "MARKER-TWO" in content
    assert "MARKER-ONE" not in content


@pytest.mark.asyncio
async def test_sequential_edits_same_note_dry_run_projected_content(
    client, file_service, v2_project_url
):
    """In validate_first mode the projected content map preserves edit ordering."""
    note = await create_note(client, v2_project_url, "SequentialDryNote")

    response = await bulk_edit(
        client,
        v2_project_url,
        [
            {"identifier": note.permalink, "operation": "append", "content": "PROJECTED-ONE"},
            {
                "identifier": note.permalink,
                "operation": "find_replace",
                "content": "PROJECTED-TWO",
                "find_text": "PROJECTED-ONE",
            },
        ],
        validate_first=True,
    )

    assert response.status_code == 200
    result = BulkEditResponse.model_validate(response.json())
    # The 2nd edit only validates if it saw the projected result of the 1st.
    assert result.validated == 2
    assert [item.status for item in result.results] == ["validated", "validated"]

    # Still a pure dry-run: nothing on disk.
    content = await read_note_file(file_service, note)
    assert "PROJECTED-ONE" not in content
    assert "PROJECTED-TWO" not in content


# --- BULK-08: no auto-creation (I-4) ---


@pytest.mark.asyncio
async def test_no_auto_creation_for_unresolved_identifier(
    client, v2_project_url, test_project, entity_repository
):
    response = await bulk_edit(
        client,
        v2_project_url,
        [{"identifier": "bulk/ghost-note", "operation": "append", "content": "should not exist"}],
    )

    assert response.status_code == 200
    result = BulkEditResponse.model_validate(response.json())
    assert result.failed == 1
    assert result.results[0].status == "failed"
    assert result.results[0].error_code == "NOT_FOUND"

    # No file created on disk.
    assert not (Path(test_project.path) / "bulk" / "ghost-note.md").exists()

    # No entity created in the database.
    resolve = await client.post(
        f"{v2_project_url}/knowledge/resolve",
        json={"identifier": "bulk/ghost-note", "strict": True},
    )
    assert resolve.status_code == 404


# --- BULK-11: path traversal (I-1) ---


@pytest.mark.asyncio
async def test_path_traversal_identifiers_fail_security_without_io(
    client, file_service, v2_project_url, test_project
):
    note = await create_note(client, v2_project_url, "TraversalSafeNote")

    # Plant a file OUTSIDE the project root that a traversal identifier targets.
    outside_file = Path(test_project.path).parent / "outside-target.md"
    outside_file.write_text("OUTSIDE ORIGINAL", encoding="utf-8")

    response = await bulk_edit(
        client,
        v2_project_url,
        [
            {"identifier": "../outside-target", "operation": "append", "content": "evil"},
            {"identifier": "/etc/passwd", "operation": "append", "content": "evil"},
            {"identifier": "..%2F..%2Fsecrets", "operation": "append", "content": "evil"},
            {"identifier": "notes/../../escape", "operation": "append", "content": "evil"},
            {"identifier": note.permalink, "operation": "append", "content": "LEGIT-EDIT"},
        ],
    )

    assert response.status_code == 200
    result = BulkEditResponse.model_validate(response.json())
    assert [item.status for item in result.results] == [
        "failed",
        "failed",
        "failed",
        "failed",
        "success",
    ]
    assert all(item.error_code == "SECURITY" for item in result.results[:4])
    assert result.failed == 4
    assert result.succeeded == 1

    # The batch was not interrupted and no file outside the project was touched.
    assert outside_file.read_text(encoding="utf-8") == "OUTSIDE ORIGINAL"
    assert "LEGIT-EDIT" in await read_note_file(file_service, note)


# --- BULK-13 (API part): replacement count mismatch ---


@pytest.mark.asyncio
async def test_replacement_count_mismatch_fails_item(client, v2_project_url):
    note = await create_note(
        client, v2_project_url, "CountNote", content="token appears once: token-x"
    )

    response = await bulk_edit(
        client,
        v2_project_url,
        [
            {
                "identifier": note.permalink,
                "operation": "find_replace",
                "content": "replaced",
                "find_text": "token-x",
                "expected_replacements": 2,
            }
        ],
    )

    assert response.status_code == 200
    result = BulkEditResponse.model_validate(response.json())
    assert result.results[0].status == "failed"
    assert result.results[0].error_code == "REPLACEMENT_COUNT_MISMATCH"


@pytest.mark.asyncio
async def test_find_text_not_found_fails_item(client, v2_project_url):
    note = await create_note(client, v2_project_url, "MissingTextNote", content="Some content")

    response = await bulk_edit(
        client,
        v2_project_url,
        [
            {
                "identifier": note.permalink,
                "operation": "find_replace",
                "content": "replaced",
                "find_text": "absent-text",
            }
        ],
    )

    assert response.status_code == 200
    result = BulkEditResponse.model_validate(response.json())
    assert result.results[0].status == "failed"
    assert result.results[0].error_code == "TEXT_NOT_FOUND"


@pytest.mark.asyncio
async def test_duplicate_section_fails_item(client, v2_project_url):
    note = await create_note(
        client,
        v2_project_url,
        "DuplicateSectionNote",
        content="## Same\n\nfirst\n\n## Same\n\nsecond",
    )

    response = await bulk_edit(
        client,
        v2_project_url,
        [
            {
                "identifier": note.permalink,
                "operation": "replace_section",
                "content": "new content",
                "section": "## Same",
            }
        ],
    )

    assert response.status_code == 200
    result = BulkEditResponse.model_validate(response.json())
    assert result.results[0].status == "failed"
    assert result.results[0].error_code == "DUPLICATE_SECTION"


# --- BULK-14: ambiguous identifier ---


@pytest.mark.asyncio
async def test_ambiguous_identifier_fails_item_batch_continues(
    client, file_service, v2_project_url
):
    await create_note(client, v2_project_url, "Duplicate Title", directory="bulk1")
    await create_note(client, v2_project_url, "Duplicate Title", directory="bulk2")
    other = await create_note(client, v2_project_url, "UnambiguousNote")

    response = await bulk_edit(
        client,
        v2_project_url,
        [
            {"identifier": "Duplicate Title", "operation": "append", "content": "x"},
            {"identifier": other.permalink, "operation": "append", "content": "AFTER-AMBIGUOUS"},
        ],
    )

    assert response.status_code == 200
    result = BulkEditResponse.model_validate(response.json())
    assert result.results[0].status == "failed"
    assert result.results[0].error_code == "AMBIGUOUS_IDENTIFIER"
    assert result.results[1].status == "success"
    assert "AFTER-AMBIGUOUS" in await read_note_file(file_service, other)


# --- global 422: no item processed (I-2/I-3/I-5/I-6) ---


@pytest.mark.asyncio
async def test_global_422_processes_no_items(client, file_service, v2_project_url):
    note = await create_note(client, v2_project_url, "UntouchedNote")

    response = await bulk_edit(
        client,
        v2_project_url,
        [
            {"identifier": note.permalink, "operation": "append", "content": "SHOULD-NOT-APPEAR"},
            {
                "identifier": "bulk/whatever",
                "operation": "append",
                "content": "x" * (MAX_CONTENT_BYTES + 1),
            },
        ],
    )

    assert response.status_code == 422
    # The valid first item was NOT applied.
    assert "SHOULD-NOT-APPEAR" not in await read_note_file(file_service, note)


@pytest.mark.asyncio
async def test_memory_identifier_rejected_with_422(client, v2_project_url):
    response = await bulk_edit(
        client,
        v2_project_url,
        [{"identifier": "memory://bulk/some-note", "operation": "append", "content": "x"}],
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_empty_edits_rejected_with_422(client, v2_project_url):
    response = await bulk_edit(client, v2_project_url, [])
    assert response.status_code == 422


# --- vector sync batched (scheduled / disabled) ---


@pytest.mark.asyncio
async def test_vector_sync_scheduled_once_for_batch(
    client, v2_project_url, task_scheduler_spy, app_config
):
    app_config.semantic_search_enabled = True
    note_a = await create_note(client, v2_project_url, "VectorNoteA")
    note_b = await create_note(client, v2_project_url, "VectorNoteB")
    start_count = len(task_scheduler_spy)

    response = await bulk_edit(
        client,
        v2_project_url,
        [
            {"identifier": note_a.permalink, "operation": "append", "content": "vec-a"},
            {"identifier": note_b.permalink, "operation": "append", "content": "vec-b"},
            # Same note edited twice: entity id must not be duplicated in the payload.
            {"identifier": note_a.permalink, "operation": "append", "content": "vec-a2"},
        ],
    )

    assert response.status_code == 200
    result = BulkEditResponse.model_validate(response.json())
    assert result.vector_sync == "scheduled"

    # Exactly ONE batch sync task for the whole batch (not one per note).
    new_tasks = task_scheduler_spy[start_count:]
    assert len(new_tasks) == 1
    assert new_tasks[0]["task_name"] == "sync_entity_vectors_batch"
    assert sorted(new_tasks[0]["payload"]["entity_ids"]) == sorted([note_a.id, note_b.id])


@pytest.mark.asyncio
async def test_vector_sync_disabled_when_semantic_off(
    client, v2_project_url, task_scheduler_spy, app_config
):
    app_config.semantic_search_enabled = False
    note = await create_note(client, v2_project_url, "NoVectorNote")
    start_count = len(task_scheduler_spy)

    response = await bulk_edit(
        client,
        v2_project_url,
        [{"identifier": note.permalink, "operation": "append", "content": "x"}],
    )

    assert response.status_code == 200
    result = BulkEditResponse.model_validate(response.json())
    assert result.vector_sync == "disabled"
    assert len(task_scheduler_spy) == start_count


@pytest.mark.asyncio
async def test_vector_sync_disabled_when_nothing_modified(
    client, v2_project_url, task_scheduler_spy, app_config
):
    app_config.semantic_search_enabled = True
    start_count = len(task_scheduler_spy)

    response = await bulk_edit(
        client,
        v2_project_url,
        [{"identifier": "bulk/missing", "operation": "append", "content": "x"}],
    )

    assert response.status_code == 200
    result = BulkEditResponse.model_validate(response.json())
    assert result.vector_sync == "disabled"
    assert len(task_scheduler_spy) == start_count
