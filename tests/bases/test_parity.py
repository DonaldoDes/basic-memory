"""US-006 — Bases vs Dataview parity on a stratified corpus (BASES-17/18/24).

For each sampled DQL block, its Bases YAML equivalent must render a
byte-identical ``result_markdown`` against the same entity dataset. Parity is
structurally guaranteed (both reuse ResultFormatter) and proven empirically
here.

Stratification (ADR-003 §5 / US-006):
  - Clauses : FROM / FROM+WHERE / FROM+WHERE+SORT / FROM+WHERE+SORT+LIMIT
  - Functions (WHERE) : contains / length / lower / upper
  - Views : TABLE / LIST
  - Fields : file.* / direct frontmatter
"""

import pytest

from basic_memory.bases.integration import create_bases_integration
from basic_memory.dataview.integration import create_dataview_integration


def _corpus_notes():
    """A small but representative entity dataset (nested structure)."""
    return [
        {
            "title": "Project Alpha",
            "file": {"path": "projects/Project Alpha.md", "name": "Project Alpha", "folder": "projects"},
            "created_at": "2026-01-01",
            "updated_at": "2026-01-10",
            "frontmatter": {"type": "project", "status": "Active", "priority": 1, "tags": ["project", "dev"]},
        },
        {
            "title": "Project Beta",
            "file": {"path": "projects/Project Beta.md", "name": "Project Beta", "folder": "projects"},
            "created_at": "2026-01-05",
            "updated_at": "2026-01-11",
            "frontmatter": {"type": "project", "status": "Archived", "priority": 2, "tags": ["project"]},
        },
        {
            "title": "Project Gamma",
            "file": {"path": "projects/Project Gamma.md", "name": "Project Gamma", "folder": "projects"},
            "created_at": "2026-01-03",
            "updated_at": "2026-01-09",
            "frontmatter": {"type": "project", "status": "Active", "priority": 3, "tags": ["project", "dev", "urgent"]},
        },
        {
            "title": "Area Dev",
            "file": {"path": "areas/Area Dev.md", "name": "Area Dev", "folder": "areas"},
            "created_at": "2026-01-01",
            "updated_at": "2026-01-12",
            "frontmatter": {"type": "area", "status": "Active", "priority": 1, "tags": ["area"]},
        },
    ]


def _render_dataview(dql: str) -> str:
    integ = create_dataview_integration(notes_provider=_corpus_notes)
    block = f"```dataview\n{dql}\n```"
    results = integ.process_note(block)
    assert results and results[0]["status"] == "success", results
    return results[0]["result_markdown"]


def _render_bases(yaml_body: str) -> str:
    integ = create_bases_integration(notes_provider=_corpus_notes)
    block = f"```base\n{yaml_body}\n```"
    results = integ.process_note(block)
    assert results and results[0]["status"] == "success", results
    return results[0]["result_markdown"]


# Stratified corpus: (id, dql, bases_yaml). Each pair must render identically.
PARITY_CORPUS = [
    # --- LIST, FROM only -----------------------------------------------------
    (
        "list_from",
        'LIST FROM "projects"',
        'filters: file.inFolder("projects")\nviews:\n  - type: list',
    ),
    # --- LIST, FROM + WHERE (equality on frontmatter) ------------------------
    (
        "list_from_where_eq",
        'LIST FROM "projects" WHERE status = "Active"',
        'filters:\n  and:\n    - file.inFolder("projects")\n    - status == "Active"\nviews:\n  - type: list',
    ),
    # --- TABLE, FROM + WHERE (file.* + frontmatter columns) ------------------
    (
        "table_from_where",
        'TABLE status, priority FROM "projects" WHERE status = "Active"',
        'filters:\n  and:\n    - file.inFolder("projects")\n    - status == "Active"\nviews:\n  - type: table\n    order: [status, priority]',
    ),
    # --- TABLE, FROM + WHERE + SORT ------------------------------------------
    (
        "table_sort",
        'TABLE priority FROM "projects" SORT priority DESC',
        'filters: file.inFolder("projects")\nviews:\n  - type: table\n    order: [priority]\n    sort:\n      - property: priority\n        direction: DESC',
    ),
    # --- TABLE, FROM + WHERE + SORT + LIMIT ----------------------------------
    (
        "table_sort_limit",
        'TABLE priority FROM "projects" SORT priority ASC LIMIT 2',
        'filters: file.inFolder("projects")\nviews:\n  - type: table\n    order: [priority]\n    sort:\n      - property: priority\n        direction: ASC\n    limit: 2',
    ),
    # --- WHERE contains() (dominant strata) ----------------------------------
    (
        "list_contains",
        'LIST FROM "projects" WHERE contains(tags, "dev")',
        'filters:\n  and:\n    - file.inFolder("projects")\n    - contains(tags, "dev")\nviews:\n  - type: list',
    ),
    # --- WHERE length() ------------------------------------------------------
    (
        "table_length",
        'TABLE priority FROM "projects" WHERE length(tags) > 1',
        'filters:\n  and:\n    - file.inFolder("projects")\n    - length(tags) > 1\nviews:\n  - type: table\n    order: [priority]',
    ),
    # --- WHERE lower() -------------------------------------------------------
    (
        "list_lower",
        'LIST FROM "projects" WHERE lower(status) = "active"',
        'filters:\n  and:\n    - file.inFolder("projects")\n    - lower(status) == "active"\nviews:\n  - type: list',
    ),
    # --- WHERE upper() -------------------------------------------------------
    (
        "list_upper",
        'LIST FROM "projects" WHERE upper(status) = "ARCHIVED"',
        'filters:\n  and:\n    - file.inFolder("projects")\n    - upper(status) == "ARCHIVED"\nviews:\n  - type: list',
    ),
    # --- TABLE on file.* field column ----------------------------------------
    (
        "table_file_field",
        'TABLE file.folder FROM "projects"',
        'filters: file.inFolder("projects")\nviews:\n  - type: table\n    order: [file.folder]',
    ),
]


@pytest.mark.parametrize("test_id,dql,yaml_body", PARITY_CORPUS, ids=[c[0] for c in PARITY_CORPUS])
def test_parity_byte_identical(test_id, dql, yaml_body):
    dv = _render_dataview(dql)
    bz = _render_bases(yaml_body)
    assert bz == dv, f"[{test_id}] parity mismatch:\n--dataview--\n{dv}\n--bases--\n{bz}"


@pytest.mark.smoke
@pytest.mark.parametrize(
    "test_id,dql,yaml_body",
    [c for c in PARITY_CORPUS if c[0] in {"list_from", "table_from_where", "list_contains"}],
    ids=lambda c: c if isinstance(c, str) else None,
)
def test_parity_smoke(test_id, dql, yaml_body):
    # BASES-24 smoke subset
    assert _render_bases(yaml_body) == _render_dataview(dql)
