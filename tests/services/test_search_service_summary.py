"""Integration tests for summary persistence at indexation (US-006a).

Test IDs SUM-07..SUM-12. These run against SQLite by default and against
Postgres under BASIC_MEMORY_TEST_POSTGRES=1 (same test bodies, dual backend):
- SUM-07 (SQLite) / SUM-08 (Postgres): indexing populates the summary column.
- SUM-09: persisted value equals compute_summary(...) — parity by construction.
- SUM-10 (SQLite) / SUM-11 (Postgres): reindex backfills every entity row.
- SUM-12: freshness — changing description then reindexing updates summary.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from basic_memory.services.search_service import compute_summary


async def _create_entity(entity_repository, *, title, permalink, description=None):
    data = {
        "project_id": entity_repository.project_id,
        "title": title,
        "note_type": "test",
        "permalink": permalink,
        "file_path": f"{permalink}.md",
        "content_type": "text/markdown",
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    if description is not None:
        data["entity_metadata"] = {"description": description}
    return await entity_repository.create(data)


async def _fetch_summary(search_service, entity_id):
    result = await search_service.repository.execute_query(
        text("SELECT summary FROM search_index WHERE entity_id = :eid AND type = 'entity'"),
        params={"eid": entity_id},
    )
    row = result.fetchone()
    return row[0] if row else None


@pytest.mark.asyncio
async def test_sum07_indexing_populates_summary_from_description(search_service, entity_repository):
    """SUM-07/08: indexing an entity persists its computed summary."""
    entity = await _create_entity(
        entity_repository,
        title="Summary Note",
        permalink="test/summary-note",
        description="This is the frontmatter description.",
    )
    await search_service.index_entity_markdown(entity, content="# Summary Note\n\nBody text.")

    summary = await _fetch_summary(search_service, entity.id)
    assert summary == "This is the frontmatter description."


@pytest.mark.asyncio
async def test_sum07_indexing_populates_summary_from_content_lead(
    search_service, entity_repository
):
    """SUM-07/08: with no description, the content lead is persisted."""
    entity = await _create_entity(entity_repository, title="Lead Note", permalink="test/lead-note")
    await search_service.index_entity_markdown(
        entity, content="# Lead Note\n\nThe first meaningful sentence of the body."
    )

    summary = await _fetch_summary(search_service, entity.id)
    assert summary == "The first meaningful sentence of the body."


@pytest.mark.asyncio
async def test_sum09_persisted_value_matches_compute_summary(search_service, entity_repository):
    """SUM-09: the persisted value is exactly compute_summary(...) — parity."""
    description = "Parity description used to derive the summary. " * 6
    content = "# Note\n\nsome body"
    entity = await _create_entity(
        entity_repository,
        title="Parity Note",
        permalink="test/parity-note",
        description=description,
    )
    await search_service.index_entity_markdown(entity, content=content)

    summary = await _fetch_summary(search_service, entity.id)
    assert summary == compute_summary(description, "some body")
    assert summary is not None
    assert len(summary) <= 201  # bounded, cut before insert


@pytest.mark.asyncio
async def test_sum09_none_summary_persisted_as_null(search_service, entity_repository):
    """A degraded entity (no description, no exploitable lead) stores NULL summary."""
    entity = await _create_entity(
        entity_repository, title="Empty Note", permalink="test/empty-note"
    )
    await search_service.index_entity_markdown(entity, content="# Empty Note\n\n")

    summary = await _fetch_summary(search_service, entity.id)
    assert summary is None


@pytest.mark.asyncio
async def test_sum10_reindex_backfills_all_entity_rows(search_service, entity_repository):
    """SUM-10/11: reindexing every entity populates the summary column on all rows."""
    entities = []
    for i in range(5):
        e = await _create_entity(
            entity_repository,
            title=f"Backfill Note {i}",
            permalink=f"test/backfill-{i}",
            description=f"Description number {i}.",
        )
        entities.append(e)

    # Simulate a full reindex pass over all entities.
    for e in entities:
        await search_service.index_entity_markdown(e, content=f"# Backfill {e.title}\n\nbody")

    for e in entities:
        summary = await _fetch_summary(search_service, e.id)
        assert summary is not None
        assert summary.startswith("Description number")


@pytest.mark.asyncio
async def test_sum12_freshness_reindex_updates_summary(search_service, entity_repository):
    """SUM-12: changing description then reindexing refreshes the persisted summary."""
    entity = await _create_entity(
        entity_repository,
        title="Fresh Note",
        permalink="test/fresh-note",
        description="Original description.",
    )
    await search_service.index_entity_markdown(entity, content="# Fresh Note\n\nbody")
    assert await _fetch_summary(search_service, entity.id) == "Original description."

    # Mutate the frontmatter description and reindex (delete + reinsert, the
    # semantics of a real reindex pass over an existing entity).
    entity.entity_metadata = {"description": "Updated description after edit."}
    await entity_repository.update(entity.id, {"entity_metadata": entity.entity_metadata})
    refreshed = await entity_repository.get_by_id(entity.id)
    await search_service.repository.delete_by_entity_id(entity.id)
    await search_service.index_entity_markdown(refreshed, content="# Fresh Note\n\nbody")

    assert await _fetch_summary(search_service, entity.id) == "Updated description after edit."


@pytest.mark.asyncio
async def test_sum_adversarial_hostile_description_persisted_safely(
    search_service, entity_repository
):
    """Adversarial (SQL/FTS5 zone): hostile description persists via parameterized
    INSERT without corrupting or dropping search_index."""
    hostile = 'x"; DROP TABLE search_index; -- MATCH "a" OR b'
    entity = await _create_entity(
        entity_repository,
        title="Hostile Note",
        permalink="test/hostile-note",
        description=hostile,
    )
    await search_service.index_entity_markdown(entity, content="# Hostile\n\nbody")

    # Table still exists and the value round-trips intact.
    summary = await _fetch_summary(search_service, entity.id)
    assert summary == compute_summary(hostile, "body")
    assert "DROP TABLE" in summary
    # search_index is intact: a normal search still works.
    result = await search_service.repository.execute_query(
        text("SELECT COUNT(*) FROM search_index WHERE entity_id = :eid"),
        params={"eid": entity.id},
    )
    assert result.scalar_one() >= 1
