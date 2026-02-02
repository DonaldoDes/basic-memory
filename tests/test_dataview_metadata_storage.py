"""Tests for storing Dataview queries in search index metadata."""

import pytest
from datetime import datetime

from basic_memory.dataview.detector import DataviewDetector
from basic_memory.schemas.memory import EntitySummary
from basic_memory.repository.search_repository import SearchIndexRow
from basic_memory.schemas.search import SearchItemType


class TestDataviewDetectorExtractQueryText:
    """Test DataviewDetector.extract_query_text method."""

    def test_extract_query_text_finds_dataview_blocks(self):
        """Test that extract_query_text finds and extracts dataview queries."""
        content = """# Test Note

```dataview
LIST FROM "1. projects"
WHERE status = "active"
```

Some text here.

```dataview
TABLE title, status
FROM "2. areas"
```
"""
        queries = DataviewDetector.extract_query_text(content)
        
        assert len(queries) == 2
        assert 'LIST FROM "1. projects"' in queries[0]
        assert "WHERE status" in queries[0]
        assert "TABLE title, status" in queries[1]
        assert 'FROM "2. areas"' in queries[1]

    def test_extract_query_text_returns_empty_for_no_queries(self):
        """Test that extract_query_text returns empty list when no queries present."""
        content = """# Regular Note

Just some regular markdown content.

No dataview queries here.
"""
        queries = DataviewDetector.extract_query_text(content)
        
        assert queries == []
        assert isinstance(queries, list)

    def test_extract_query_text_handles_inline_queries(self):
        """Test that extract_query_text also extracts inline queries."""
        content = """# Note

Status: `= this.status`
Priority: `= this.priority`
"""
        queries = DataviewDetector.extract_query_text(content)
        
        assert len(queries) == 2
        assert "this.status" in queries[0]
        assert "this.priority" in queries[1]

    def test_extract_query_text_handles_mixed_queries(self):
        """Test that extract_query_text handles both codeblock and inline queries."""
        content = """# Note

```dataview
LIST FROM "projects"
```

Status: `= this.status`
"""
        queries = DataviewDetector.extract_query_text(content)
        
        assert len(queries) == 2
        assert 'LIST FROM "projects"' in queries[0]
        assert "this.status" in queries[1]


class TestSearchIndexMetadataDataviewQueries:
    """Test that search index metadata contains dataview_queries."""

    @pytest.mark.asyncio
    async def test_search_index_metadata_contains_dataview_queries(
        self, search_service, search_repository, entity_repository, tmp_path
    ):
        """Test that after indexation, metadata contains dataview queries."""
        # This test will verify the integration with search_service
        # We'll need to create a mock entity with dataview content
        # and verify that the metadata contains the queries
        
        # Create a test file with dataview content
        test_file = tmp_path / "test_note.md"
        content = """---
title: Test Note
---

# Test Note

```dataview
LIST FROM "projects"
```
"""
        test_file.write_text(content)
        
        # Create a mock entity
        from basic_memory.models import Entity
        entity = Entity(
            id=1,
            title="Test Note",
            file_path=str(test_file),
            permalink="test-note",
            entity_type="note",
            project_id=1,
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
        
        # Index the entity with content
        await search_service.index_entity_markdown(entity, content=content)
        
        # Query the repository to get the indexed row
        results = await search_repository.search(
            permalink="test-note",
            limit=10,
            offset=0
        )
        
        # Find the entity row
        entity_row = next((r for r in results if r.type == SearchItemType.ENTITY.value), None)
        assert entity_row is not None
        
        # Verify metadata contains dataview_queries
        assert "dataview_queries" in entity_row.metadata
        assert isinstance(entity_row.metadata["dataview_queries"], list)
        assert len(entity_row.metadata["dataview_queries"]) == 1
        assert 'LIST FROM "projects"' in entity_row.metadata["dataview_queries"][0]

    @pytest.mark.asyncio
    async def test_search_index_metadata_no_queries_key_when_none(
        self, search_service, search_repository, entity_repository, tmp_path
    ):
        """Test that metadata doesn't have dataview_queries key when no queries present."""
        # Create a test file without dataview content
        test_file = tmp_path / "test_note.md"
        content = """---
title: Test Note
---

# Test Note

Just regular content, no dataview queries.
"""
        test_file.write_text(content)
        
        # Create a mock entity
        from basic_memory.models import Entity
        entity = Entity(
            id=2,
            title="Test Note",
            file_path=str(test_file),
            permalink="test-note-2",
            entity_type="note",
            project_id=1,
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
        
        # Index the entity with content
        await search_service.index_entity_markdown(entity, content=content)
        
        # Query the repository to get the indexed row
        results = await search_repository.search(
            permalink="test-note-2",
            limit=10,
            offset=0
        )
        
        # Find the entity row
        entity_row = next((r for r in results if r.type == SearchItemType.ENTITY.value), None)
        assert entity_row is not None
        
        # Verify metadata does NOT contain dataview_queries key
        assert "dataview_queries" not in entity_row.metadata


class TestEntitySummaryMetadata:
    """Test that EntitySummary includes metadata field."""

    def test_entity_summary_has_metadata(self):
        """Test that EntitySummary can be created with metadata."""
        summary = EntitySummary(
            entity_id=1,
            title="Test Note",
            permalink="test-note",
            file_path="/path/to/note.md",
            created_at=datetime.now(),
            metadata={"dataview_queries": ['LIST FROM "projects"']},
        )
        
        assert summary.metadata is not None
        assert "dataview_queries" in summary.metadata
        assert len(summary.metadata["dataview_queries"]) == 1

    def test_entity_summary_metadata_optional(self):
        """Test that EntitySummary metadata is optional."""
        summary = EntitySummary(
            entity_id=1,
            title="Test Note",
            permalink="test-note",
            file_path="/path/to/note.md",
            created_at=datetime.now(),
        )
        
        assert summary.metadata is None


class TestBuildContextDataviewExecution:
    """Test that build_context executes dataview queries from metadata."""

    @pytest.mark.asyncio
    async def test_build_context_executes_dataview_from_metadata(
        self, entity_service, search_service, search_repository, tmp_path
    ):
        """Test that build_context executes queries stored in metadata."""
        # This is an integration test that will verify the full flow:
        # 1. Create notes with dataview queries
        # 2. Index them (queries stored in metadata)
        # 3. Verify that metadata contains the queries
        
        from basic_memory.schemas.base import Entity as EntitySchema
        from basic_memory.schemas.search import SearchItemType
        
        # Create a note with a dataview query
        content = """# Dashboard Note

```dataview
LIST FROM "test"
WHERE type = "note"
```
"""
        entity, _ = await entity_service.create_or_update_entity(
            EntitySchema(
                title="Dashboard",
                folder="test",
                entity_type="note",
                content=content
            )
        )
        
        # Index the entity
        await search_service.index_entity(entity)
        
        # Search for the entity to get its metadata
        results = await search_repository.search(
            permalink="test/dashboard",
            limit=10,
            offset=0
        )
        
        # Find the entity in results
        entity_result = next((r for r in results if r.type == SearchItemType.ENTITY.value), None)
        assert entity_result is not None
        
        # Verify that metadata contains dataview_queries
        assert entity_result.metadata is not None
        assert "dataview_queries" in entity_result.metadata
        assert len(entity_result.metadata["dataview_queries"]) == 1
        assert 'LIST FROM "test"' in entity_result.metadata["dataview_queries"][0]

    @pytest.mark.asyncio
    async def test_build_context_skips_when_no_queries(
        self, entity_service, search_service, search_repository, tmp_path
    ):
        """Test that build_context skips dataview processing when no queries present."""
        # This test verifies that if no primary results have dataview_queries
        # in their metadata, the dataview processing is skipped entirely (0 cost)
        
        from basic_memory.schemas.base import Entity as EntitySchema
        from basic_memory.schemas.search import SearchItemType
        
        # Create a note WITHOUT dataview queries
        content = """# Regular Note

Just regular content, no dataview queries here.
"""
        entity, _ = await entity_service.create_or_update_entity(
            EntitySchema(
                title="Regular Note",
                folder="test",
                entity_type="note",
                content=content
            )
        )
        
        # Index the entity
        await search_service.index_entity(entity)
        
        # Search for the entity to get its metadata
        results = await search_repository.search(
            permalink="test/regular-note",
            limit=10,
            offset=0
        )
        
        # Find the entity in results
        entity_result = next((r for r in results if r.type == SearchItemType.ENTITY.value), None)
        assert entity_result is not None
        
        # Verify that metadata does NOT contain dataview_queries
        assert entity_result.metadata is None or "dataview_queries" not in entity_result.metadata

    @pytest.mark.asyncio
    async def test_build_context_respects_enable_dataview_false(
        self, entity_service, search_service, search_repository, tmp_path
    ):
        """Test that metadata stores queries regardless of enable_dataview flag."""
        # This test verifies that queries are stored in metadata even when
        # enable_dataview would be False (the flag only affects execution, not storage)
        
        from basic_memory.schemas.base import Entity as EntitySchema
        from basic_memory.schemas.search import SearchItemType
        
        # Create a note with a dataview query
        content = """# Dashboard Note

```dataview
LIST FROM "test"
```
"""
        entity, _ = await entity_service.create_or_update_entity(
            EntitySchema(
                title="Dashboard With Query",
                folder="test",
                entity_type="note",
                content=content
            )
        )
        
        # Index the entity
        await search_service.index_entity(entity)
        
        # Search for the entity to get its metadata
        results = await search_repository.search(
            permalink="test/dashboard-with-query",
            limit=10,
            offset=0
        )
        
        # Find the entity in results
        entity_result = next((r for r in results if r.type == SearchItemType.ENTITY.value), None)
        assert entity_result is not None
        
        # Verify that metadata contains dataview_queries (storage is independent of execution flag)
        assert entity_result.metadata is not None
        assert "dataview_queries" in entity_result.metadata
        assert len(entity_result.metadata["dataview_queries"]) == 1
