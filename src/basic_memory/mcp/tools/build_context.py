"""Build context tool for Basic Memory MCP server."""

from typing import Annotated, Optional, Literal, cast

import logfire
from loguru import logger
from fastmcp import Context
from pydantic import AliasChoices, Field

from basic_memory.config import ConfigManager
from basic_memory.mcp.project_context import (
    detect_project_from_memory_url_prefix,
    get_project_client,
    resolve_project_and_path,
)
from basic_memory.mcp.server import mcp
from basic_memory.schemas.base import TimeFrame
from basic_memory.schemas.memory import (
    ContextResult,
    EntitySummary,
    GraphContext,
    MemoryUrl,
    ObservationSummary,
    RelationSummary,
)
from basic_memory.bases.detector import BasesDetector
from basic_memory.bases.integration import create_bases_integration


def _format_entity_block(result: ContextResult) -> str:
    """Format a single context result as a markdown block."""
    primary = result.primary_result
    lines = []

    # --- Header ---
    lines.append(f"## {primary.title}")
    if primary.permalink:
        lines.append(f"permalink: {primary.permalink}")
    # RelationSummary has no content field; Entity/Observation do
    if not isinstance(primary, RelationSummary) and primary.content:
        lines.append("")
        lines.append(primary.content)

    # --- Observations ---
    if result.observations:
        lines.append("")
        lines.append("### Observations")
        for obs in result.observations:
            lines.append(f"- [{obs.category}] {obs.content}")

    # --- Relations (from primary's related_results that are RelationSummary) ---
    relation_items: list[RelationSummary] = [
        r for r in result.related_results if isinstance(r, RelationSummary)
    ]
    if relation_items:
        lines.append("")
        lines.append("### Relations")
        for rel in relation_items:
            # Unresolved forward references have no resolved entity yet; fall back
            # to the literal target text instead of rendering [[None]] (#955)
            target = rel.to_entity or rel.to_name
            lines.append(f"- {rel.relation_type} [[{target}]]")

    # --- Related entities (non-relation related results) ---
    related_entities: list[EntitySummary | ObservationSummary] = [
        r for r in result.related_results if not isinstance(r, RelationSummary)
    ]
    if related_entities:
        lines.append("")
        lines.append("### Related")
        for item in related_entities:
            permalink = item.permalink if item.permalink else ""
            lines.append(f"- [[{item.title}]] ({permalink})")
            # Short digest (US-006b) — helps decide whether to follow the link
            # without a read_note. Only EntitySummary carries it; render when set.
            summary = getattr(item, "summary", None)
            if summary:
                lines.append(f"  summary: {summary}")

    return "\n".join(lines)


def _format_context_markdown(graph: GraphContext, project: str) -> str:
    """Format GraphContext as compact markdown text.

    Produces a human-readable markdown representation that is much smaller
    than the equivalent JSON, suitable for LLM consumption when structured
    data isn't needed.
    """
    if not graph.results:
        uri = graph.metadata.uri or ""
        return f"No results found for '{uri}' in project '{project}'."

    parts = []

    # --- Title from first primary result ---
    first_title = graph.results[0].primary_result.title
    if len(graph.results) == 1:
        parts.append(f"# Context: {first_title}")
    else:
        uri = graph.metadata.uri or ""
        parts.append(f"# Context: {uri}")

    parts.append("")

    # --- Entity blocks separated by --- ---
    entity_blocks = [_format_entity_block(result) for result in graph.results]
    parts.append("\n\n---\n\n".join(entity_blocks))

    # --- Footer ---
    meta = graph.metadata
    primary_count = meta.primary_count or 0
    related_count = meta.related_count or 0
    parts.append("")
    parts.append("---")
    parts.append(
        f"*{primary_count} primary, {related_count} related"
        f" | depth={meta.depth} | project: {project}*"
    )

    return "\n".join(parts)


@mcp.tool(
    title="Build Context",
    description="""Build context from a memory:// URI to continue conversations naturally.

    Use this to follow up on previous discussions or explore related topics.

    Memory URL Format:
    - Use paths like "folder/note" or "memory://folder/note"
    - Pattern matching: "folder/*" matches all notes in folder
    - Valid characters: letters, numbers, hyphens, underscores, forward slashes
    - Avoid: double slashes (//), angle brackets (<>), quotes, pipes (|)
    - Examples: "specs/search", "projects/basic-memory", "notes/*"

    Timeframes support natural language like:
    - "2 days ago", "last week", "today", "3 months ago"
    - Or standard formats like "7d", "24h"

    Format options:
    - "json" (default): Structured JSON with internal fields excluded
    - "text": Compact markdown text for LLM consumption
    """,
    tags={"navigation", "notes"},
    annotations={"readOnlyHint": True, "openWorldHint": False},
)
async def build_context(
    url: Annotated[
        MemoryUrl,
        Field(validation_alias=AliasChoices("url", "uri", "memory_url")),
    ],
    project: Optional[str] = None,
    project_id: Optional[str] = None,
    depth: str | int | None = 1,
    timeframe: Annotated[
        Optional[TimeFrame],
        Field(
            default="7d",
            validation_alias=AliasChoices("timeframe", "since", "time_range", "lookback"),
        ),
    ] = "7d",
    # `offset` is intentionally NOT aliased: it has different semantics
    # (item-indexed vs. 1-indexed page-number).
    page: Annotated[
        int,
        Field(default=1, validation_alias=AliasChoices("page", "page_number")),
    ] = 1,
    page_size: Annotated[
        int,
        Field(default=10, validation_alias=AliasChoices("page_size", "limit", "per_page")),
    ] = 10,
    max_related: Annotated[
        int,
        Field(default=10, validation_alias=AliasChoices("max_related", "max_results")),
    ] = 10,
    output_format: Literal["json", "text"] = "json",
    enable_dataview: bool = True,
    enable_bases: bool = True,
    context: Context | None = None,
) -> dict | str:
    """Get context needed to continue a discussion within a specific project.

    This tool enables natural continuation of discussions by loading relevant context
    from memory:// URIs. It uses pattern matching to find relevant content and builds
    a rich context graph of related information.

    Project Resolution:
    Server resolves projects using a unified priority chain (same in local and cloud modes):
    Single Project Mode → project parameter → default project.
    Uses default project automatically. Specify `project` parameter to target a different project.

    Args:
        project: Project name to build context from. Optional - server will resolve using hierarchy.
                If unknown, use list_memory_projects() to discover available projects.
        project_id: Project external_id (UUID). Prefer this over `project` when known —
                it routes to the exact project regardless of name collisions across cloud
                workspaces. Takes precedence over `project`. Get from list_memory_projects().
        url: memory:// URI pointing to discussion content (e.g. memory://specs/search)
        depth: How many relation hops to traverse (1-3 recommended for performance)
        timeframe: How far back to look. Supports natural language like "2 days ago", "last week"
        page: Page number of results to return (default: 1)
        page_size: Number of results to return per page (default: 10)
        max_related: Maximum number of related results to return (default: 10)
        output_format: Response format - "json" for structured JSON dict,
            "text" for compact markdown text
        enable_dataview: Execute Dataview queries in context notes (default: True)
        enable_bases: Execute Obsidian Bases (```base```) queries in context notes (default: True)
        context: Optional FastMCP context for performance caching.

    Returns:
        dict (output_format="json"): Structured JSON with internal fields excluded
        str (output_format="text"): Compact markdown representation

    Examples:
        # Continue a specific discussion
        build_context("my-project", "memory://specs/search")

        # Get deeper context about a component
        build_context("work-docs", "memory://components/memory-service", depth=2)

        # Get text output for compact context
        build_context("research", "memory://specs/search", output_format="text")

    Raises:
        ToolError: If project doesn't exist or depth parameter is invalid
    """
    # Validate pagination arguments before they reach the context service.
    # Trigger: page < 1 or page_size < 1 (e.g. page_size=0 or negative).
    # Why: a non-positive page_size flows into context_service as limit, where the
    #      primary slice does primary = primary[:limit] — so limit=0 truncates the
    #      requested entity to [] and the caller's valid memory:// lookup silently
    #      returns primary_count=0. Mirrors recent_activity's guard for consistency.
    # Outcome: caller gets an explicit ValueError instead of a dropped primary result.
    if page < 1:
        raise ValueError(f"page must be >= 1, got {page}")
    if page_size < 1:
        raise ValueError(f"page_size must be >= 1, got {page_size}")

    # Detect project from memory URL prefix before routing.
    # project_id routes by external UUID, so it bypasses URL discovery entirely.
    if project is None and project_id is None:
        detected = await detect_project_from_memory_url_prefix(
            url,
            ConfigManager().config,
            context=context,
        )
        if detected:
            project = detected

    # Convert string depth to integer if needed
    if isinstance(depth, str):
        try:
            depth = int(depth)
        except ValueError:
            from mcp.server.fastmcp.exceptions import ToolError

            raise ToolError(f"Invalid depth parameter: '{depth}' is not a valid integer")

    # URL is already validated and normalized by MemoryUrl type annotation

    with logfire.span(
        "mcp.tool.build_context",
        entrypoint="mcp",
        tool_name="build_context",
        requested_project=project,
        requested_project_id=project_id,
        depth=depth or 1,
        timeframe=timeframe,
        page=page,
        page_size=page_size,
        max_related=max_related,
        output_format=output_format,
        is_memory_url=str(url).startswith("memory://"),
    ):
        async with get_project_client(project, context=context, project_id=project_id) as (
            client,
            active_project,
        ):
            logger.info(
                f"MCP tool call tool=build_context project={active_project.name} "
                f"url={url} depth={depth} timeframe={timeframe} output_format={output_format}"
            )

            # Resolve memory:// identifier with project-prefix awareness
            _, resolved_path, _ = await resolve_project_and_path(
                client,
                url,
                active_project.name,
                context,
            )

            # Import here to avoid circular import
            from basic_memory.mcp.clients import MemoryClient
            from basic_memory.mcp.clients.knowledge import KnowledgeClient

            # Use typed MemoryClient for API calls
            memory_client = MemoryClient(client, active_project.external_id)
            graph = await memory_client.build_context(
                resolved_path,
                depth=depth or 1,
                timeframe=timeframe,
                page=page,
                page_size=page_size,
                max_related=max_related,
            )

            # ``enable_dataview`` is still accepted on the signature for backward
            # compatibility but is a no-op (the Dataview executor was removed).
            # ``​```dataview​`` blocks are left inert; use ``enable_bases`` below
            # for live query rendering.

            # Enrich with Bases (```base```) via on-the-fly detection on the
            # primary content. Bases detects blocks directly on the returned
            # content — no dependency on sync_service (ADR-003 §4).
            if enable_bases:
                has_base = any(
                    cr.primary_result.type == "entity"
                    and cr.primary_result.content
                    and BasesDetector.has_base_blocks(cast(str, cr.primary_result.content))
                    for cr in graph.results
                )
                if has_base:
                    logger.info("Enriching graph context with on-the-fly Bases queries")
                    knowledge_client = KnowledgeClient(client, active_project.external_id)
                    notes = await knowledge_client.list_entities_for_bases()
                    bases_integration = create_bases_integration(notes_provider=lambda: notes)
                    for cr in graph.results:
                        primary = cr.primary_result
                        if (
                            primary.type == "entity"
                            and primary.content
                            and BasesDetector.has_base_blocks(cast(str, primary.content))
                        ):
                            try:
                                # US-b (ADR-005 §Axe 2): thread the host-note
                                # identity (the primary result IS the host of its
                                # own base blocks) so this / file.hasLink(this.file)
                                # resolve. Identity = path/permalink/title only.
                                host_metadata = {
                                    "permalink": getattr(primary, "permalink", None),
                                    "path": getattr(primary, "file_path", None),
                                    "title": getattr(primary, "title", None),
                                }
                                results = bases_integration.process_note(
                                    cast(str, primary.content),
                                    note_metadata=host_metadata,
                                )
                                section = "\n\n---\n## Bases Query Results\n\n"
                                appended = section
                                for r in results:
                                    if r["status"] == "success" and r.get("result_markdown"):
                                        appended += r["result_markdown"] + "\n\n"
                                if len(appended) > len(section):
                                    primary.content = cast(str, primary.content) + appended
                            except Exception as e:  # pragma: no cover
                                logger.warning(f"Failed to execute Bases queries: {e}")

            logger.info(
                f"MCP tool response: tool=build_context project={active_project.name} "
                f"uri={graph.metadata.uri or resolved_path} "
                f"primary_count={graph.metadata.primary_count or 0} "
                f"related_count={graph.metadata.related_count or 0} "
                f"output_format={output_format}"
            )

            if output_format == "text":
                return _format_context_markdown(graph, active_project.name)

            return graph.model_dump()
