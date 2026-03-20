"""Test that dataview_link relations are NOT persisted during sync.

The dataview system now operates entirely at the MCP layer (build_context/read_note
with enable_dataview=True). The sync path must NOT create dataview_link relations.
"""

import pytest
from pathlib import Path

from basic_memory.config import ProjectConfig
from basic_memory.repository import RelationRepository
from basic_memory.services import EntityService
from basic_memory.sync.sync_service import SyncService


async def create_note_with_dataview(project_dir: Path, name: str, content: str) -> Path:
    """Create a markdown file with dataview query."""
    file_path = project_dir / f"{name}.md"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    return file_path


@pytest.mark.asyncio
async def test_sync_does_not_create_dataview_link_relations(
    sync_service: SyncService,
    project_config: ProjectConfig,
    relation_repository: RelationRepository,
):
    """Syncing a note with ```dataview blocks must NOT create dataview_link relations."""
    project_dir = project_config.home

    # Create a target note
    await create_note_with_dataview(
        project_dir,
        "target-note",
        "---\ntitle: Target Note\npermalink: target-note\ntype: note\nstatus: Active\n---\n\n# Target Note\n\nSome content.\n",
    )

    # Create a note containing a dataview query that would match the target
    await create_note_with_dataview(
        project_dir,
        "index-note",
        '---\ntitle: Index Note\npermalink: index-note\n---\n\n# Index Note\n\n```dataview\nTABLE title, status\nFROM ""\nWHERE type = "note"\n```\n',
    )

    # Sync the directory
    await sync_service.sync(project_dir)

    # Verify: ZERO dataview_link relations should exist
    dataview_relations = await relation_repository.find_by_type("dataview_link")
    assert len(dataview_relations) == 0, (
        f"Expected 0 dataview_link relations after sync, got {len(dataview_relations)}. "
        "Dataview links should NOT be persisted during sync."
    )


@pytest.mark.asyncio
async def test_startup_cleanup_removes_existing_dataview_links(
    sync_service: SyncService,
    project_config: ProjectConfig,
    relation_repository: RelationRepository,
    entity_service: EntityService,
):
    """If dataview_link relations exist from a previous version, sync should clean them up."""
    project_dir = project_config.home

    # Create and sync a note to get an entity
    await create_note_with_dataview(
        project_dir,
        "cleanup-test",
        "---\ntitle: Cleanup Test\npermalink: cleanup-test\n---\n\n# Cleanup Test\n",
    )
    await sync_service.sync(project_dir)

    # Manually insert a legacy dataview_link relation
    from basic_memory.models import Relation

    entities = await sync_service.entity_repository.find_by_permalinks(["cleanup-test"])
    assert len(entities) == 1
    entity = entities[0]

    legacy_relation = Relation(
        project_id=entity.project_id,
        from_id=entity.id,
        to_id=entity.id,  # self-ref for simplicity
        to_name="Cleanup Test",
        relation_type="dataview_link",
    )
    await relation_repository.add(legacy_relation)

    # Verify the legacy relation exists
    before = await relation_repository.find_by_type("dataview_link")
    assert len(before) >= 1

    # Sync again -- should clean up legacy dataview_link relations
    await sync_service.sync(project_dir)

    after = await relation_repository.find_by_type("dataview_link")
    assert len(after) == 0, (
        f"Expected 0 dataview_link relations after cleanup sync, got {len(after)}. "
        "Startup cleanup should remove legacy dataview_link relations."
    )
