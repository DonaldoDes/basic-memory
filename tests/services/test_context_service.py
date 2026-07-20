"""Tests for context service."""

from datetime import datetime, timedelta, UTC

import pytest
import pytest_asyncio

from basic_memory.repository.search_repository import SearchIndexRow
from basic_memory.schemas.memory import memory_url, memory_url_path
from basic_memory.schemas.search import SearchItemType
from basic_memory.services.context_service import ContextService
from basic_memory.models.knowledge import Entity, Relation
from basic_memory.models.project import Project


@pytest_asyncio.fixture
async def context_service(
    search_repository, entity_repository, observation_repository, link_resolver
):
    """Create context service for testing."""
    return ContextService(
        search_repository, entity_repository, observation_repository, link_resolver=link_resolver
    )


@pytest.mark.asyncio
async def test_find_connected_depth_limit(context_service, test_graph):
    """Test depth limiting works.
    Our traversal path is:
    - Depth 0: Root
    - Depth 1: Relations + directly connected entities (Connected1, Connected2)
    - Depth 2: Relations + next level entities (Deep)
    """
    type_id_pairs = [("entity", test_graph["root"].id)]

    # With depth=1, we get direct connections
    # shallow_results = await context_service.find_related(type_id_pairs, max_depth=1)
    # shallow_entities = {(r.id, r.type) for r in shallow_results if r.type == "entity"}
    #
    # assert (test_graph["deep"].id, "entity") not in shallow_entities

    # search deeper
    deep_results = await context_service.find_related(type_id_pairs, max_depth=3, max_results=100)
    deep_entities = {(r.id, r.type) for r in deep_results if r.type == "entity"}
    print(deep_entities)
    # Should now include Deep entity
    assert (test_graph["deep"].id, "entity") in deep_entities


@pytest.mark.asyncio
async def test_find_connected_timeframe(
    context_service, test_graph, search_repository, entity_repository, app_config
):
    """Test timeframe filtering.
    This tests how traversal is affected by the item dates.
    When we filter by date, items are only included if:
    1. They match the timeframe
    2. There is a valid path to them through other items in the timeframe
    """
    # Skip for Postgres - needs investigation of duplicate key violations
    from basic_memory.config import DatabaseBackend

    if app_config.database_backend == DatabaseBackend.POSTGRES:
        pytest.skip("Not yet supported for Postgres - duplicate key violation issue")

    now = datetime.now(UTC)
    old_date = now - timedelta(days=10)
    recent_date = now - timedelta(days=1)

    # Update entity table timestamps directly
    # Root entity uses old date
    root_entity = test_graph["root"]
    await entity_repository.update(root_entity.id, {"created_at": old_date, "updated_at": old_date})

    # Connected entity uses recent date
    connected_entity = test_graph["connected1"]
    await entity_repository.update(
        connected_entity.id, {"created_at": recent_date, "updated_at": recent_date}
    )

    # Also update search_index for test consistency
    await search_repository.index_item(
        SearchIndexRow(
            project_id=entity_repository.project_id,
            id=test_graph["root"].id,
            title=test_graph["root"].title,
            content_snippet="Root content",
            permalink=test_graph["root"].permalink,
            file_path=test_graph["root"].file_path,
            type=SearchItemType.ENTITY,
            metadata={"created_at": old_date.isoformat()},
            created_at=old_date,
            updated_at=old_date,
        )
    )
    await search_repository.index_item(
        SearchIndexRow(
            project_id=entity_repository.project_id,
            id=test_graph["relations"][0].id,
            title="Root Entity → Connected Entity 1",
            content_snippet="",
            permalink=f"{test_graph['root'].permalink}/connects_to/{test_graph['connected1'].permalink}",
            file_path=test_graph["root"].file_path,
            type=SearchItemType.RELATION,
            from_id=test_graph["root"].id,
            to_id=test_graph["connected1"].id,
            relation_type="connects_to",
            metadata={"created_at": old_date.isoformat()},
            created_at=old_date,
            updated_at=old_date,
        )
    )
    await search_repository.index_item(
        SearchIndexRow(
            project_id=entity_repository.project_id,
            id=test_graph["connected1"].id,
            title=test_graph["connected1"].title,
            content_snippet="Connected 1 content",
            permalink=test_graph["connected1"].permalink,
            file_path=test_graph["connected1"].file_path,
            type=SearchItemType.ENTITY,
            metadata={"created_at": recent_date.isoformat()},
            created_at=recent_date,
            updated_at=recent_date,
        )
    )

    type_id_pairs = [("entity", test_graph["root"].id)]

    # Search with a 7-day cutoff
    since_date = now - timedelta(days=7)
    results = await context_service.find_related(type_id_pairs, since=since_date)

    # Only connected1 is recent, but we can't get to it
    # because its connecting relation is too old and is filtered out
    # (we can only reach connected1 through a relation starting from root)
    entity_ids = {r.id for r in results if r.type == "entity"}
    assert len(entity_ids) == 0  # No accessible entities within timeframe


@pytest.mark.asyncio
async def test_build_context(context_service, test_graph):
    """Test exact permalink lookup."""
    url = memory_url.validate_strings("memory://test-project/test/root")
    context_result = await context_service.build_context(url)

    # Check metadata
    assert context_result.metadata.uri == memory_url_path(url)
    assert context_result.metadata.depth == 1
    assert context_result.metadata.primary_count == 1
    assert context_result.metadata.related_count > 0
    assert context_result.metadata.generated_at is not None

    # Check results
    assert len(context_result.results) == 1
    context_item = context_result.results[0]

    # Check primary result
    primary_result = context_item.primary_result
    assert primary_result.id == test_graph["root"].id
    assert primary_result.type == "entity"
    assert primary_result.title == "Root"
    assert primary_result.permalink == "test-project/test/root"
    assert primary_result.file_path == "test/Root.md"
    assert primary_result.created_at is not None

    # Check related results
    assert len(context_item.related_results) > 0

    # Find related relation
    relation = next((r for r in context_item.related_results if r.type == "relation"), None)
    assert relation is not None
    assert relation.relation_type == "connects_to"
    assert relation.from_id == test_graph["root"].id
    assert relation.to_id == test_graph["connected1"].id

    # Find related entity
    related_entity = next((r for r in context_item.related_results if r.type == "entity"), None)
    assert related_entity is not None
    assert related_entity.id == test_graph["connected1"].id
    assert related_entity.title == test_graph["connected1"].title
    assert related_entity.permalink == test_graph["connected1"].permalink


@pytest.mark.asyncio
async def test_build_context_with_observations(context_service, test_graph):
    """Test context building with observations."""
    # The test_graph fixture already creates observations for root entity
    # Let's use those existing observations

    # Build context
    url = memory_url.validate_strings("memory://test-project/test/root")
    context_result = await context_service.build_context(url, include_observations=True)

    # Check the metadata
    assert context_result.metadata.total_observations > 0
    assert len(context_result.results) == 1

    # Check that observations were included
    context_item = context_result.results[0]
    assert len(context_item.observations) > 0

    # Check observation properties
    for observation in context_item.observations:
        assert observation.type == "observation"
        assert observation.category in ["note", "tech"]  # Categories from test_graph fixture
        assert observation.entity_id == test_graph["root"].id

    # Verify at least one observation has the correct category and content
    note_observation = next((o for o in context_item.observations if o.category == "note"), None)
    assert note_observation is not None
    assert "Root note" in note_observation.content


@pytest.mark.asyncio
async def test_build_context_observation_permalinks_match_search_index(
    context_service, search_service, entity_service
):
    """Regression test for #929: observation permalinks must match the search index.

    build_context used to rebuild the synthetic observation permalink inline,
    without the 200-char truncation (#446) or the content digest (#931) that
    Observation.permalink applies, so for long observations it returned
    permalinks the search index doesn't contain.
    """
    from basic_memory.schemas.base import Entity as EntitySchema
    from basic_memory.schemas.search import SearchQuery

    long_observation = "x" * 210 + " LONG_OBS_MARKER"
    entity, _ = await entity_service.create_or_update_entity(
        EntitySchema(
            title="Long Obs Entity",
            note_type="test",
            directory="test",
            content=f"# Long Obs Entity\n- [note] {long_observation}\n",
        )
    )
    await search_service.index_entity(entity)

    url = memory_url.validate_strings(f"memory://{entity.permalink}")
    context_result = await context_service.build_context(url, include_observations=True)
    assert len(context_result.results) == 1
    context_item = context_result.results[0]
    assert len(context_item.observations) == 1
    obs_row = context_item.observations[0]

    # The model property is the single definition of the permalink format
    assert obs_row.permalink == entity.observations[0].permalink

    # The search index row for this observation carries the same permalink
    index_rows = await search_service.search(SearchQuery(text="LONG_OBS_MARKER"))
    obs_permalinks = [r.permalink for r in index_rows if r.type == SearchItemType.OBSERVATION.value]
    assert obs_permalinks == [obs_row.permalink]


@pytest.mark.asyncio
async def test_build_context_not_found(context_service):
    """Test handling non-existent permalinks."""
    context = await context_service.build_context("memory://does/not/exist")
    assert len(context.results) == 0
    assert context.metadata.primary_count == 0
    assert context.metadata.related_count == 0


@pytest.mark.asyncio
async def test_context_metadata(context_service, test_graph):
    """Test metadata is correctly populated."""
    context = await context_service.build_context("memory://test-project/test/root", depth=2)
    metadata = context.metadata
    assert metadata.uri == "test-project/test/root"
    assert metadata.depth == 2
    assert metadata.generated_at is not None
    assert metadata.primary_count > 0


@pytest.mark.asyncio
async def test_project_isolation_in_find_related(session_maker, app_config):
    """Test that find_related respects project boundaries and doesn't leak data."""
    from basic_memory.repository.entity_repository import EntityRepository
    from basic_memory.repository.observation_repository import ObservationRepository
    from basic_memory.repository.sqlite_search_repository import SQLiteSearchRepository
    from basic_memory.repository.postgres_search_repository import PostgresSearchRepository
    from basic_memory.config import DatabaseBackend
    from basic_memory import db

    # Create database session
    async with db.scoped_session(session_maker) as db_session:
        # Create two separate projects
        project1 = Project(name="project1", path="/test1")
        project2 = Project(name="project2", path="/test2")
        db_session.add(project1)
        db_session.add(project2)
        await db_session.flush()

        # Create entities in project1
        entity1_p1 = Entity(
            title="Entity1_P1",
            note_type="document",
            content_type="text/markdown",
            project_id=project1.id,
            permalink="project1/entity1",
            file_path="project1/entity1.md",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        entity2_p1 = Entity(
            title="Entity2_P1",
            note_type="document",
            content_type="text/markdown",
            project_id=project1.id,
            permalink="project1/entity2",
            file_path="project1/entity2.md",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

        # Create entities in project2
        entity1_p2 = Entity(
            title="Entity1_P2",
            note_type="document",
            content_type="text/markdown",
            project_id=project2.id,
            permalink="project2/entity1",
            file_path="project2/entity1.md",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

        db_session.add_all([entity1_p1, entity2_p1, entity1_p2])
        await db_session.flush()

        # Create relation in project1 (between entities of project1)
        relation_p1 = Relation(
            project_id=project1.id,
            from_id=entity1_p1.id,
            to_id=entity2_p1.id,
            to_name="Entity2_P1",
            relation_type="connects_to",
        )
        db_session.add(relation_p1)
        await db_session.commit()

        # Create database-specific search repositories based on backend
        if app_config.database_backend == DatabaseBackend.POSTGRES:
            search_repo_p1 = PostgresSearchRepository(session_maker, project1.id)
            search_repo_p2 = PostgresSearchRepository(session_maker, project2.id)
        else:
            search_repo_p1 = SQLiteSearchRepository(session_maker, project1.id)
            search_repo_p2 = SQLiteSearchRepository(session_maker, project2.id)

        # Create repositories for project1
        entity_repo_p1 = EntityRepository(session_maker, project1.id)
        obs_repo_p1 = ObservationRepository(session_maker, project1.id)
        context_service_p1 = ContextService(search_repo_p1, entity_repo_p1, obs_repo_p1)

        # Create repositories for project2
        entity_repo_p2 = EntityRepository(session_maker, project2.id)
        obs_repo_p2 = ObservationRepository(session_maker, project2.id)
        context_service_p2 = ContextService(search_repo_p2, entity_repo_p2, obs_repo_p2)

        # Test: find_related for project1 should only return project1 entities
        type_id_pairs_p1 = [("entity", entity1_p1.id)]
        related_p1 = await context_service_p1.find_related(type_id_pairs_p1, max_depth=2)

        # Verify only project1 entities are returned
        related_entity_ids = [r.id for r in related_p1 if r.type == "entity"]
        assert entity2_p1.id in related_entity_ids  # Should find connected entity2 in project1
        assert entity1_p2.id not in related_entity_ids  # Should NOT find entity from project2

        # Test: find_related for project2 should return empty (no relations)
        type_id_pairs_p2 = [("entity", entity1_p2.id)]
        related_p2 = await context_service_p2.find_related(type_id_pairs_p2, max_depth=2)

        # Project2 has no relations, so should return empty
        assert len(related_p2) == 0

        # Double-check: verify entities exist in their respective projects
        assert entity1_p1.project_id == project1.id
        assert entity2_p1.project_id == project1.id
        assert entity1_p2.project_id == project2.id


@pytest.mark.asyncio
async def test_find_related_expands_cross_project_relation_targets(session_maker, app_config):
    """Explicit cross-project links should expand without exposing unrelated incoming links."""
    from basic_memory.repository.entity_repository import EntityRepository
    from basic_memory.repository.observation_repository import ObservationRepository
    from basic_memory.repository.sqlite_search_repository import SQLiteSearchRepository
    from basic_memory.repository.postgres_search_repository import PostgresSearchRepository
    from basic_memory.config import DatabaseBackend
    from basic_memory import db

    async with db.scoped_session(session_maker) as db_session:
        project1 = Project(name="project1", path="/test1")
        project2 = Project(name="project2", path="/test2")
        project3 = Project(name="project3", path="/test3")
        db_session.add_all([project1, project2, project3])
        await db_session.flush()

        now = datetime.now(UTC)
        source = Entity(
            title="Source",
            note_type="document",
            content_type="text/markdown",
            project_id=project1.id,
            permalink="project1/source",
            file_path="project1/source.md",
            created_at=now,
            updated_at=now,
        )
        target = Entity(
            title="Company Standards",
            note_type="document",
            content_type="text/markdown",
            project_id=project2.id,
            permalink="project2/company-standards",
            file_path="project2/company-standards.md",
            created_at=now,
            updated_at=now,
        )
        target_child = Entity(
            title="Review Checklist",
            note_type="document",
            content_type="text/markdown",
            project_id=project2.id,
            permalink="project2/review-checklist",
            file_path="project2/review-checklist.md",
            created_at=now,
            updated_at=now,
        )
        unrelated_source = Entity(
            title="Unrelated Source",
            note_type="document",
            content_type="text/markdown",
            project_id=project3.id,
            permalink="project3/unrelated-source",
            file_path="project3/unrelated-source.md",
            created_at=now,
            updated_at=now,
        )
        db_session.add_all([source, target, target_child, unrelated_source])
        await db_session.flush()

        cross_project_relation = Relation(
            project_id=project1.id,
            from_id=source.id,
            to_id=target.id,
            to_name="Company Standards",
            relation_type="links_to",
        )
        target_relation = Relation(
            project_id=project2.id,
            from_id=target.id,
            to_id=target_child.id,
            to_name="Review Checklist",
            relation_type="links_to",
        )
        unrelated_incoming_relation = Relation(
            project_id=project3.id,
            from_id=unrelated_source.id,
            to_id=target.id,
            to_name="Company Standards",
            relation_type="links_to",
        )
        db_session.add_all([cross_project_relation, target_relation, unrelated_incoming_relation])
        await db_session.commit()

    if app_config.database_backend == DatabaseBackend.POSTGRES:
        search_repo_p1 = PostgresSearchRepository(session_maker, project1.id)
    else:
        search_repo_p1 = SQLiteSearchRepository(session_maker, project1.id)

    entity_repo_p1 = EntityRepository(session_maker, project1.id)
    obs_repo_p1 = ObservationRepository(session_maker, project1.id)
    context_service_p1 = ContextService(search_repo_p1, entity_repo_p1, obs_repo_p1)

    await search_repo_p1.index_item(
        SearchIndexRow(
            project_id=project1.id,
            id=source.id,
            title=source.title,
            content_snippet="Source content",
            permalink=source.permalink,
            file_path=source.file_path,
            type=SearchItemType.ENTITY,
            metadata={"created_at": now.isoformat()},
            created_at=now,
            updated_at=now,
        )
    )

    context = await context_service_p1.build_context(
        memory_url.validate_strings("memory://project1/source"),
        depth=2,
        max_related=100,
    )
    assert len(context.results) == 1

    context_related_entity_ids = {
        row.id for row in context.results[0].related_results if row.type == "entity"
    }
    context_related_relation_ids = {
        row.id for row in context.results[0].related_results if row.type == "relation"
    }

    assert target.id in context_related_entity_ids
    assert target_child.id in context_related_entity_ids
    assert unrelated_source.id not in context_related_entity_ids
    assert cross_project_relation.id in context_related_relation_ids
    assert target_relation.id in context_related_relation_ids
    assert unrelated_incoming_relation.id not in context_related_relation_ids

    related = await context_service_p1.find_related(
        [("entity", source.id)], max_depth=2, max_results=100
    )

    related_entity_ids = {row.id for row in related if row.type == "entity"}
    related_relation_ids = {row.id for row in related if row.type == "relation"}

    assert target.id in related_entity_ids
    assert target_child.id in related_entity_ids
    assert unrelated_source.id not in related_entity_ids
    assert cross_project_relation.id in related_relation_ids
    assert target_relation.id in related_relation_ids
    assert unrelated_incoming_relation.id not in related_relation_ids


@pytest.mark.asyncio
async def test_find_related_does_not_revisit_entities_in_cycles(session_maker, app_config):
    """Recursive graph expansion should stop when a path loops back to a visited entity."""
    from basic_memory.repository.entity_repository import EntityRepository
    from basic_memory.repository.observation_repository import ObservationRepository
    from basic_memory.repository.sqlite_search_repository import SQLiteSearchRepository
    from basic_memory.repository.postgres_search_repository import PostgresSearchRepository
    from basic_memory.config import DatabaseBackend
    from basic_memory import db

    async with db.scoped_session(session_maker) as db_session:
        project = Project(name="cycle-project", path="/cycle")
        db_session.add(project)
        await db_session.flush()

        now = datetime.now(UTC)
        root = Entity(
            title="Root",
            note_type="document",
            content_type="text/markdown",
            project_id=project.id,
            permalink="cycle/root",
            file_path="cycle/root.md",
            created_at=now,
            updated_at=now,
        )
        connected = Entity(
            title="Connected",
            note_type="document",
            content_type="text/markdown",
            project_id=project.id,
            permalink="cycle/connected",
            file_path="cycle/connected.md",
            created_at=now,
            updated_at=now,
        )
        db_session.add_all([root, connected])
        await db_session.flush()

        root_to_connected = Relation(
            project_id=project.id,
            from_id=root.id,
            to_id=connected.id,
            to_name="Connected",
            relation_type="links_to",
        )
        connected_to_root = Relation(
            project_id=project.id,
            from_id=connected.id,
            to_id=root.id,
            to_name="Root",
            relation_type="links_to",
        )
        db_session.add_all([root_to_connected, connected_to_root])
        await db_session.commit()

    if app_config.database_backend == DatabaseBackend.POSTGRES:
        search_repo = PostgresSearchRepository(session_maker, project.id)
    else:
        search_repo = SQLiteSearchRepository(session_maker, project.id)

    entity_repo = EntityRepository(session_maker, project.id)
    obs_repo = ObservationRepository(session_maker, project.id)
    context_service = ContextService(search_repo, entity_repo, obs_repo)

    related = await context_service.find_related(
        [("entity", root.id)], max_depth=4, max_results=100
    )

    related_entity_ids = [row.id for row in related if row.type == "entity"]
    related_relation_ids = {row.id for row in related if row.type == "relation"}

    assert related_entity_ids == [connected.id]
    assert related_relation_ids == {root_to_connected.id, connected_to_root.id}


@pytest.mark.asyncio
async def test_build_context_fallback_via_link_resolver(context_service, test_graph):
    """Test that build_context falls back to LinkResolver when exact permalink fails.

    The test_graph creates entities with permalinks like 'test-project/test/root'.
    Looking up by title ('Root') won't match the exact permalink, but LinkResolver
    can resolve it via title matching.
    """
    # This identifier is the entity title, not a permalink — exact lookup will fail
    url = memory_url.validate_strings("memory://Root")
    context_result = await context_service.build_context(url)

    # LinkResolver should resolve 'Root' → entity with permalink 'test-project/test/root'
    assert context_result.metadata.primary_count == 1
    assert len(context_result.results) == 1
    assert context_result.results[0].primary_result.id == test_graph["root"].id


@pytest.mark.asyncio
async def test_build_context_fallback_not_found(context_service):
    """Test that build_context returns empty when both exact lookup and fallback fail."""
    url = memory_url.validate_strings("memory://completely-nonexistent-note-xyz")
    context_result = await context_service.build_context(url)

    assert context_result.metadata.primary_count == 0
    assert len(context_result.results) == 0


@pytest.mark.asyncio
async def test_build_context_without_link_resolver(
    search_repository, entity_repository, observation_repository, test_graph
):
    """Test that build_context still works without a link_resolver (no fallback)."""
    service = ContextService(search_repository, entity_repository, observation_repository)

    # Exact permalink lookup should still work
    url = memory_url.validate_strings("memory://test-project/test/root")
    context_result = await service.build_context(url)
    assert context_result.metadata.primary_count == 1

    # Title-based lookup should return empty (no fallback available)
    url = memory_url.validate_strings("memory://Root")
    context_result = await service.build_context(url)
    assert context_result.metadata.primary_count == 0


@pytest.mark.asyncio
async def test_find_related_carries_to_name_for_unresolved_relations(session_maker, app_config):
    """Relation rows expose to_name so unresolved forward refs render by name (#955).

    A forward reference (to_id NULL) previously surfaced with no usable target
    text — build_context printed [[None]] even though the markdown named the
    target. The context query must select to_name for both resolved and
    unresolved relation rows.
    """
    from basic_memory.repository.entity_repository import EntityRepository
    from basic_memory.repository.observation_repository import ObservationRepository
    from basic_memory.repository.sqlite_search_repository import SQLiteSearchRepository
    from basic_memory.repository.postgres_search_repository import PostgresSearchRepository
    from basic_memory.config import DatabaseBackend
    from basic_memory import db

    async with db.scoped_session(session_maker) as db_session:
        project = Project(name="forward-ref-project", path="/forward-ref")
        db_session.add(project)
        await db_session.flush()

        source = Entity(
            title="write-note(3)",
            note_type="document",
            content_type="text/markdown",
            project_id=project.id,
            permalink="man3/write-note-3",
            file_path="man3/write-note-3.md",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        resolved_target = Entity(
            title="bm-note(5)",
            note_type="document",
            content_type="text/markdown",
            project_id=project.id,
            permalink="man5/bm-note-5",
            file_path="man5/bm-note-5.md",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        db_session.add_all([source, resolved_target])
        await db_session.flush()

        resolved = Relation(
            project_id=project.id,
            from_id=source.id,
            to_id=resolved_target.id,
            to_name="bm-note(5)",
            relation_type="see_also",
        )
        # Forward reference: the target page does not exist yet
        unresolved = Relation(
            project_id=project.id,
            from_id=source.id,
            to_id=None,
            to_name="edit-note(3)",
            relation_type="see_also",
        )
        db_session.add_all([resolved, unresolved])
        await db_session.commit()

        if app_config.database_backend == DatabaseBackend.POSTGRES:
            search_repo = PostgresSearchRepository(session_maker, project.id)
        else:
            search_repo = SQLiteSearchRepository(session_maker, project.id)
        entity_repo = EntityRepository(session_maker, project.id)
        obs_repo = ObservationRepository(session_maker, project.id)
        context_service = ContextService(search_repo, entity_repo, obs_repo)

        related = await context_service.find_related([("entity", source.id)], max_depth=2)
        relation_rows = {r.to_name: r for r in related if r.type == "relation"}

        assert "edit-note(3)" in relation_rows, "unresolved relation row missing to_name"
        assert relation_rows["edit-note(3)"].to_id is None
        assert relation_rows["bm-note(5)"].to_name == "bm-note(5)"


@pytest.mark.asyncio
async def test_pattern_search_falls_back_for_legacy_unqualified_rows(context_service, test_graph):
    """Workspace-qualified patterns fall back to the project form for legacy rows (#957).

    Rows written before workspace canonicalization (or via clients that did not
    forward workspace headers) store project-qualified permalinks. A pattern
    canonicalized under an active workspace context would otherwise match
    nothing — the field failure that opened the issue.
    """
    from basic_memory.workspace_context import workspace_permalink_context

    # test_graph rows are stored without any workspace prefix (legacy form).
    # Query with a workspace-qualified pattern under an active context.
    with workspace_permalink_context(workspace_slug="team-paul", workspace_type="organization"):
        context = await context_service.build_context("memory://team-paul/test-project/test/*")

    permalinks = {result.primary_result.permalink for result in context.results}
    assert permalinks, "fallback did not match legacy rows"
    assert all(p and p.startswith("test-project/test/") for p in permalinks), permalinks


# ---------------------------------------------------------------------------
# BUG-022: build_context must not silently substitute an arbitrary note when an
# EXACT memory:// URL fails to resolve but its tokens match the FTS corpus.
#
# Root cause (two points):
#   - context_service.py exact-URL branch calls resolve_link(strict=False),
#     authorizing fuzzy FTS fallback.
#   - link_resolver.py step 5 returns results[0] unconditionally (no score
#     threshold).
# Fix: seal the build_context call-site with strict=True so exact resolutions
#   (permalink/title/file_path) survive but the unscored FTS fallback is bypassed.
#   The strict=False default of resolve_link is UNCHANGED for sync/indexing
#   wikilink resolution (test_link_resolver.py covers that; BUG-022-04 below
#   re-asserts it at this layer).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_context_no_silent_substitution_on_unresolved_exact_url(
    context_service, test_graph
):
    """BUG-022-01 (adversarial): an exact URL that does NOT resolve but whose
    token fuzzy-matches the corpus must return empty — never a substituted note.

    ``memory://connected`` is not an existing permalink/title/file_path, but the
    token ``connected`` FTS-fuzzy-matches "Connected Entity 1"/"2". Before the
    fix, resolve_link(strict=False) fell through to the unscored FTS top-1 and
    build_context returned that arbitrary note (primary_count == 1). The caller
    could not distinguish this from a deliberate hit.
    """
    url = memory_url.validate_strings("memory://connected")
    context_result = await context_service.build_context(url)

    assert context_result.metadata.primary_count == 0, (
        "unresolved exact URL substituted an arbitrary FTS match "
        f"(got {context_result.metadata.primary_count} primary result(s))"
    )
    assert context_result.results == []
    # metadata.uri must NOT be rewritten to a substituted note's permalink.
    assert "connected-entity" not in (context_result.metadata.uri or "")


@pytest.mark.asyncio
async def test_build_context_empty_on_unresolved_gibberish_url(context_service, test_graph):
    """BUG-022-02: a second, independent witness — an exact URL made of pure
    gibberish tokens must also return empty after the fix.

    Note: in the unit test environment the fuzzy fallback (search_service) returns
    a nearest-neighbour even for gibberish (no ``semantic_min_similarity`` gate,
    unlike prod), so before the fix this ALSO substitutes an arbitrary note. The
    fix bypasses the unscored fallback entirely, so the result is empty regardless
    of search backend or token content — which is exactly why the fix is robust to
    the prod ``hybrid`` / ``min_similarity=0.55`` configuration."""
    url = memory_url.validate_strings("memory://zzz-nonexistent-note-qqqwxyz")
    context_result = await context_service.build_context(url)

    assert context_result.metadata.primary_count == 0
    assert context_result.results == []


@pytest.mark.asyncio
async def test_build_context_exact_url_still_resolves(context_service, test_graph):
    """BUG-022-03: an exact URL that resolves normally is unaffected by the fix
    (no regression on the nominal path)."""
    url = memory_url.validate_strings("memory://test-project/test/root")
    context_result = await context_service.build_context(url)

    assert context_result.metadata.primary_count == 1
    assert context_result.results[0].primary_result.id == test_graph["root"].id


@pytest.mark.asyncio
async def test_build_context_exact_title_and_path_resolutions_survive_fix(
    context_service, test_graph
):
    """BUG-022-03b: the fix bypasses only the unscored FTS fuzzy step. Exact
    resolutions 1-4 (permalink, title, file_path, file_path.md) MUST still work
    through the build_context exact-URL branch, so that a URL addressing a note
    by title or by file path keeps resolving."""
    # Resolve by exact title ("Root") — reaches resolve_link, which returns the
    # title match (step 2) even in strict mode.
    by_title = await context_service.build_context(memory_url.validate_strings("memory://Root"))
    assert by_title.metadata.primary_count == 1
    assert by_title.results[0].primary_result.id == test_graph["root"].id

    # Resolve by exact file_path ("test/Root.md") — reaches resolve_link file
    # path match (steps 3-4), preserved in strict mode.
    by_path = await context_service.build_context(
        memory_url.validate_strings("memory://test/Root.md")
    )
    assert by_path.metadata.primary_count == 1
    assert by_path.results[0].primary_result.id == test_graph["root"].id


@pytest.mark.asyncio
async def test_link_resolver_fuzzy_wikilink_still_works_for_sync(link_resolver, test_graph):
    """BUG-022-04 (non-regression, critical): the fix is sealed to the
    build_context call-site. resolve_link's DEFAULT (strict=False) fuzzy
    matching — used massively to resolve ``[[...]]`` wikilinks during
    sync/indexing (sync_service, batch_indexer, entity_service,
    knowledge_router) — MUST remain intact. A partial/approximate wikilink still
    resolves to its best FTS match."""
    # "Connectd Entty" (misspelled, no exact permalink/title/file_path) can only
    # be resolved via the fuzzy FTS fallback. The crisp contract:
    #   - strict=False (sync/indexing default) → still fuzzy-resolves (non-None).
    #   - strict=True  (build_context call-site after fix) → returns None.
    # This proves the fix is sealed to the call-site and did not alter the
    # resolve_link default relied upon by sync_service/batch_indexer/entity_service.
    # "Connected Entity" (no trailing number) matches no exact title/permalink/
    # file_path (real titles are "Connected Entity 1"/"2"), so it can only be
    # resolved via the fuzzy FTS fallback.
    fuzzy = await link_resolver.resolve_link("Connected Entity", strict=False)
    assert fuzzy is not None, "sync fuzzy wikilink resolution regressed"

    strict = await link_resolver.resolve_link("Connected Entity", strict=True)
    assert strict is None, "strict mode must not fuzzy-resolve"


# ---------------------------------------------------------------------------
# US-006b: build_context must project search_index.summary onto related entity
# rows (SUM-13 SQLite / SUM-14 Postgres — same body, dual backend). The summary
# column is populated by US-006a; this US only exposes it.
#
# Sensitive zone ("Résolution de contexte"): the projection is orthogonal to the
# BUG-022 strict-resolution seal — an adversarial test re-asserts that adding
# si.summary to the CTE does not reintroduce silent note substitution and that
# the UNION ALL arity stays consistent (a broken arity raises OperationalError).
# ---------------------------------------------------------------------------

from sqlalchemy import text as _sql_text  # noqa: E402


@pytest.mark.asyncio
async def test_sum13_find_related_projects_summary_on_entities_null_on_relations(
    context_service, test_graph
):
    """SUM-13/14: the recursive CTE projects search_index.summary onto related
    ENTITY rows; RELATION rows carry summary = NULL (symmetric to content)."""
    sentinel = {
        test_graph["connected1"].id: "Summary of connected one.",
        test_graph["connected2"].id: "Summary of connected two.",
    }
    for eid, summ in sentinel.items():
        await context_service.search_repository.execute_query(
            _sql_text("UPDATE search_index SET summary = :s WHERE id = :id AND type = 'entity'"),
            params={"s": summ, "id": eid},
        )

    related = await context_service.find_related(
        [("entity", test_graph["root"].id)], max_depth=3, max_results=100
    )

    entity_rows = {r.id: r for r in related if r.type == "entity"}
    assert entity_rows[test_graph["connected1"].id].summary == "Summary of connected one."
    assert entity_rows[test_graph["connected2"].id].summary == "Summary of connected two."

    # Adversarial (arity/CAST): relation rows must never carry an entity summary.
    relation_rows = [r for r in related if r.type == "relation"]
    assert relation_rows, "expected at least one relation row in the traversal"
    assert all(r.summary is None for r in relation_rows), (
        "a relation row leaked a non-null summary — UNION ALL summary column "
        "must be NULL on relation branches"
    )


@pytest.mark.asyncio
async def test_sum13_summary_projection_preserves_strict_resolution(context_service, test_graph):
    """Adversarial (sensitive zone, BUG-022 non-regression): adding si.summary to
    the CTE must not reintroduce the silent substitution of an unresolved exact
    URL, and a resolved URL must still traverse with summary available on related.
    """
    # Unresolved exact URL whose tokens fuzzy-match the corpus → still empty.
    empty = await context_service.build_context(memory_url.validate_strings("memory://connected"))
    assert empty.metadata.primary_count == 0
    assert empty.results == []
    assert "connected-entity" not in (empty.metadata.uri or "")

    # Resolved URL → related entities expose the summary attribute end-to-end.
    ctx = await context_service.build_context(
        memory_url.validate_strings("memory://test-project/test/root")
    )
    assert ctx.metadata.primary_count == 1
    related_entities = [r for r in ctx.results[0].related_results if r.type == "entity"]
    assert related_entities, "expected related entities in the traversal"
    assert all(hasattr(r, "summary") for r in related_entities)
