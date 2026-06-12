"""Pytest fixtures for Bases tests.

Notes use the nested structure returned by list_entities_for_dataview:
    {"file": {"path": ..., "name": ..., "folder": ...}, "frontmatter": {...},
     "title": ..., "created_at": ..., "updated_at": ...}
"""

import pytest


@pytest.fixture
def sample_notes():
    return [
        {
            "title": "Project Alpha",
            "file": {
                "path": "projects/Project Alpha.md",
                "name": "Project Alpha",
                "folder": "projects",
            },
            "created_at": "2026-01-01",
            "updated_at": "2026-01-10",
            "frontmatter": {
                "type": "project",
                "status": "Active",
                "priority": 1,
                "tags": ["project", "dev"],
            },
        },
        {
            "title": "Project Beta",
            "file": {
                "path": "projects/Project Beta.md",
                "name": "Project Beta",
                "folder": "projects",
            },
            "created_at": "2026-01-05",
            "updated_at": "2026-01-11",
            "frontmatter": {
                "type": "project",
                "status": "Archived",
                "priority": 2,
                "tags": ["project"],
            },
        },
        {
            "title": "Area Dev",
            "file": {
                "path": "areas/Area Dev.md",
                "name": "Area Dev",
                "folder": "areas",
            },
            "created_at": "2026-01-01",
            "updated_at": "2026-01-12",
            "frontmatter": {
                "type": "area",
                "status": "Active",
                "priority": 3,
                "tags": ["area", "dev"],
            },
        },
    ]
