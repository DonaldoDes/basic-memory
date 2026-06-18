"""Live-path integration tests for US-1 (M-Bases-P4, Gap #1, centerpiece).

Body relations (``part_of``, ``member_of``, ``attendee``, …) live in the note
BODY (``## Relations\n- part_of [[X]]``), not the frontmatter. Before US-1 the
provider ``knowledge_router.list_entities_for_dataview`` exposed only the
frontmatter fields plus a flat ``file.outlinks`` (union of all relations). It did
NOT expose each relation TYPE as a named list-of-links key, so a filter
``part_of.contains(this.file)`` resolved ``part_of`` to ``None`` (key absent) and
rendered 0 — the production failure on the ~24 structural blocks (areas/*,
org-relation nodes).

US-1 (ADR-006 §Gap #1, Option A):
  1. Provider — group ``outgoing_relations`` by ``relation_type`` and expose
     each type as a list-of-links key on the note dict (identities normalised
     consistently with ``_HostRef``: target permalink AND ``to_name``/title).
     ``file.outlinks`` (union) stays unchanged (additive).
  2. Executor — ``contains`` on a LIST-OF-LINKS receiver is membership
     identity-aware (the tested element against the host's ``_HostRef`` identity
     set), NOT a substring of the stringified list (NC-1). Substring ``contains``
     on a STRING is unchanged (non-regression).

These tests go through the REAL render path (``BasesIntegration.process_note`` →
``BasesExecutor`` via a provider that mirrors ``list_entities_for_dataview``).
The dataset is built by :func:`build_dataview_dataset` (the SAME canonical
builder as ``test_live_path_integration``, extended here with ``relation_type``
grouping) — a hand-fabricated dict that bypasses the provider grouping does NOT
count for AC (constitution / ADR-006 §"Stratégie de vérification").

Security continuity (ADR-002/003/004/005, ADR-006 §Invariants): relations are
read from the already-resolved dataset, never the FS; ``contains`` membership is
a closed dispatch — no ``eval``/``exec``/``getattr``/``__import__`` introduced.
"""

from __future__ import annotations

from basic_memory.bases.integration import create_bases_integration
from tests.bases.test_live_path_integration import (
    HOST_METADATA,
    HOST_PERMALINK,
    HOST_TITLE,
    _Entity,
    _Rel,
    build_dataview_dataset,
)


def _block(body: str) -> str:
    return f"```base\n{body}```"


# ---------------------------------------------------------------------------
# An Area host with child notes related by part_of, members by member_of, plus
# a noise note linking the host by a DIFFERENT relation type and a non-linker.
# ---------------------------------------------------------------------------
def _area_entities() -> list[_Entity]:
    return [
        _Entity(
            title="Child-A",
            file_path="areas/Area/Child-A.md",
            permalink="areas/area/child-a",
            metadata={"status": "Active"},
            # part_of the host, linked by PERMALINK
            outgoing_relations=[
                _Rel(to_name=HOST_TITLE, to_permalink=HOST_PERMALINK, relation_type="part_of")
            ],
        ),
        _Entity(
            title="Child-B",
            file_path="areas/Area/Child-B.md",
            permalink="areas/area/child-b",
            metadata={"status": "Active"},
            # part_of the host, linked by TITLE only (unresolved relation)
            outgoing_relations=[
                _Rel(to_name=HOST_TITLE, to_permalink=None, relation_type="part_of")
            ],
        ),
        _Entity(
            title="Member-1",
            file_path="areas/Area/Member-1.md",
            permalink="areas/area/member-1",
            # member_of the host (NOT part_of) — must answer member_of, not part_of
            outgoing_relations=[
                _Rel(to_name=HOST_TITLE, to_permalink=HOST_PERMALINK, relation_type="member_of")
            ],
        ),
        _Entity(
            title="Stranger",
            file_path="areas/Area/Stranger.md",
            permalink="areas/area/stranger",
            # part_of SOMETHING ELSE — never the host
            outgoing_relations=[
                _Rel(to_name="Other Area", to_permalink="areas/other", relation_type="part_of")
            ],
        ),
    ]


def _run(body: str, entities: list[_Entity], host: dict | None = None) -> dict:
    integ = create_bases_integration(
        notes_provider=lambda: build_dataview_dataset(entities)
    )
    results = integ.process_note(_block(body), note_metadata=host)
    assert len(results) == 1
    return results[0]


# ===========================================================================
# BP4-A-01 — part_of.contains(this.file) renders an Area's children (live)
# ===========================================================================
class TestPartOfContainsLivePath:
    def test_part_of_contains_renders_children_membership_exact(self):
        """BP4-A-01: part_of.contains(this.file) → the 2 part_of children only.

        Child-A links by permalink, Child-B by title; both are part_of the host.
        Member-1 is member_of (not part_of) and Stranger is part_of something
        else → neither matches. Membership is identity-exact, no substring.
        """
        r = _run(
            "filters: part_of.contains(this.file)\nviews:\n  - type: list\n",
            _area_entities(),
            host=HOST_METADATA,
        )
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 2
        titles = {row.get("title") for row in r["results"]}
        assert titles == {"Child-A", "Child-B"}


# ===========================================================================
# BP4-A-02 — member_of.contains(this.file) renders org-relation members (live)
# ===========================================================================
class TestMemberOfContainsLivePath:
    def test_member_of_contains_renders_members_only(self):
        """BP4-A-02: member_of.contains(this.file) → the member, not the children.

        Only Member-1 is member_of the host. The part_of children must NOT leak
        into the member_of key (the provider groups by relation_type).
        """
        r = _run(
            "filters: member_of.contains(this.file)\nviews:\n  - type: list\n",
            _area_entities(),
            host=HOST_METADATA,
        )
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 1
        assert {row.get("title") for row in r["results"]} == {"Member-1"}


# ===========================================================================
# BP4-A-05 — membership is identity-aware, NOT substring (false-positive guard)
# ===========================================================================
class TestMembershipNotSubstring:
    def test_permalink_prefix_does_not_false_match(self):
        """BP4-A-05: a permalink that is a PREFIX of the host's must NOT match.

        Substring semantics on the stringified list would falsely match a row
        whose relation target is a prefix of the host identity. Membership is
        exact, so only the exact-identity row matches.
        """
        host = {
            "path": "Host.md",
            "permalink": "areas/area/host",
            "title": "Host Node",
        }
        entities = [
            _Entity(
                title="ExactMatch",
                file_path="x/exact.md",
                permalink="x/exact",
                # exactly the host permalink
                outgoing_relations=[
                    _Rel(to_name="Host Node", to_permalink="areas/area/host",
                         relation_type="part_of")
                ],
            ),
            _Entity(
                title="PrefixOnly",
                file_path="x/prefix.md",
                permalink="x/prefix",
                # a permalink that the host's permalink starts WITH — a substring
                # match would wrongly include this row.
                outgoing_relations=[
                    _Rel(to_name="Host", to_permalink="areas/area",
                         relation_type="part_of")
                ],
            ),
        ]
        r = _run(
            "filters: part_of.contains(this.file)\nviews:\n  - type: list\n",
            entities,
            host=host,
        )
        assert r["status"] == "success", r.get("error")
        assert {row.get("title") for row in r["results"]} == {"ExactMatch"}

    def test_string_contains_substring_still_works(self):
        """BP4-A-05 non-regression: title.contains("foo") stays a substring test.

        Membership semantics applies ONLY to a list-of-links receiver; a STRING
        receiver keeps its substring ``contains``.
        """
        entities = [
            _Entity(title="foobar note", file_path="x/foo.md", permalink="x/foo"),
            _Entity(title="unrelated", file_path="x/bar.md", permalink="x/bar"),
        ]
        r = _run(
            'filters: title.contains("foo")\nviews:\n  - type: list\n',
            entities,
        )
        assert r["status"] == "success", r.get("error")
        assert {row.get("title") for row in r["results"]} == {"foobar note"}


# ===========================================================================
# BP4-A-06 — file.outlinks / hasLink unchanged (union preserved, additive)
# ===========================================================================
class TestOutlinksUnchanged:
    def test_haslink_still_matches_union_of_all_relation_types(self):
        """BP4-A-06: file.hasLink(this.file) sees the UNION of all relations.

        Child-A (part_of), Member-1 (member_of) both link the host → hasLink
        (union) returns BOTH, proving file.outlinks stays the union and the new
        per-type keys are purely additive.
        """
        r = _run(
            "filters: file.hasLink(this.file)\nviews:\n  - type: list\n",
            _area_entities(),
            host=HOST_METADATA,
        )
        assert r["status"] == "success", r.get("error")
        # Child-A, Child-B, Member-1 all link the host (part_of x2 + member_of);
        # Stranger links Other Area only.
        assert r["result_count"] == 3
        assert {row.get("title") for row in r["results"]} == {
            "Child-A",
            "Child-B",
            "Member-1",
        }


# ===========================================================================
# BP4-A-07 — collision frontmatter key ↔ body relation: body relation canonical
# ===========================================================================
class TestFrontmatterRelationCollision:
    def test_body_relation_wins_over_frontmatter_homonym(self):
        """BP4-A-07: a body relation key overrides a homonym frontmatter scalar.

        The note carries BOTH a frontmatter ``part_of: "some string"`` AND a body
        relation ``part_of`` pointing at the host. The exposed ``part_of`` key is
        the body relation (a list of links), so the membership filter matches —
        a frontmatter scalar string would have made ``contains`` substring-fall
        back and miss.
        """
        entities = [
            _Entity(
                title="Collider",
                file_path="x/coll.md",
                permalink="x/coll",
                # frontmatter homonym — a bare scalar, NOT the host identity
                metadata={"part_of": "a frontmatter string value"},
                outgoing_relations=[
                    _Rel(to_name=HOST_TITLE, to_permalink=HOST_PERMALINK,
                         relation_type="part_of")
                ],
            ),
        ]
        r = _run(
            "filters: part_of.contains(this.file)\nviews:\n  - type: list\n",
            entities,
            host=HOST_METADATA,
        )
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 1
        assert {row.get("title") for row in r["results"]} == {"Collider"}
