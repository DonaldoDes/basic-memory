"""Live-path integration tests for US-3 (M-Bases-P4, ADR-006 §Gap #5).

BP4-C-05 / BP4-C-06. These tests go through the REAL MCP render path
(``BasesIntegration.process_note`` → ``BasesExecutor`` via a provider that mirrors
``knowledge_router.list_entities_for_dataview``), with a dataset built by the
canonical ``build_dataview_dataset`` builder shared with the other live-path
tests. Per ADR-006 §"Stratégie de vérification" (normative), no synthetic dataset
divergent from the provider is used — a hand-fabricated dict that bypasses the
provider's outlink grouping does NOT count for AC.

Two real vault constructs (areas.md ×3 / Basic Memory index ×3 families) are
exercised on the live path:
  - ``any(file.outlinks, l => l.startsWith("areas/"))`` as a WHERE filter
    (predicate iterator), and ``filter(...)`` returning a sub-list.
  - ``FLATTEN file.links AS link`` (a COMPUTED list expression) unfolding the
    row's outlinks into one row per element.
"""

from __future__ import annotations

from basic_memory.bases.integration import create_bases_integration
from tests.bases.test_live_path_integration import _Entity, _Rel, build_dataview_dataset


def _block(body: str) -> str:
    return f"```base\n{body}```"


def _run(body: str, entities: list[_Entity]) -> dict:
    integ = create_bases_integration(notes_provider=lambda: build_dataview_dataset(entities))
    results = integ.process_note(_block(body))
    assert len(results) == 1
    return results[0]


def _area_entities() -> list[_Entity]:
    """Notes whose outlinks target areas/* (and one that does not)."""
    return [
        _Entity(
            title="In Area One",
            file_path="notes/one.md",
            permalink="notes/one",
            outgoing_relations=[
                _Rel(to_name="Area X", to_permalink="areas/x", relation_type="part_of")
            ],
        ),
        _Entity(
            title="In Area Two",
            file_path="notes/two.md",
            permalink="notes/two",
            outgoing_relations=[
                _Rel(to_name="Area Y", to_permalink="areas/y", relation_type="part_of")
            ],
        ),
        _Entity(
            title="No Area",
            file_path="notes/three.md",
            permalink="notes/three",
            outgoing_relations=[
                _Rel(to_name="Project Z", to_permalink="projects/z", relation_type="part_of")
            ],
        ),
    ]


# ===========================================================================
# BP4-C-05 — areas.md family: any()/filter() predicate over file.outlinks (live)
# ===========================================================================
class TestAreasIteratorLivePath:
    def test_any_outlinks_in_areas_filters_rows(self):
        """BP4-C-05: WHERE any(file.outlinks, l => l.startsWith("areas/")) keeps
        only notes that link an area — the areas.md membership pattern, live."""
        body = 'filters: any(file.outlinks, l => l.startsWith("areas/"))\nviews:\n  - type: list\n'
        r = _run(body, _area_entities())
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 2
        assert {row.get("title") for row in r["results"]} == {"In Area One", "In Area Two"}

    def test_filter_outlinks_length_as_calculated_column(self):
        """BP4-C-05: a calculated column length(filter(file.outlinks, l =>
        l.startsWith("areas/"))) renders the count of area links per row."""
        body = (
            'filters: file.inFolder("notes")\n'
            "formulas:\n"
            '  AreaLinks: length(filter(file.outlinks, l => l.startsWith("areas/")))\n'
            "views:\n"
            "  - type: table\n"
            "    order:\n"
            "      - file.name\n"
            "      - formula.AreaLinks\n"
        )
        r = _run(body, _area_entities())
        assert r["status"] == "success", r.get("error")
        by_title = {row.get("title"): row.get("AreaLinks") for row in r["results"]}
        assert by_title["In Area One"] == 1
        assert by_title["In Area Two"] == 1
        assert by_title["No Area"] == 0


# ===========================================================================
# BP4-C-06 — Basic Memory index family: FLATTEN file.links AS link (live)
# ===========================================================================
class TestIndexFlattenExpressionLivePath:
    def test_flatten_file_links_expands_one_row_per_link(self):
        """BP4-C-06: FLATTEN file.links AS link unfolds a COMPUTED list (the row's
        outlinks) into one row per element on the real render path."""
        entities = [
            _Entity(
                title="Hub",
                file_path="index/hub.md",
                permalink="index/hub",
                outgoing_relations=[
                    _Rel(to_name="Target A", to_permalink="t/a", relation_type="links_to"),
                    _Rel(to_name="Target B", to_permalink="t/b", relation_type="links_to"),
                ],
            ),
        ]
        body = (
            'filters: file.inFolder("index")\n'
            "views:\n"
            "  - type: table\n"
            "    flatten:\n"
            "      expression: file.links\n"
            "      as: link\n"
            "    order:\n"
            "      - file.name\n"
            "      - link\n"
        )
        r = _run(body, entities)
        assert r["status"] == "success", r.get("error")
        # The single hub row, carrying 4 outlinks (permalink + to_name per rel),
        # unfolds to one row per link element — cells non-empty.
        assert r["result_count"] >= 2
        links = [row.get("link") for row in r["results"]]
        assert all(link for link in links)  # no empty cells (the silent trap)
        assert "t/a" in links and "t/b" in links
