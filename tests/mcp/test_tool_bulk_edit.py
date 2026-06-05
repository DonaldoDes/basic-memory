"""Tests for the bulk_edit_notes MCP tool (params/response mapping)."""

import pytest

from basic_memory.mcp.tools.bulk_edit import bulk_edit_notes
from basic_memory.mcp.tools.read_note import read_note
from basic_memory.mcp.tools.write_note import write_note


@pytest.mark.asyncio
async def test_bulk_edit_notes_success_summary(client, test_project):
    """A successful batch returns a per-item markdown report."""
    await write_note(
        project=test_project.name,
        title="Bulk Tool Note A",
        directory="bulk",
        content="Content A",
    )
    await write_note(
        project=test_project.name,
        title="Bulk Tool Note B",
        directory="bulk",
        content="Content B",
    )

    result = await bulk_edit_notes(
        edits=[
            {
                "identifier": "bulk/bulk-tool-note-a",
                "operation": "append",
                "content": "TOOL-APPEND-A",
            },
            {
                "identifier": "bulk/bulk-tool-note-b",
                "operation": "find_replace",
                "content": "Content B2",
                "find_text": "Content B",
            },
        ],
        project=test_project.name,
    )

    assert isinstance(result, str)
    assert "Bulk edit" in result
    assert "2/2" in result
    assert "success" in result
    assert "bulk/bulk-tool-note-a" in result
    assert "bulk/bulk-tool-note-b" in result

    # The edits were actually applied.
    content_a = await read_note("bulk/bulk-tool-note-a", project=test_project.name)
    assert "TOOL-APPEND-A" in content_a


@pytest.mark.asyncio
async def test_bulk_edit_notes_validate_first_dry_run(client, test_project):
    """validate_first reports validated items without writing anything."""
    await write_note(
        project=test_project.name,
        title="Bulk Tool Dry Note",
        directory="bulk",
        content="Dry content",
    )

    result = await bulk_edit_notes(
        edits=[
            {
                "identifier": "bulk/bulk-tool-dry-note",
                "operation": "append",
                "content": "DRY-MARKER",
            }
        ],
        validate_first=True,
        project=test_project.name,
    )

    assert isinstance(result, str)
    assert "validated" in result

    content = await read_note("bulk/bulk-tool-dry-note", project=test_project.name)
    assert "DRY-MARKER" not in content


@pytest.mark.asyncio
async def test_bulk_edit_notes_reports_item_failures(client, test_project):
    """Failed items surface their error code in the report."""
    await write_note(
        project=test_project.name,
        title="Bulk Tool Mixed Note",
        directory="bulk",
        content="Mixed content",
    )

    result = await bulk_edit_notes(
        edits=[
            {
                "identifier": "bulk/bulk-tool-mixed-note",
                "operation": "append",
                "content": "MIXED-OK",
            },
            {"identifier": "bulk/no-such-note", "operation": "append", "content": "x"},
        ],
        project=test_project.name,
    )

    assert isinstance(result, str)
    assert "1/2" in result
    assert "NOT_FOUND" in result
    assert "bulk/no-such-note" in result


@pytest.mark.asyncio
async def test_bulk_edit_notes_client_side_validation_error(client, test_project):
    """Schema violations are reported as a global validation failure, nothing runs."""
    result = await bulk_edit_notes(edits=[], project=test_project.name)

    assert isinstance(result, str)
    assert "validation" in result.lower()


@pytest.mark.asyncio
async def test_bulk_edit_notes_rejects_memory_identifier(client, test_project):
    """memory:// identifiers violate the single-project contract (I-6)."""
    result = await bulk_edit_notes(
        edits=[
            {
                "identifier": "memory://bulk/some-note",
                "operation": "append",
                "content": "x",
            }
        ],
        project=test_project.name,
    )

    assert isinstance(result, str)
    assert "validation" in result.lower()
    assert "memory://" in result


@pytest.mark.asyncio
async def test_bulk_edit_notes_stop_on_error(client, test_project):
    """stop_on_error surfaces skipped items in the report."""
    await write_note(
        project=test_project.name,
        title="Bulk Tool Stop Note",
        directory="bulk",
        content="Stop content",
    )

    result = await bulk_edit_notes(
        edits=[
            {"identifier": "bulk/no-such-note", "operation": "append", "content": "x"},
            {
                "identifier": "bulk/bulk-tool-stop-note",
                "operation": "append",
                "content": "NEVER-WRITTEN",
            },
        ],
        stop_on_error=True,
        project=test_project.name,
    )

    assert isinstance(result, str)
    assert "skipped" in result

    content = await read_note("bulk/bulk-tool-stop-note", project=test_project.name)
    assert "NEVER-WRITTEN" not in content
