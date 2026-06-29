"""Bulk edit notes tool for Basic Memory MCP server.

Implements the MCP side of specs/bulk-edit-notes/spec.md: a thin adapter that
validates the batch against the BulkEditRequest schema, delegates to the
knowledge client (single HTTP round-trip — C-1: no per-item resolve call),
and renders the per-item execution report.
"""

from typing import Optional

import logfire
from fastmcp import Context
from loguru import logger
from pydantic import ValidationError

from basic_memory.mcp.project_context import add_project_metadata, get_project_client
from basic_memory.mcp.server import mcp
from basic_memory.schemas.v2.bulk_edit import BulkEditRequest, BulkEditResponse


def _format_validation_error(error: ValidationError) -> str:
    """Render a schema violation as actionable guidance (nothing was executed)."""
    lines = [
        "# Bulk edit failed - validation error",
        "",
        "The request was rejected by schema validation. NO item was processed.",
        "",
        "## Errors",
    ]
    for issue in error.errors():
        location = ".".join(str(part) for part in issue["loc"]) or "request"
        lines.append(f"- `{location}`: {issue['msg']}")
    lines.extend(
        [
            "",
            "## Constraints",
            "- 1 to 100 edits per batch",
            "- content: max 1 MiB per item, 10 MiB cumulated",
            "- identifiers: project-local titles or permalinks only "
            "(no memory:// URLs, no 'project::note' references)",
            "- find_replace requires find_text; section operations require section",
            "- expected_replacements must be >= 1",
        ]
    )
    return "\n".join(lines)


def _format_bulk_response(response: BulkEditResponse, project_name: str) -> str:
    """Render the batch execution report as markdown (summary + per-item detail)."""
    lines = [
        f"# Bulk edit: {response.succeeded}/{response.total} succeeded",
        f"project: {project_name}",
        f"succeeded: {response.succeeded} | failed: {response.failed} | "
        f"skipped: {response.skipped} | validated: {response.validated}",
        f"vector_sync: {response.vector_sync}",
        "",
        "## Results",
    ]
    for item in response.results:
        if item.status == "success":
            checksum = item.checksum[:8] if item.checksum else "unknown"
            lines.append(f"- [success] {item.identifier} -> {item.permalink} ({checksum})")
        elif item.status == "validated":
            lines.append(f"- [validated] {item.identifier} -> {item.permalink}")
        elif item.status == "skipped":
            lines.append(f"- [skipped] {item.identifier}")
        else:
            lines.append(f"- [failed] {item.identifier} - {item.error_code}: {item.error}")
    return "\n".join(lines)


@mcp.tool(
    title="Bulk Edit Notes",
    description=(
        "Apply up to 100 edit operations (append, prepend, find_replace, "
        "replace_section, insert_before_section, insert_after_section) across "
        "notes of one project in a single call, with per-note success/failure "
        "report and optional dry-run (validate_first)."
    ),
    tags={"notes"},
    annotations={"destructiveHint": False, "openWorldHint": False},
)
async def bulk_edit_notes(
    edits: list[dict],
    validate_first: bool = False,
    stop_on_error: bool = False,
    project: Optional[str] = None,
    project_id: Optional[str] = None,
    context: Context | None = None,
) -> str | dict:
    """Edit up to 100 notes of one project in a single MCP call.

    Each edit item supports the same operations as edit_note:
    {"identifier": "folder/note", "operation": "append", "content": "...",
     "section": "## Header" (section ops), "find_text": "..." (find_replace),
     "expected_replacements": 1}

    Execution is best-effort per note: a failed item does not interrupt the
    batch (unless stop_on_error=true, which marks remaining items skipped).
    Notes are edited sequentially in request order — an edit targeting a note
    already edited in the same batch sees the previous result.

    Args:
        edits: List of edit operations (1 to 100 items, single project).
               Identifiers are project-local titles or permalinks; memory://
               URLs and 'project::note' references are rejected.
        validate_first: When true, performs a PURE dry-run — every item is
               validated (status 'validated' or 'failed') and nothing is
               written, even if all items pass.
        stop_on_error: When true, the first failed item marks every remaining
               item as 'skipped'. Already-succeeded items are NOT rolled back.
        project: Project name to edit in. Optional - server will resolve using
               hierarchy. If unknown, use list_memory_projects().
        project_id: Project external_id (UUID). Takes precedence over project.
        context: Optional FastMCP context for performance caching.

    Returns:
        Markdown report: batch summary (succeeded/failed/skipped/validated,
        vector_sync) plus one line per item in request order.

    Examples:
        # Replace a renamed wikilink across a milestone
        bulk_edit_notes(edits=[
            {"identifier": "stories/us-001", "operation": "find_replace",
             "content": "[[New Name]]", "find_text": "[[Old Name]]"},
            {"identifier": "stories/us-002", "operation": "find_replace",
             "content": "[[New Name]]", "find_text": "[[Old Name]]"},
        ], project="my-project")

        # Dry-run before applying
        bulk_edit_notes(edits=[...], validate_first=True, project="my-project")
    """
    try:
        request = BulkEditRequest(
            # `edits` arrives as list[dict] off the MCP wire (typed Any), so the
            # type-checker can't see dict -> BulkEditOperation as safe here;
            # Pydantic coerces each dict against the BulkEditOperation schema.
            edits=edits,  # type: ignore[arg-type]
            validate_first=validate_first,
            stop_on_error=stop_on_error,
        )
    except ValidationError as error:
        logger.info(f"MCP tool call tool=bulk_edit_notes rejected by schema validation: {error}")
        return _format_validation_error(error)

    with logfire.span(
        "mcp.tool.bulk_edit_notes",
        entrypoint="mcp",
        tool_name="bulk_edit_notes",
        requested_project=project,
        requested_project_id=project_id,
        edit_count=len(request.edits),
        validate_first=validate_first,
        stop_on_error=stop_on_error,
    ):
        async with get_project_client(project, context=context, project_id=project_id) as (
            client,
            active_project,
        ):
            logger.info(
                f"MCP tool call tool=bulk_edit_notes project={active_project.name} "
                f"edit_count={len(request.edits)} validate_first={validate_first} "
                f"stop_on_error={stop_on_error}"
            )

            # Import here to avoid circular import (same pattern as edit_note).
            from basic_memory.mcp.clients import KnowledgeClient

            knowledge_client = KnowledgeClient(client, active_project.external_id)
            response = await knowledge_client.bulk_edit_entities(request.model_dump())

            logger.info(
                f"MCP tool response: tool=bulk_edit_notes project={active_project.name} "
                f"total={response.total} succeeded={response.succeeded} "
                f"failed={response.failed} skipped={response.skipped} "
                f"validated={response.validated} vector_sync={response.vector_sync}"
            )

            report = _format_bulk_response(response, active_project.name)
            return add_project_metadata(report, active_project.name)
