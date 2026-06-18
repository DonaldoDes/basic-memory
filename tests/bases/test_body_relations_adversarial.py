"""Adversarial tests for US-1 body relations (M-Bases-P4, sensitive zone).

The provider now exposes each relation TYPE as a list-of-links key and the
executor membership test (``part_of.contains(this.file)``) is a NEW evaluation
surface over UNTRUSTED note content (ADR-006 §Invariants — "zone sensible
renforcée"). These tests pin the hostile cases:

  ADV-1  Empty / absent relation list never matches (a host with no children
         renders 0, no crash, no false positive).
  ADV-2  A relation pointing ELSEWHERE never matches the host (membership is
         exact identity, not "any link present").
  ADV-3  Substring false-positive is impossible: a relation target that is a
         SUPERSTRING or SUBSTRING of a host identity must NOT match.
  ADV-4  Cardinality of an exposed relation-type key is BOUNDED — a pathological
         note with thousands of relations of one type is capped, the block stays
         live (no DoS, no partial-eval crash).
  ADV-5  Collision frontmatter ↔ body relation: the body relation is canonical;
         a hostile frontmatter scalar homonym cannot suppress the relation.
  ADV-6  Membership evaluation touches NO filesystem (sentinel patches
         ``open``/``os`` to explode).
  ADV-7  Static guard: the touched source introduces no
         ``eval``/``exec``/``getattr``/``__import__`` on note content.
"""

from __future__ import annotations

import builtins
import re
from pathlib import Path

from basic_memory.bases.formula_eval import _member_contains, _VirtualFile
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


def _run(body: str, entities: list[_Entity], host: dict | None = None) -> dict:
    integ = create_bases_integration(
        notes_provider=lambda: build_dataview_dataset(entities)
    )
    results = integ.process_note(_block(body), note_metadata=host)
    assert len(results) == 1
    return results[0]


# ===========================================================================
# ADV-1 — empty / absent relation list
# ===========================================================================
class TestEmptyRelation:
    def test_no_part_of_relation_renders_zero(self):
        """A note with relations of OTHER types only → part_of key absent → 0."""
        entities = [
            _Entity(
                title="OnlyMemberOf",
                file_path="x/a.md",
                permalink="x/a",
                outgoing_relations=[
                    _Rel(to_name=HOST_TITLE, to_permalink=HOST_PERMALINK,
                         relation_type="member_of")
                ],
            )
        ]
        r = _run(
            "filters: part_of.contains(this.file)\nviews:\n  - type: list\n",
            entities,
            host=HOST_METADATA,
        )
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 0

    def test_empty_list_membership_is_false(self):
        """Direct executor unit: contains on an EMPTY list is always False."""
        host_file = _VirtualFile("Host.md", identities=["host/x", "Host"])
        assert _member_contains([], [host_file]) is False


# ===========================================================================
# ADV-2 — relation pointing elsewhere
# ===========================================================================
class TestRelationElsewhere:
    def test_part_of_other_target_does_not_match_host(self):
        entities = [
            _Entity(
                title="ElsewhereChild",
                file_path="x/b.md",
                permalink="x/b",
                outgoing_relations=[
                    _Rel(to_name="Other", to_permalink="some/other/node",
                         relation_type="part_of")
                ],
            )
        ]
        r = _run(
            "filters: part_of.contains(this.file)\nviews:\n  - type: list\n",
            entities,
            host=HOST_METADATA,
        )
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 0


# ===========================================================================
# ADV-3 — substring false-positive impossible
# ===========================================================================
class TestNoSubstringFalsePositive:
    def test_superstring_target_does_not_match(self):
        """A relation target that CONTAINS a host identity as substring ≠ match."""
        host_file = _VirtualFile("Host.md", identities=["areas/area/host"])
        # element is a superstring of the host permalink
        assert _member_contains(["areas/area/host/child"], [host_file]) is False

    def test_substring_target_does_not_match(self):
        """A relation target that is a SUBSTRING of a host identity ≠ match."""
        host_file = _VirtualFile("Host.md", identities=["areas/area/host"])
        assert _member_contains(["areas/area"], [host_file]) is False

    def test_exact_identity_matches(self):
        host_file = _VirtualFile("Host.md", identities=["areas/area/host", "Host"])
        assert _member_contains(["areas/area/host"], [host_file]) is True
        assert _member_contains(["Host"], [host_file]) is True


# ===========================================================================
# ADV-4 — cardinality of an exposed relation key is bounded
# ===========================================================================
class TestCardinalityBounded:
    def test_pathological_relation_cardinality_is_capped(self):
        """A note with 5000 part_of relations exposes a CAPPED list, stays live.

        The exposed ``part_of`` key length must not exceed the provider cap; the
        block renders without a DoS or partial-eval crash. The host is included
        among the targets so the filter still matches the row.
        """
        rels = [
            _Rel(to_name=f"noise-{i}", to_permalink=f"noise/{i}",
                 relation_type="part_of")
            for i in range(5000)
        ]
        # ensure the host IS among the (capped) targets — place it first
        rels.insert(0, _Rel(to_name=HOST_TITLE, to_permalink=HOST_PERMALINK,
                            relation_type="part_of"))
        entities = [
            _Entity(title="Pathological", file_path="x/p.md", permalink="x/p",
                    outgoing_relations=rels)
        ]
        dataset = build_dataview_dataset(entities)
        # provider cap enforced on the exposed list
        assert len(dataset[0]["part_of"]) <= 500
        r = _run(
            "filters: part_of.contains(this.file)\nviews:\n  - type: list\n",
            entities,
            host=HOST_METADATA,
        )
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 1


# ===========================================================================
# ADV-5 — collision frontmatter ↔ body relation, hostile scalar
# ===========================================================================
class TestCollisionHostileFrontmatter:
    def test_frontmatter_scalar_cannot_suppress_body_relation(self):
        """A frontmatter ``part_of: "<host permalink as a lure>"`` cannot break it.

        Even a frontmatter string crafted to look like the host permalink must
        not change the outcome: the body relation (a real list) is canonical and
        matches; membership stays exact (the lure string, even if equal to an
        identity, is merged but never causes a substring leak elsewhere).
        """
        entities = [
            _Entity(
                title="Lure",
                file_path="x/lure.md",
                permalink="x/lure",
                metadata={"part_of": HOST_PERMALINK},  # scalar homonym
                outgoing_relations=[
                    _Rel(to_name=HOST_TITLE, to_permalink=HOST_PERMALINK,
                         relation_type="part_of")
                ],
            )
        ]
        dataset = build_dataview_dataset(entities)
        # body relation canonical → exposed value is a LIST, not the scalar
        assert isinstance(dataset[0]["part_of"], list)
        r = _run(
            "filters: part_of.contains(this.file)\nviews:\n  - type: list\n",
            entities,
            host=HOST_METADATA,
        )
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 1


# ===========================================================================
# ADV-6 — membership touches no filesystem
# ===========================================================================
class TestNoFilesystemAccess:
    def test_membership_does_not_open_files(self, monkeypatch):
        """Rendering part_of.contains must never call open()/os on the FS."""

        def _explode(*a, **k):  # pragma: no cover - must never run
            raise AssertionError("filesystem access during membership eval")

        monkeypatch.setattr(builtins, "open", _explode)
        import os as _os

        monkeypatch.setattr(_os, "stat", _explode)
        # _area-like single child linking the host
        entities = [
            _Entity(
                title="Child",
                file_path="x/c.md",
                permalink="x/c",
                outgoing_relations=[
                    _Rel(to_name=HOST_TITLE, to_permalink=HOST_PERMALINK,
                         relation_type="part_of")
                ],
            )
        ]
        # dataset must be built OUTSIDE the patch (provider reads files); the
        # membership eval below runs against the in-memory dataset only.
        dataset = build_dataview_dataset(entities)
        integ = create_bases_integration(notes_provider=lambda: dataset)
        r = integ.process_note(
            _block("filters: part_of.contains(this.file)\nviews:\n  - type: list\n"),
            note_metadata=HOST_METADATA,
        )[0]
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 1


# ===========================================================================
# ADV-7 — static guard: no new eval/exec/getattr/__import__ on touched source
# ===========================================================================
class TestNoDynamicEvaluation:
    def test_no_forbidden_primitives_in_formula_sandbox(self):
        """The membership change introduces no dynamic-execution primitive.

        Uses the SAME convention as ``test_live_path_adversarial`` — a negative
        lookbehind so the AST evaluator's OWN ``self.eval(...)`` method (named
        ``eval`` by design, NOT Python builtin ``eval``) and ``re.compile`` are
        not false positives. The formula sandbox evaluates UNTRUSTED note content
        → no eval/exec/getattr/__import__/compile reachable.
        """
        repo = Path(__file__).resolve().parents[2]
        sandbox = repo / "src/basic_memory/bases/formula_eval.py"
        forbidden = re.compile(
            r"(?<![\w.])(?:exec|compile|__import__|getattr)\s*\("
            r"|(?<![\w.])eval\s*\("
        )
        offenders: list[str] = []
        text = sandbox.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("def "):
                continue
            if forbidden.search(line):
                offenders.append(f"{sandbox.name}:{lineno}: {stripped}")
        assert not offenders, "Forbidden primitive in sandbox:\n" + "\n".join(offenders)

    def test_provider_uses_no_eval_exec_import(self):
        """The provider grouping introduces no eval/exec/__import__.

        ``getattr`` IS used in the provider, but ONLY to read attributes off the
        trusted SQLAlchemy ``Entity``/``Relation`` ORM rows (``relation_type``,
        ``to_entity``, ``outgoing_relations``) — never note CONTENT. The dynamic
        execution primitives that could run untrusted content are forbidden.
        """
        repo = Path(__file__).resolve().parents[2]
        provider = repo / "src/basic_memory/api/v2/routers/knowledge_router.py"
        forbidden = re.compile(
            r"(?<![\w.])(?:exec|__import__)\s*\(" r"|(?<![\w.])eval\s*\("
        )
        text = provider.read_text(encoding="utf-8")
        offenders = []
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("def "):
                continue
            if forbidden.search(line):
                offenders.append(f"knowledge_router.py:{lineno}: {stripped}")
        assert not offenders, "Forbidden primitive in provider:\n" + "\n".join(offenders)
