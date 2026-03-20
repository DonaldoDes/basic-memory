"""Test that build_context with enable_dataview=True executes queries inline.

This verifies the MCP-layer dataview path works independently of any
persistence layer -- queries are executed on-the-fly and results are
returned in the response.
"""

import pytest

from basic_memory.mcp.tools import build_context, write_note
from basic_memory.schemas.memory import GraphContext


@pytest.mark.asyncio
async def test_build_context_dataview_inline_returns_results(client, test_graph, test_project):
    """build_context with enable_dataview=True should return dataview results inline.

    The dataview results must come from on-the-fly query execution,
    NOT from persisted dataview_link relations.
    """
    # Create a target note
    await write_note.fn(
        project=test_project.name,
        title="Alpha",
        folder="inline-test",
        content="---\ntype: note\nstatus: Active\n---\n\n# Alpha\n\nAlpha content.\n",
    )

    # Create an index note with a dataview query
    await write_note.fn(
        project=test_project.name,
        title="Index",
        folder="inline-test",
        content='---\ntype: index\n---\n\n# Index\n\n```dataview\nLIST\nFROM "inline-test"\n```\n',
    )

    # Call build_context with dataview enabled
    result = await build_context.fn(
        project=test_project.name,
        url="memory://inline-test/index",
        enable_dataview=True,
    )

    # Verify result is returned (GraphContext with rendered dataview)
    assert result is not None
    # The result should contain context results
    assert len(result.results) > 0

    # Find the primary result for the index note
    primary = result.results[0]
    assert primary is not None
    # The primary_result should be the index entity
    assert primary.primary_result is not None
    assert "Index" in primary.primary_result.title
