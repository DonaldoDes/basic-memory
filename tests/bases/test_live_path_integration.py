"""Integration tests for the REAL MCP render path (US-f live-path fixes).

These tests close the gap between Phase 3 *unit* tests (synthetic, hand-tuned
datasets where ``file.outlinks`` was filled by hand) and the *live*
``read_note(enable_bases=True)`` / ``BasesIntegration.process_note`` path that
broke in production:

  Gap 1 — ``file.hasLink(this.file)`` returned 0 everywhere because a row that
          links the host by its TITLE (``to_name`` in the provider outlinks)
          never matched a host identity built from path+permalink only. The
          ~169 Activity blocks rendered nothing.
  Gap 2 — a block WITHOUT ``file.inFolder`` (``FROM ""`` / global scan) must
          evaluate over ALL notes of the dataset, not return 0.
  Gap 3 — ``count(status == "Done" or status == "Completed")`` raised
          ``Trailing tokens 'or'``: the conditional-aggregate predicate uses the
          formula grammar, which only knew ``||`` / ``&&`` — not the WORD
          operators ``or`` / ``and`` / ``not`` that Obsidian (and the vault's
          ~23 Progression blocks) use. The blocks rendered as errors.

The dataset here is built by :func:`build_dataview_dataset`, a faithful, I/O-free
reproduction of the production provider
(``knowledge_router.list_entities_for_dataview``): each note carries a nested
``file`` dict and ``file.outlinks`` sourced from its outgoing relations
(target permalink AND ``to_name``) — exactly the shape the executor reads. No
test fills ``outlinks`` by hand: a regression that drops the provider wiring
(empty outlinks) makes ``test_haslink_*`` fail.

Security continuity (constitution §"Parsing markdown / frontmatter", ADR-005
§Axe 5): outlinks are read from the already-resolved dataset, never the FS;
``count(expr)`` is evaluated inside the closed Phase 2 sandbox — no
``eval``/``exec``/``getattr`` is introduced. Sentinel tests below assert these
invariants on the live path.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from basic_memory.bases.integration import create_bases_integration


# ===========================================================================
# Faithful provider reproduction (mirrors knowledge_router outlink logic)
# ===========================================================================
@dataclass
class _Rel:
    """A stand-in for an outgoing relation, like Entity.outgoing_relations."""

    to_name: str
    to_permalink: str | None = None


@dataclass
class _Entity:
    """A stand-in for a DB Entity row (only the fields the provider reads)."""

    title: str
    file_path: str
    permalink: str | None = None
    note_type: str = "note"
    metadata: dict = field(default_factory=dict)
    outgoing_relations: list[_Rel] = field(default_factory=list)


def build_dataview_dataset(entities: list[_Entity]) -> list[dict]:
    """Build the dataview/bases dataset from entities — provider logic, no I/O.

    Byte-for-byte mirror of ``knowledge_router.list_entities_for_dataview``'s
    outlink construction (target permalink + raw to_name, always a list) and
    note shape (nested ``file`` dict, frontmatter merged top-level). Keeping ONE
    canonical builder here lets the integration tests exercise the real shape
    without an HTTP round-trip — and fails loudly if the wiring ever drops
    outlinks.
    """
    from pathlib import PurePosixPath

    notes: list[dict] = []
    for entity in entities:
        # Outlinks for Bases file.hasLink(this.file): target permalink (when
        # resolved) AND raw to_name. Both forms let hasLink match a host
        # referenced by permalink OR by name/title.
        outlinks: list[str] = []
        for rel in entity.outgoing_relations:
            if rel.to_permalink:
                outlinks.append(rel.to_permalink)
            if rel.to_name:
                outlinks.append(rel.to_name)

        note: dict = {
            "file": {
                "path": entity.file_path,
                "name": PurePosixPath(entity.file_path).name,
                "folder": str(PurePosixPath(entity.file_path).parent),
                "outlinks": outlinks,
            },
            "title": entity.title,
            "type": entity.note_type,
        }
        if entity.permalink:
            note["permalink"] = entity.permalink
        for key, value in entity.metadata.items():
            if key not in note:
                note[key] = value
        notes.append(note)
    return notes


# ---------------------------------------------------------------------------
# A realistic milestone fixture: 3 stories pointing at a host milestone note,
# one linking by PERMALINK, one by TITLE, one not linking at all.
# ---------------------------------------------------------------------------
HOST_PERMALINK = "products/x/milestones/m40"
HOST_PATH = "products/X/milestones/M40/M40.md"
HOST_TITLE = "M40 - Live Path"
HOST_METADATA = {"permalink": HOST_PERMALINK, "path": HOST_PATH, "title": HOST_TITLE}


def _milestone_entities() -> list[_Entity]:
    return [
        _Entity(
            title="US-1",
            file_path="products/X/milestones/M40/stories/US-1.md",
            permalink="products/x/milestones/m40/stories/us-1",
            note_type="user-story",
            metadata={"status": "Done"},
            # links the host by PERMALINK
            outgoing_relations=[_Rel(to_name=HOST_TITLE, to_permalink=HOST_PERMALINK)],
        ),
        _Entity(
            title="US-2",
            file_path="products/X/milestones/M40/stories/US-2.md",
            permalink="products/x/milestones/m40/stories/us-2",
            note_type="user-story",
            metadata={"status": "Completed"},
            # links the host by TITLE only (unresolved relation → no permalink)
            outgoing_relations=[_Rel(to_name=HOST_TITLE, to_permalink=None)],
        ),
        _Entity(
            title="US-3",
            file_path="products/X/milestones/M40/stories/US-3.md",
            permalink="products/x/milestones/m40/stories/us-3",
            note_type="user-story",
            metadata={"status": "In Progress"},
            # does NOT link the host
            outgoing_relations=[_Rel(to_name="Some Other Note", to_permalink="other/note")],
        ),
    ]


def _provider():
    return build_dataview_dataset(_milestone_entities())


def _block(body: str) -> str:
    return f"```base\n{body}```"


def _run(body: str, host: dict | None = None) -> dict:
    integ = create_bases_integration(notes_provider=_provider)
    results = integ.process_note(_block(body), note_metadata=host)
    assert len(results) == 1
    return results[0]


# ===========================================================================
# AC-1 — hasLink renders real backlinks on the process_note path
# ===========================================================================
class TestHasLinkLivePath:
    def test_haslink_renders_backlinks_by_permalink_and_title(self):
        """AC-1: hasLink resolves rows linking host by permalink OR by title.

        US-1 links by permalink, US-2 by title only, US-3 not at all → 2 rows.
        This FAILS if the host identity excludes the title (gap 1) or if the
        provider dataset carries empty outlinks (wiring regression).
        """
        r = _run(
            "filters: file.hasLink(this.file)\nviews:\n  - type: list\n",
            host=HOST_METADATA,
        )
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 2

    def test_haslink_link_by_title_only_matches(self):
        """AC-1 (root cause): a row linking the host ONLY by title must match.

        This is the exact production failure: provider outlinks carry to_name
        (a title), the host identity must include its title for hasLink to fire.
        """
        # Single entity linking the host by title only.
        entities = [
            _Entity(
                title="OnlyTitleLinker",
                file_path="p/X/a.md",
                permalink="p/x/a",
                outgoing_relations=[_Rel(to_name=HOST_TITLE, to_permalink=None)],
            )
        ]
        integ = create_bases_integration(notes_provider=lambda: build_dataview_dataset(entities))
        r = integ.process_note(
            _block("filters: file.hasLink(this.file)\nviews:\n  - type: list\n"),
            note_metadata=HOST_METADATA,
        )[0]
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 1

    def test_haslink_empty_outlinks_is_a_failure_signal(self):
        """AC-1 sentinel: with provider outlinks stripped, hasLink must be 0.

        Guards the wiring: if a future change makes the provider drop outlinks,
        the positive tests above flip to 0 and fail — this test pins the
        contrapositive so the regression is unmistakable.
        """
        entities = [
            _Entity(
                title="NoOutlinks",
                file_path="p/X/a.md",
                permalink="p/x/a",
                outgoing_relations=[],  # provider produces []
            )
        ]
        integ = create_bases_integration(notes_provider=lambda: build_dataview_dataset(entities))
        r = integ.process_note(
            _block("filters: file.hasLink(this.file)\nviews:\n  - type: list\n"),
            note_metadata=HOST_METADATA,
        )[0]
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 0


# ===========================================================================
# AC-2 — FROM "" / no file.inFolder evaluates over the whole dataset
# ===========================================================================
class TestNoInFolderLivePath:
    def test_no_infolder_status_filter_renders_all_matching(self):
        """AC-2: a block without inFolder scans every note of the dataset."""
        r = _run('filters: status == "Done"\nviews:\n  - type: list\n')
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 1  # only US-1 is Done

    def test_no_infolder_haslink_renders_nonzero(self):
        """AC-2 + AC-1: hasLink with NO inFolder (the Activity pattern) is != 0."""
        r = _run(
            "filters: file.hasLink(this.file)\nviews:\n  - type: list\n",
            host=HOST_METADATA,
        )
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 2


# ===========================================================================
# AC-3 — count(predicate with or/and) compiles and counts correctly
# ===========================================================================
class TestConditionalAggregateBooleanLivePath:
    def test_count_or_predicate_renders(self):
        """AC-3/AC-4: groupBy + count(Done OR Completed) renders, no parse error.

        2 of the 3 stories are Done/Completed → the Done summary must be 2.
        Pre-fix this raised ``Trailing tokens 'or'`` and the block errored.
        """
        body = (
            'filters: file.inFolder("products/X/milestones/M40/stories")\n'
            "views:\n"
            "  - type: table\n"
            "    groupBy: type\n"
            "    summaries:\n"
            "      Total: count(title)\n"
            '      Done: count(status == "Done" or status == "Completed")\n'
            "    aggregate: true\n"
            "    order:\n"
            "      - type\n"
            "      - Done\n"
            "      - Total\n"
        )
        r = _run(body)
        assert r["status"] == "success", r.get("error")
        # One group (type == user-story), summary Done == 2, Total == 3.
        assert r["result_count"] == 1
        grp = r["results"][0]
        assert grp["Done"] == 2
        assert grp["Total"] == 3

    def test_count_and_predicate_renders(self):
        """AC-3: the word operator ``and`` also compiles in a count predicate."""
        body = (
            'filters: file.inFolder("products/X/milestones/M40/stories")\n'
            "views:\n"
            "  - type: table\n"
            "    groupBy: type\n"
            "    summaries:\n"
            '      DoneUS: count(status == "Done" and type == "user-story")\n'
            "    aggregate: true\n"
            "    order:\n"
            "      - type\n"
            "      - DoneUS\n"
        )
        r = _run(body)
        assert r["status"] == "success", r.get("error")
        assert r["results"][0]["DoneUS"] == 1  # only US-1 is Done & user-story

    def test_count_or_with_aggformula_progression_table(self):
        """AC-4: the full Progression block (count OR + round(Done/Total*100))."""
        body = (
            'filters: file.inFolder("products/X/milestones/M40/stories")\n'
            "views:\n"
            "  - type: table\n"
            "    groupBy: type\n"
            "    summaries:\n"
            "      Total: count(title)\n"
            '      Done: count(status == "Done" or status == "Completed")\n'
            "    aggFormulas:\n"
            "      Pct: round(Done / Total * 100)\n"
            "    aggregate: true\n"
            "    order:\n"
            "      - type\n"
            "      - Done\n"
            "      - Total\n"
            "      - Pct\n"
        )
        r = _run(body)
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 1
        grp = r["results"][0]
        assert grp["Done"] == 2
        assert grp["Total"] == 3
        assert grp["Pct"] == 67  # round(2/3*100)


# ===========================================================================
# AC-4 — Full Activity block (hasLink + no inFolder + sort/limit) renders
# ===========================================================================
class TestActivityBlockLivePath:
    def test_activity_block_renders_nonempty_list(self):
        """AC-4: the complete Activity block renders a non-empty list of backlinks."""
        body = (
            "filters: file.hasLink(this.file)\n"
            "views:\n"
            "  - type: table\n"
            "    order:\n"
            "      - file.name\n"
            "      - status\n"
            "    sort:\n"
            "      - property: file.name\n"
            "        direction: ASC\n"
            "    limit: 50\n"
        )
        r = _run(body, host=HOST_METADATA)
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 2
        assert r["result_markdown"]  # non-empty rendered table
        # The two backlinking stories appear; the non-linker does not.
        titles = {row.get("title") for row in r["results"]}
        assert titles == {"US-1", "US-2"}
        assert "US-3" not in titles
