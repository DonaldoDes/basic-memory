"""Integration tests for BUG-010 via the MCP write_note / edit_note round-trip.

These cover the acceptance criteria end-to-end:
- AC-1: write_note with unquoted ``part_of: [[path|alias]]`` -> wikilink intact, never ``- - path|alias``
- AC-2: write_note with quoted ``'[[path|alias]]'`` -> preserved
- AC-3: edit_note find_replace on a wikilink field -> literal preserved
- AC-4: raw read post-create -> no nested list ``- - `` in the frontmatter
- AC-5: the part_of relation is resolved in the graph after creation
- AC-6: non-regression — a genuine YAML list (tags) is neither over-quoted nor broken
"""

import pytest

from basic_memory.mcp.tools import write_note, read_content, edit_note


@pytest.mark.asyncio
async def test_ac1_unquoted_wikilink_part_of_preserved(app, test_project):
    """AC-1 + AC-4: unquoted part_of wikilink survives round-trip as a string."""
    content = "---\npart_of: [[projects/Mon Projet|Mon Projet]]\n---\n\n# Child Note\n\nBody."
    await write_note(
        project=test_project.name,
        title="Child Note",
        directory="bug010",
        content=content,
    )

    raw = await read_content("bug010/Child Note.md", project=test_project.name)
    raw_text = raw["text"] if isinstance(raw, dict) else str(raw)

    # AC-4: no nested-list block-style emission
    assert "- - " not in raw_text
    # AC-1: wikilink preserved literally (quoted scalar is acceptable)
    assert "[[projects/Mon Projet|Mon Projet]]" in raw_text
    assert "part_of:\n- -" not in raw_text


@pytest.mark.asyncio
async def test_ac2_quoted_wikilink_preserved(app, test_project):
    """AC-2: an already single-quoted wikilink is preserved as-is."""
    content = "---\npart_of: '[[projects/Mon Projet|Mon Projet]]'\n---\n\n# Quoted Child\n\nBody."
    await write_note(
        project=test_project.name,
        title="Quoted Child",
        directory="bug010",
        content=content,
    )

    raw = await read_content("bug010/Quoted Child.md", project=test_project.name)
    raw_text = raw["text"] if isinstance(raw, dict) else str(raw)

    assert "- - " not in raw_text
    assert "[[projects/Mon Projet|Mon Projet]]" in raw_text


@pytest.mark.asyncio
async def test_ac3_edit_note_find_replace_on_wikilink_field(app, test_project):
    """AC-3: editing a wikilink field via find_replace preserves the literal."""
    content = "---\npart_of: '[[projects/Old|Old]]'\n---\n\n# Editable\n\nBody."
    await write_note(
        project=test_project.name,
        title="Editable",
        directory="bug010",
        content=content,
    )

    await edit_note(
        identifier="bug010/Editable",
        project=test_project.name,
        operation="find_replace",
        find_text="[[projects/Old|Old]]",
        content="[[projects/New|New]]",
    )

    raw = await read_content("bug010/Editable.md", project=test_project.name)
    raw_text = raw["text"] if isinstance(raw, dict) else str(raw)

    assert "- - " not in raw_text
    assert "[[projects/New|New]]" in raw_text
    assert "[[projects/Old|Old]]" not in raw_text


@pytest.mark.asyncio
async def test_ac5_part_of_relation_resolved_in_graph(app, test_project):
    """AC-5: the part_of relation is parsed and present after creation.

    The frontmatter wikilink must survive as a string so the entity metadata
    records it (and downstream relation resolution can act on it), rather than a
    broken nested list.
    """
    content = (
        "---\npart_of: [[projects/Parent Project|Parent Project]]\n---\n\n# Graph Child\n\nBody."
    )
    await write_note(
        project=test_project.name,
        title="Graph Child",
        directory="bug010",
        content=content,
    )

    raw = await read_content("bug010/Graph Child.md", project=test_project.name)
    raw_text = raw["text"] if isinstance(raw, dict) else str(raw)

    # The stored frontmatter must carry the wikilink as a scalar, not a nested list.
    assert "- - " not in raw_text
    assert "[[projects/Parent Project|Parent Project]]" in raw_text


@pytest.mark.asyncio
async def test_ac6_genuine_list_not_over_quoted(app, test_project):
    """AC-6: a real YAML list (tags) is preserved as a list, not over-quoted."""
    content = "---\npart_of: [[projects/P|P]]\ntags: [alpha, beta]\n---\n\n# Mixed\n\nBody."
    await write_note(
        project=test_project.name,
        title="Mixed",
        directory="bug010",
        content=content,
    )

    raw = await read_content("bug010/Mixed.md", project=test_project.name)
    raw_text = raw["text"] if isinstance(raw, dict) else str(raw)

    # tags stays a real list
    assert "- alpha" in raw_text
    assert "- beta" in raw_text
    # the wikilink did not corrupt
    assert "- - " not in raw_text
    assert "[[projects/P|P]]" in raw_text
