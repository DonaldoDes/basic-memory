"""US-b (M-Bases-P3) — Self-reference ``this`` + ``file.hasLink(this.file)``.

Wires the host-note identity end to end (caller -> ``process_note(content,
note_metadata=...)`` -> executor -> sandbox evaluation context exposing
``this.file``) and implements ``file.hasLink(this.file)`` as a boolean filter
predicate, bounded to the dataset (reads the row's outlinks, never the
filesystem, never recomputing a global link graph).

Test IDs (ADR-005 §Axe 2, US-b):
    BP3-B-01  process_note propagates note_metadata (host identity) to executor
    BP3-B-02  the 3 callers pass the host path/permalink to process_note
    BP3-B-03  this.file resolves to the host identity, never another note
    BP3-B-04  file.hasLink(this.file) boolean true iff outlink present in row
    BP3-B-05  hasLink reads dataset outlinks, no filesystem, no graph recompute [ADVERSARIAL]
    BP3-B-06  this bounded to dataset + host (no arbitrary note referenceable)   [ADVERSARIAL]
    BP3-B-07  converted Activity block renders the list of inbound notes
    BP3-B-08  block without this is non-regressed (transparent wiring seam)
    BP3-B-09  file.backlinks (list) rejected/inert in v1 (deferred)
"""

import inspect

import pytest

from basic_memory.bases.errors import BasesUnsupportedError
from basic_memory.bases.executor import BasesExecutor
from basic_memory.bases.formula_eval import evaluate_formula, safe_evaluate_formula
from basic_memory.bases.formula_parser import parse_formula
from basic_memory.bases.integration import create_bases_integration
from basic_memory.bases.parser import BasesParser


# ---------------------------------------------------------------------------
# Datasets — nested shape returned by list_entities_for_dataview, now carrying
# outlinks on the file sub-dict (the ONLY new dataset field this US relies on).
# ---------------------------------------------------------------------------
HOST_PERMALINK = "products/X/host"
HOST_PATH = "products/X/Host.md"


def _row(title, path, outlinks, permalink=None):
    file = {
        "path": path,
        "name": title,
        "folder": path.rsplit("/", 1)[0],
        "outlinks": list(outlinks),
    }
    note = {
        "title": title,
        "file": file,
        "created_at": "2026-01-01",
        "updated_at": "2026-01-10",
        "frontmatter": {"status": "Active"},
    }
    if permalink is not None:
        note["permalink"] = permalink
    return note


def _activity_notes():
    """A, B point to the host; C does not."""
    return [
        _row("A", "products/X/A.md", outlinks=[HOST_PERMALINK], permalink="products/X/a"),
        _row("B", "products/X/B.md", outlinks=[HOST_PERMALINK], permalink="products/X/b"),
        _row("C", "products/X/C.md", outlinks=["products/X/other"], permalink="products/X/c"),
    ]


HOST_METADATA = {"permalink": HOST_PERMALINK, "path": HOST_PATH, "title": "Host"}

ACTIVITY_BODY = """filters: file.hasLink(this.file)
views:
  - type: list
"""

ACTIVITY_BLOCK = f"```base\n{ACTIVITY_BODY}```"

PLAIN_BLOCK = """```base
filters: status == "Active"
views:
  - type: list
```"""


# ===========================================================================
# BP3-B-01 — process_note propagates note_metadata to the executor
# ===========================================================================
class TestHostIdentityWiring:
    def test_process_note_accepts_and_threads_note_metadata(self):
        # BP3-B-01: with host identity wired, hasLink(this.file) can resolve.
        integ = create_bases_integration(notes_provider=_activity_notes)
        results = integ.process_note(ACTIVITY_BLOCK, note_metadata=HOST_METADATA)
        assert len(results) == 1
        r = results[0]
        assert r["status"] == "success", r.get("error")
        # A and B point to the host, C does not.
        assert r["result_count"] == 2

    def test_process_note_without_metadata_for_this_block_is_inert(self):
        # BP3-B-01 corollary: a this-using block with NO host identity cannot
        # resolve this.file → inert block (status:error), never a crash, never
        # a silent wrong match against an arbitrary note.
        integ = create_bases_integration(notes_provider=_activity_notes)
        results = integ.process_note(ACTIVITY_BLOCK)  # no note_metadata
        assert len(results) == 1
        r = results[0]
        # The block must NOT silently match rows: either inert or zero matches,
        # never the 2-match success that requires a resolved host.
        assert r["status"] == "error" or r["result_count"] == 0


# ===========================================================================
# BP3-B-02 — the 3 callers pass the host path/permalink to process_note
# ===========================================================================
class TestCallersPassHostIdentity:
    def _source(self, fn):
        return inspect.getsource(fn)

    def test_read_note_enrich_passes_note_metadata(self):
        from basic_memory.mcp.tools.read_note import _enrich_with_bases

        src = self._source(_enrich_with_bases)
        assert "note_metadata=" in src

    def test_build_context_passes_note_metadata(self):
        import importlib

        bc = importlib.import_module("basic_memory.mcp.tools.build_context")
        src = inspect.getsource(bc)
        # The bases enrichment path threads note_metadata into process_note.
        assert "note_metadata=" in src

    def test_search_passes_note_metadata(self):
        import importlib

        s = importlib.import_module("basic_memory.mcp.tools.search")
        src = inspect.getsource(s)
        assert "note_metadata=" in src

    def test_enrich_with_bases_accepts_host_identity_param(self):
        # The seam must carry the host identity from the caller.
        from basic_memory.mcp.tools.read_note import _enrich_with_bases

        sig = inspect.signature(_enrich_with_bases)
        # A new optional parameter carrying the host identity.
        assert any(p in sig.parameters for p in ("note_metadata", "host_metadata", "host_identity"))


# ===========================================================================
# BP3-B-03 — this.file resolves to the host identity, never another note
# ===========================================================================
class TestThisResolvesHost:
    def test_this_file_path_is_host_path(self):
        node = parse_formula("this.file.path")
        # Host identity is injected into the evaluation context.
        value = evaluate_formula(node, _activity_notes()[0], host=HOST_METADATA)
        assert value == HOST_PATH

    def test_this_file_link_is_host_permalink_based(self):
        node = parse_formula("this.file.path")
        # Even evaluating against row C (which points elsewhere), this.file is
        # ALWAYS the host — never the current row, never another note.
        for row in _activity_notes():
            value = evaluate_formula(node, row, host=HOST_METADATA)
            assert value == HOST_PATH

    def test_this_without_host_resolves_to_none_not_a_row(self):
        # No host injected → this.file is None (the block goes inert downstream),
        # never silently the current row.
        node = parse_formula("this.file.path")
        value, err = safe_evaluate_formula(node, _activity_notes()[0])
        # Either it degrades to None/empty, or it errors — never the row path.
        assert value in (None, "")


# ===========================================================================
# BP3-B-04 — file.hasLink(this.file) boolean true iff outlink present in row
# ===========================================================================
class TestHasLinkBoolean:
    def test_haslink_true_when_row_points_to_host(self):
        node = parse_formula("file.hasLink(this.file)")
        row = _activity_notes()[0]  # A → host
        assert evaluate_formula(node, row, host=HOST_METADATA) is True

    def test_haslink_false_when_row_does_not_point_to_host(self):
        node = parse_formula("file.hasLink(this.file)")
        row = _activity_notes()[2]  # C → other
        assert evaluate_formula(node, row, host=HOST_METADATA) is False

    def test_haslink_matches_on_path_or_permalink(self):
        # Host identity may be referenced by permalink OR path in outlinks.
        node = parse_formula("file.hasLink(this.file)")
        row_by_path = _row("D", "products/X/D.md", outlinks=[HOST_PATH])
        assert evaluate_formula(node, row_by_path, host=HOST_METADATA) is True

    def test_haslink_filters_dataset_via_executor(self):
        # End-to-end through the executor select pipeline.
        query = BasesParser.parse(ACTIVITY_BODY)
        rows = BasesExecutor(_activity_notes(), host=HOST_METADATA).select(query)
        titles = sorted(r["title"] for r in rows)
        assert titles == ["A", "B"]


# ===========================================================================
# BP3-B-05 — hasLink reads dataset outlinks only (no FS, no graph recompute)
# [ADVERSARIAL]
# ===========================================================================
class TestHasLinkBoundedNoFilesystem:
    def test_haslink_never_opens_a_file(self, monkeypatch):
        import builtins

        calls = {"open": 0}
        real_open = builtins.open

        def _sentinel_open(*a, **k):
            calls["open"] += 1
            return real_open(*a, **k)

        monkeypatch.setattr(builtins, "open", _sentinel_open)

        node = parse_formula("file.hasLink(this.file)")
        for row in _activity_notes():
            evaluate_formula(node, row, host=HOST_METADATA)
        assert calls["open"] == 0

    def test_haslink_never_calls_os_module(self, monkeypatch):
        import os

        sentinel = {"hit": 0}
        for name in ("stat", "scandir", "listdir", "walk", "open"):
            if hasattr(os, name):
                monkeypatch.setattr(
                    os, name, lambda *a, _s=sentinel, **k: _s.__setitem__("hit", _s["hit"] + 1)
                )
        node = parse_formula("file.hasLink(this.file)")
        for row in _activity_notes():
            evaluate_formula(node, row, host=HOST_METADATA)
        assert sentinel["hit"] == 0

    def test_haslink_reads_only_the_row_outlinks(self):
        # A row with NO outlinks key cannot match (no recompute, no FS lookup).
        node = parse_formula("file.hasLink(this.file)")
        bare = {
            "title": "Bare",
            "file": {"path": "products/X/Bare.md", "name": "Bare", "folder": "products/X"},
            "frontmatter": {},
        }
        assert evaluate_formula(node, bare, host=HOST_METADATA) is False


# ===========================================================================
# BP3-B-06 — this bounded to dataset + host (no arbitrary note referenceable)
# [ADVERSARIAL]
# ===========================================================================
class TestThisBounded:
    def test_this_exposes_only_file_member(self):
        # this.content / this.frontmatter / arbitrary members are refused —
        # this is NOT a handle to reload the host from disk.
        for hostile in (
            "this.content",
            "this.frontmatter",
            "this.body",
            "this.file.content",
        ):
            with pytest.raises(BasesUnsupportedError):
                node = parse_formula(hostile)
                evaluate_formula(node, _activity_notes()[0], host=HOST_METADATA)

    def test_this_dunder_is_rejected(self):
        for hostile in ("this.__class__", "this.file.__class__"):
            with pytest.raises(BasesUnsupportedError):
                parse_formula(hostile)

    def test_haslink_cannot_target_arbitrary_constructed_path(self):
        # hasLink only compares against the injected host identity; it cannot be
        # coerced to test membership of an arbitrary attacker path that is not in
        # the row outlinks. A row pointing only to the host matches the host, and
        # a row pointing elsewhere never matches the host.
        node = parse_formula("file.hasLink(this.file)")
        evil_host = {"permalink": "secret/credentials", "path": "secret/credentials.md"}
        # Row A points to HOST_PERMALINK, NOT to the evil host → no match.
        assert evaluate_formula(node, _activity_notes()[0], host=evil_host) is False


# ===========================================================================
# BP3-B-07 — converted Activity block renders the list of inbound notes
# ===========================================================================
class TestActivityBlockRenders:
    def test_activity_block_renders_inbound_notes(self):
        integ = create_bases_integration(notes_provider=_activity_notes)
        results = integ.process_note(ACTIVITY_BLOCK, note_metadata=HOST_METADATA)
        r = results[0]
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 2
        md = r["result_markdown"]
        assert "[[A]]" in md and "[[B]]" in md
        assert "[[C]]" not in md


# ===========================================================================
# BP3-B-08 — block without this is non-regressed (transparent wiring seam)
# ===========================================================================
class TestNoRegressionWithoutThis:
    def test_plain_block_identical_with_or_without_metadata(self):
        integ = create_bases_integration(notes_provider=_activity_notes)
        without = integ.process_note(PLAIN_BLOCK)
        with_md = integ.process_note(PLAIN_BLOCK, note_metadata=HOST_METADATA)
        assert without[0]["status"] == "success"
        assert with_md[0]["status"] == "success"
        assert without[0]["result_markdown"] == with_md[0]["result_markdown"]
        assert without[0]["result_count"] == with_md[0]["result_count"] == 3


# ===========================================================================
# BP3-B-09 — file.backlinks (list) rejected/inert in v1 (deferred)
# ===========================================================================
class TestBacklinksDeferred:
    def test_backlinks_member_is_unsupported(self):
        # file.backlinks (list) is OUT of v1 → parser refuses the member.
        with pytest.raises(BasesUnsupportedError):
            parse_formula("file.backlinks")

    def test_backlinks_block_is_inert(self):
        block = """```base
filters: file.backlinks
views:
  - type: list
```"""
        integ = create_bases_integration(notes_provider=_activity_notes)
        results = integ.process_note(block, note_metadata=HOST_METADATA)
        assert results[0]["status"] == "error"
        assert results[0]["error_type"] in ("unsupported", "parse")
