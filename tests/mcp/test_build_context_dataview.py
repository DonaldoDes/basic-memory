"""Tests for build_context Dataview integration.

These tests verify that build_context properly provides notes to the Dataview
integration for query execution.
"""

import pytest
from unittest.mock import patch, MagicMock

from basic_memory.mcp.tools import build_context
from basic_memory.schemas.memory import GraphContext


@pytest.mark.asyncio
async def test_build_context_dataview_receives_notes_provider(client, test_graph, test_project):
    """Test that build_context passes a notes_provider to DataviewIntegration.
    
    This is the core test for the bug fix: build_context was calling
    create_dataview_integration() without a notes_provider, causing
    Dataview queries to return 0 results.
    """
    # Create a note with a dataview query so that create_dataview_integration is called
    from basic_memory.mcp.tools import write_note
    await write_note.fn(
        project=test_project.name,
        title="Root",
        folder="test",
        content="""# Root

```dataview
LIST FROM "test"
```
"""
    )
    
    with patch('basic_memory.mcp.tools.build_context.create_dataview_integration') as mock_create:
        # Setup mock to return a mock integration
        mock_integration = MagicMock()
        mock_integration._execute_query.return_value = {
            'status': 'success',
            'result_markdown': '- [[Test]]'
        }
        mock_create.return_value = mock_integration
        
        # Call build_context with dataview enabled
        await build_context.fn(
            project=test_project.name,
            url="memory://test/root",
            enable_dataview=True
        )
        
        # Verify create_dataview_integration was called with a notes_provider
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args
        
        # The notes_provider should be passed as a keyword argument or positional
        if call_kwargs.kwargs:
            assert 'notes_provider' in call_kwargs.kwargs
            notes_provider = call_kwargs.kwargs['notes_provider']
        else:
            # Positional argument
            assert len(call_kwargs.args) > 0
            notes_provider = call_kwargs.args[0]
        
        # notes_provider should be callable
        assert callable(notes_provider)
        
        # notes_provider should return a list of notes
        notes = notes_provider()
        assert isinstance(notes, list)


@pytest.mark.asyncio
async def test_build_context_dataview_notes_have_required_fields(client, test_graph, test_project):
    """Test that notes provided to Dataview have the required fields.
    
    Dataview expects notes with specific fields:
    - file.path, file.name, file.folder
    - title
    - type (entity_type)
    - permalink (optional)
    - frontmatter fields (optional)
    """
    # Create a note with a dataview query so that create_dataview_integration is called
    from basic_memory.mcp.tools import write_note
    await write_note.fn(
        project=test_project.name,
        title="Root",
        folder="test",
        content="""# Root

```dataview
LIST FROM "test"
```
"""
    )
    
    captured_notes = []
    
    def capture_notes_provider(notes_provider=None):
        """Capture the notes_provider and return a mock integration."""
        if notes_provider:
            captured_notes.extend(notes_provider())
        mock_integration = MagicMock()
        mock_integration._execute_query.return_value = {
            'status': 'success',
            'result_markdown': '- [[Test]]'
        }
        return mock_integration
    
    with patch('basic_memory.mcp.tools.build_context.create_dataview_integration', 
               side_effect=capture_notes_provider):
        await build_context.fn(
            project=test_project.name,
            url="memory://test/root",
            enable_dataview=True
        )
    
    # Should have captured some notes (from test_graph fixture)
    assert len(captured_notes) > 0, "No notes were provided to Dataview"
    
    # Verify each note has required fields
    for note in captured_notes:
        # file object with path, name, folder
        assert 'file' in note, f"Note missing 'file' field: {note}"
        assert 'path' in note['file'], f"Note missing 'file.path': {note}"
        assert 'name' in note['file'], f"Note missing 'file.name': {note}"
        assert 'folder' in note['file'], f"Note missing 'file.folder': {note}"
        
        # title is required
        assert 'title' in note, f"Note missing 'title': {note}"
        
        # type (entity_type) is required
        assert 'type' in note, f"Note missing 'type': {note}"


@pytest.mark.asyncio
async def test_build_context_dataview_disabled_no_notes_fetch(client, test_graph, test_project):
    """Test that when enable_dataview=False, no notes are fetched."""
    with patch('basic_memory.mcp.tools.build_context.create_dataview_integration') as mock_create:
        # Call build_context with dataview disabled
        await build_context.fn(
            project=test_project.name,
            url="memory://test/root",
            enable_dataview=False
        )
        
        # create_dataview_integration should not be called
        mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_build_context_dataview_notes_count_matches_entities(
    client, test_graph, test_project, entity_repository
):
    """Test that the number of notes matches the number of entities in the project."""
    # Create a note with a dataview query so that create_dataview_integration is called
    from basic_memory.mcp.tools import write_note
    await write_note.fn(
        project=test_project.name,
        title="Root",
        folder="test",
        content="""# Root

```dataview
LIST FROM "test"
```
"""
    )
    
    captured_notes = []
    
    def capture_notes_provider(notes_provider=None):
        if notes_provider:
            captured_notes.extend(notes_provider())
        mock_integration = MagicMock()
        mock_integration._execute_query.return_value = {
            'status': 'success',
            'result_markdown': '- [[Test]]'
        }
        return mock_integration
    
    with patch('basic_memory.mcp.tools.build_context.create_dataview_integration',
               side_effect=capture_notes_provider):
        await build_context.fn(
            project=test_project.name,
            url="memory://test/root",
            enable_dataview=True
        )
    
    # Get actual entity count from repository
    all_entities = await entity_repository.find_all()
    
    # Notes count should match entity count
    assert len(captured_notes) == len(all_entities), \
        f"Expected {len(all_entities)} notes, got {len(captured_notes)}"


@pytest.mark.asyncio
async def test_build_context_dataview_empty_results_still_provides_notes(client, test_graph, test_project):
    """Test that even when build_context returns no results, notes are still provided to Dataview."""
    # Create a note with a dataview query so that create_dataview_integration is called
    from basic_memory.mcp.tools import write_note
    await write_note.fn(
        project=test_project.name,
        title="NoteWithQuery",
        folder="test",
        content="""# NoteWithQuery

```dataview
LIST FROM "test"
```
"""
    )
    
    captured_notes = []
    
    def capture_notes_provider(notes_provider=None):
        if notes_provider:
            captured_notes.extend(notes_provider())
        mock_integration = MagicMock()
        mock_integration._execute_query.return_value = {
            'status': 'success',
            'result_markdown': '- [[Test]]'
        }
        return mock_integration
    
    with patch('basic_memory.mcp.tools.build_context.create_dataview_integration',
               side_effect=capture_notes_provider):
        # Query for the note we just created - should return results
        result = await build_context.fn(
            project=test_project.name,
            url="memory://test/note-with-query",
            enable_dataview=True
        )
    
    # Should have results now (the note we created)
    assert len(result.results) > 0
    
    # Notes should be provided for Dataview queries
    assert len(captured_notes) > 0, "Notes should be provided for Dataview queries"


@pytest.mark.asyncio
async def test_build_context_dataview_results_markdown_included(client, test_graph, test_project):
    """Test that Dataview query result_markdown is included in content, not just a summary.
    
    This is the core test for the bug fix: build_context was only adding a summary
    like "Dataview: 3 queries executed" instead of including the actual result_markdown.
    """
    # Create a note with a dataview query so that create_dataview_integration is called
    from basic_memory.mcp.tools import write_note
    await write_note.fn(
        project=test_project.name,
        title="Root",
        folder="test",
        content="""# Root

```dataview
TABLE status FROM "test"
```
"""
    )
    
    def mock_create_integration(notes_provider=None):
        """Mock integration that returns results with markdown."""
        mock_integration = MagicMock()
        mock_integration.execute_raw_query.return_value = {
            'query_id': 1,
            'line_number': 10,
            'query_type': 'TABLE',
            'status': 'success',
            'execution_time_ms': 15,
            'result_count': 3,
            'result_markdown': '| Title | Status |\n|-------|--------|\n| US-001 | Done |\n| US-002 | In Progress |\n| US-003 | Ready |',
            'discovered_links': []
        }
        return mock_integration
    
    with patch('basic_memory.mcp.tools.build_context.create_dataview_integration',
               side_effect=mock_create_integration):
        result = await build_context.fn(
            project=test_project.name,
            url="memory://test/root",
            enable_dataview=True
        )
    
    # Should have results
    assert len(result.results) > 0
    
    # Get the primary result content
    primary_content = result.results[0].primary_result.content
    
    # Verify the result_markdown is included, not just a summary
    assert '| Title | Status |' in primary_content, "Dataview table header not found"
    assert '| US-001 | Done |' in primary_content, "Dataview table row not found"
    assert '| US-002 | In Progress |' in primary_content, "Dataview table row not found"
    assert '| US-003 | Ready |' in primary_content, "Dataview table row not found"
    
    # Verify it's in a proper section
    assert '## Dataview Query Results' in primary_content, "Dataview section header not found"


@pytest.mark.asyncio
async def test_build_context_dataview_multiple_queries_all_included(client, test_graph, test_project):
    """Test that multiple Dataview queries all have their result_markdown included."""
    # Create a note with multiple dataview queries so that create_dataview_integration is called
    from basic_memory.mcp.tools import write_note
    await write_note.fn(
        project=test_project.name,
        title="Root",
        folder="test",
        content="""# Root

```dataview
TABLE status FROM "test"
```

```dataview
LIST FROM "test" WHERE type = "bug"
```
"""
    )
    
    def mock_create_integration(notes_provider=None):
        """Mock integration that returns multiple query results."""
        mock_integration = MagicMock()
        # Mock execute_raw_query to return different results for each query
        def execute_query_side_effect(query_text, query_id):
            if 'TABLE' in query_text:
                return {
                    'query_id': query_id,
                    'line_number': 0,
                    'query_type': 'TABLE',
                    'status': 'success',
                    'execution_time_ms': 15,
                    'result_count': 2,
                    'result_markdown': '| Title | Status |\n|-------|--------|\n| US-001 | Done |',
                    'discovered_links': []
                }
            else:
                return {
                    'query_id': query_id,
                    'line_number': 0,
                    'query_type': 'LIST',
                    'status': 'success',
                    'execution_time_ms': 10,
                    'result_count': 3,
                    'result_markdown': '- [[Bug-001]]\n- [[Bug-002]]\n- [[Bug-003]]',
                    'discovered_links': []
                }
        mock_integration.execute_raw_query.side_effect = execute_query_side_effect
        return mock_integration
    
    with patch('basic_memory.mcp.tools.build_context.create_dataview_integration',
               side_effect=mock_create_integration):
        result = await build_context.fn(
            project=test_project.name,
            url="memory://test/root",
            enable_dataview=True
        )
    
    # Should have results
    assert len(result.results) > 0
    
    # Get the primary result content
    primary_content = result.results[0].primary_result.content
    
    # Verify both query results are included
    assert '| US-001 | Done |' in primary_content, "First query result not found"
    assert '- [[Bug-001]]' in primary_content, "Second query result not found"
    assert '- [[Bug-002]]' in primary_content, "Second query result not found"
    assert '- [[Bug-003]]' in primary_content, "Second query result not found"


@pytest.mark.asyncio
async def test_build_context_dataview_failed_query_not_included(client, test_graph, test_project):
    """Test that failed Dataview queries don't add empty sections."""
    # Create a note with a dataview query so that create_dataview_integration is called
    from basic_memory.mcp.tools import write_note
    await write_note.fn(
        project=test_project.name,
        title="Root",
        folder="test",
        content="""# Root

```dataview
TABLE invalid syntax
```
"""
    )
    
    def mock_create_integration(notes_provider=None):
        """Mock integration that returns a failed query."""
        mock_integration = MagicMock()
        mock_integration._execute_query.return_value = {
            'query_id': 1,
            'line_number': 10,
            'query_type': 'TABLE',
            'status': 'error',
            'execution_time_ms': 5,
            'result_count': 0,
            'error': 'Invalid syntax',
            'discovered_links': []
        }
        return mock_integration
    
    with patch('basic_memory.mcp.tools.build_context.create_dataview_integration',
               side_effect=mock_create_integration):
        result = await build_context.fn(
            project=test_project.name,
            url="memory://test/root",
            enable_dataview=True
        )
    
    # Should have results
    assert len(result.results) > 0
    
    # Get the primary result content
    primary_content = result.results[0].primary_result.content
    
    # Failed queries should not add markdown sections
    # (only successful queries with result_markdown should be included)
    assert '## Dataview Query Results' not in primary_content or \
           primary_content.count('## Dataview Query Results') == 0 or \
           'Invalid syntax' not in primary_content
