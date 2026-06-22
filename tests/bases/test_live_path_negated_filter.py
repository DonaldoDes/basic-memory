"""Live-path tests for US-7 (M-Bases-P4): negated function-call filter leaf.

These exercise the REAL MCP render path (``BasesIntegration.process_note`` via
``create_bases_integration``) — NOT a synthetic executor call — with a dataset
faithful to the production provider shape (nested ``file`` dict carrying
``path``/``name``/``folder``), per the ticket's NORMATIVE requirement (leçon
P3/US-f: synthetic-green / live-red divergence).

Scenarios:
  BP4-G-05  ``not file.inFolder(...)`` + ``!contains(file.path, ...)`` render
            non-empty, correct exclusions on the live path (no 'Trailing tokens').
  BP4-G-07  A faithful reproduction of the Vault Lint pattern (exclude a subtree
            with ``not contains(file.path, ...)`` / ``not file.inFolder(...)``)
            renders the same surviving rows as the equivalent dataview exclusion.
"""

from __future__ import annotations

from basic_memory.bases.integration import create_bases_integration


def _dataset() -> list[dict]:
    """A faithful provider-shape dataset spanning several subtrees."""
    paths = [
        "products/X/note-a.md",
        "products/X/note-b.md",
        "resources/daily/2026-06-22.md",
        "resources/daily/2026-06-21.md",
        "archives/old/legacy-1.md",
        "areas/admin/contract.md",
    ]
    from pathlib import PurePosixPath

    notes: list[dict] = []
    for p in paths:
        notes.append(
            {
                "file": {
                    "path": p,
                    "name": PurePosixPath(p).name,
                    "folder": str(PurePosixPath(p).parent),
                    "outlinks": [],
                },
                "title": PurePosixPath(p).stem,
                "type": "note",
            }
        )
    return notes


def _block(body: str) -> str:
    return f"```base\n{body}```"


def _run(body: str) -> dict:
    integ = create_bases_integration(notes_provider=_dataset)
    results = integ.process_note(_block(body), note_metadata=None)
    assert len(results) == 1
    return results[0]


def _paths(result: dict) -> set[str]:
    """Collect the rendered rows' file.path values (any column key shape)."""
    out: set[str] = set()
    for row in result.get("results", []):
        # rows carry file.path under that key in the projection
        val = row.get("file.path") or row.get("path")
        if val:
            out.add(val)
    return out


# ===========================================================================
# BP4-G-05 — not file.inFolder / !contains render non-empty on the live path
# ===========================================================================
class TestNegatedInFolderLivePath:
    def test_not_infolder_excludes_subtree_live(self):
        """BP4-G-05: ``not file.inFolder("resources/daily")`` excludes that subtree.

        On the REAL process_note path: every note NOT under resources/daily is
        rendered; the two daily notes are excluded; no 'Trailing tokens' error.
        """
        body = (
            'filters: not file.inFolder("resources/daily")\n'
            "views:\n"
            "  - type: table\n"
            "    order:\n"
            "      - file.name\n"
            "      - file.path\n"
        )
        r = _run(body)
        assert r["status"] == "success", r.get("error")
        paths = _paths(r)
        # The 4 non-daily notes survive, the 2 daily notes are excluded.
        assert "resources/daily/2026-06-22.md" not in paths
        assert "resources/daily/2026-06-21.md" not in paths
        assert "products/X/note-a.md" in paths
        assert "archives/old/legacy-1.md" in paths
        assert r["result_count"] == 4

    def test_not_contains_file_path_excludes_by_substring_live(self):
        """BP4-G-05: ``!contains(file.path, "archives")`` excludes by path substring."""
        body = (
            'filters: not contains(file.path, "archives")\n'
            "views:\n"
            "  - type: table\n"
            "    order:\n"
            "      - file.name\n"
            "      - file.path\n"
        )
        r = _run(body)
        assert r["status"] == "success", r.get("error")
        paths = _paths(r)
        assert "archives/old/legacy-1.md" not in paths
        assert "products/X/note-a.md" in paths
        assert "resources/daily/2026-06-22.md" in paths
        # 5 of 6 notes survive (only the archives note is excluded).
        assert r["result_count"] == 5

    def test_combined_negations_and_positive_from_live(self):
        """BP4-G-05: a positive FROM combined with a negated subtree exclusion."""
        body = (
            "filters:\n"
            "  and:\n"
            '    - file.inFolder("resources")\n'
            '    - not file.inFolder("resources/daily")\n'
            "views:\n"
            "  - type: table\n"
            "    order:\n"
            "      - file.path\n"
        )
        r = _run(body)
        assert r["status"] == "success", r.get("error")
        # FROM resources scopes to the 2 daily notes; the negated inFolder then
        # excludes all of them → empty but successful (no error).
        assert r["result_count"] == 0


# ===========================================================================
# BP4-G-07 — Vault-Lint-style subtree exclusion matches a dataview exclusion
# ===========================================================================
class TestVaultLintParityLivePath:
    def test_exclusion_matches_expected_dataview_surviving_rows(self):
        """BP4-G-07: ``not contains(file.path, "archives")`` over the dataset
        yields exactly the rows a dataview ``WHERE !contains(file.path,
        "archives")`` would keep — every note whose path lacks 'archives'.
        """
        body = (
            'filters: not contains(file.path, "archives")\n'
            "views:\n"
            "  - type: table\n"
            "    order:\n"
            "      - file.path\n"
        )
        r = _run(body)
        assert r["status"] == "success", r.get("error")
        got = _paths(r)
        expected = {n["file"]["path"] for n in _dataset() if "archives" not in n["file"]["path"]}
        assert got == expected

    def test_not_infolder_matches_expected_dataview_surviving_rows(self):
        """BP4-G-07: ``not file.inFolder("resources/daily")`` keeps exactly the
        notes a dataview prefix-exclusion would keep (path not under the subtree).
        """
        body = (
            'filters: not file.inFolder("resources/daily")\n'
            "views:\n"
            "  - type: table\n"
            "    order:\n"
            "      - file.path\n"
        )
        r = _run(body)
        assert r["status"] == "success", r.get("error")
        got = _paths(r)
        expected = {
            n["file"]["path"] for n in _dataset() if "resources/daily" not in n["file"]["path"]
        }
        assert got == expected
