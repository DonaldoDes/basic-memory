"""Unit tests for ``file.folder`` as a requestable member/field (US-8 / BP4-H).

Ground-truth gap (verified against HEAD before this change):

- The formula parser already PARSES ``file.folder.startsWith(...)`` (``folder`` is
  absorbed into a dotted ``FField`` named ``file.folder``; ``.startsWith`` then
  parses as a closed-table member). It also parses ``startsWith(file.folder, "x")``.
- BUT when the dataset row carries only ``file.path`` (the flat executor-projected
  shape) and NOT an explicit ``folder``, ``file.folder`` resolves to the empty
  string → ``startsWith(file.folder, "x")`` reads vide and the filter renders 0
  (the silent-empty-cell trap the ticket warns about).

US-8 closes that gap by deriving ``folder`` from ``file.path`` IN THE BASES
SANDBOX (``_eval_field``) — reading the dataset row only, never the filesystem,
never ``getattr`` on content, no ``eval``/``exec``/``__import__``.

Test IDs: BP4-H-01 (parse), BP4-H-02 (member ↔ column coherence),
BP4-H-04 (dataset-only derivation). Adversarial: BP4-H-05 (sandbox closed).
"""

from __future__ import annotations

import pytest

from basic_memory.bases.errors import BasesError
from basic_memory.bases.formula_eval import FormulaEvaluator
from basic_memory.bases.formula_parser import parse_formula


def _eval(expr: str, row: dict):
    return FormulaEvaluator(row).eval(parse_formula(expr))


# --------------------------------------------------------------------------- #
# BP4-H-01 — file.folder method-chain + function form parse without breaking
# --------------------------------------------------------------------------- #
class TestFolderParses:
    @pytest.mark.parametrize(
        "expr",
        [
            'file.folder.startsWith("products")',
            'file.folder.endsWith("stories")',
            'file.folder.contains("milestones")',
            'startsWith(file.folder, "products")',
            "file.folder",
        ],
    )
    def test_parses_without_member_error(self, expr):
        # Must not raise — folder is a queryable file member/field.
        parse_formula(expr)


# --------------------------------------------------------------------------- #
# BP4-H-04 — folder derived from the dataset row (file.path), never FS, even
# when the row carries only file.path (flat executor-projected shape).
# --------------------------------------------------------------------------- #
class TestFolderDerivedFromPath:
    def test_flat_row_folder_derived_from_file_path(self):
        # The real gap: row has file.path but no explicit folder.
        row = {"file.path": "resources/refs/note.md", "title": "Note"}
        assert _eval("file.folder", row) == "resources/refs"

    def test_flat_row_startswith_reads_real_folder(self):
        row = {"file.path": "resources/refs/note.md", "title": "Note"}
        assert _eval('startsWith(file.folder, "resources")', row) is True
        assert _eval('startsWith(file.folder, "products")', row) is False

    def test_flat_row_method_chain_reads_real_folder(self):
        row = {"file.path": "products/bm/m1/stories/us-8.md", "title": "US"}
        assert _eval('file.folder.startsWith("products")', row) is True
        assert _eval('file.folder.endsWith("stories")', row) is True
        assert _eval('file.folder.contains("m1")', row) is True

    def test_root_level_note_folder_is_empty_string(self):
        # A note at the vault root has no parent folder → empty string, not "."
        row = {"file.path": "readme.md", "title": "Readme"}
        assert _eval("file.folder", row) == ""

    def test_explicit_nested_folder_takes_precedence(self):
        # Provider nested shape: explicit folder must be honoured verbatim.
        row = {
            "file": {"path": "a/b/note.md", "name": "note.md", "folder": "custom/folder"},
            "title": "Note",
        }
        assert _eval("file.folder", row) == "custom/folder"
        assert _eval('startsWith(file.folder, "custom")', row) is True

    def test_explicit_flat_folder_key_takes_precedence(self):
        row = {"file.path": "a/b/note.md", "file.folder": "override", "title": "Note"}
        assert _eval("file.folder", row) == "override"


# --------------------------------------------------------------------------- #
# BP4-H-02 — coherence: the value read via file.folder (member-chain receiver)
# equals the value read via file.folder (column/field) — no divergence.
# --------------------------------------------------------------------------- #
class TestMemberColumnCoherence:
    @pytest.mark.parametrize(
        "row",
        [
            {"file.path": "resources/refs/note.md", "title": "N"},
            {"file": {"path": "resources/refs/note.md", "folder": "resources/refs"}, "title": "N"},
        ],
    )
    def test_member_chain_receiver_matches_column(self, row):
        column_value = _eval("file.folder", row)
        # The method-chain receiver resolves the SAME folder string.
        prefix = column_value.split("/", 1)[0] if column_value else ""
        assert _eval(f'file.folder.startsWith("{prefix}")', row) is True
        assert column_value == "resources/refs"


# --------------------------------------------------------------------------- #
# BP4-H-05 — ADVERSARIAL: folder member introduces NO access outside the
# dataset. Sandbox stays closed (no eval/exec/getattr/__import__/FS).
# --------------------------------------------------------------------------- #
class TestFolderSandboxClosed:
    def test_filesystem_sentinel_never_touched(self, monkeypatch):
        """[ADVERSARIAL] Resolving file.folder triggers ZERO filesystem access.

        A real folder exists on the row's path (resources/refs) but the value is
        derived purely from the dataset string. We trip a sentinel on every
        filesystem entry point — if folder resolution ever opens/stats a path,
        the sentinel fires.
        """
        import builtins
        import os

        def _boom(*a, **k):  # pragma: no cover - must never run
            raise AssertionError("filesystem access during file.folder resolution")

        # Narrow sentinel on the FS *content* entry points folder-derivation
        # could use to read a directory/file (open, os.listdir, os.scandir).
        # os.stat / os.path.exists are intentionally NOT patched: pytest's own
        # traceback/linecache machinery calls them, and folder derivation uses
        # PurePosixPath (a pure string parent op) with no filesystem access.
        monkeypatch.setattr(builtins, "open", _boom)
        monkeypatch.setattr(os, "listdir", _boom)
        monkeypatch.setattr(os, "scandir", _boom)

        row = {"file.path": "resources/refs/note.md", "title": "Note"}
        assert _eval("file.folder", row) == "resources/refs"
        assert _eval('startsWith(file.folder, "resources")', row) is True
        assert _eval('file.folder.endsWith("refs")', row) is True

    def test_path_traversal_in_folder_is_inert_string_not_fs(self):
        """[ADVERSARIAL] A hostile traversal path stays an inert dataset string.

        ``file.folder`` of a ``../../etc/passwd`` path is just the parent string;
        it is NEVER resolved against the filesystem, so no escape is possible.
        """
        row = {"file.path": "../../etc/passwd.md", "title": "Evil"}
        # Derived purely as a string parent; no FS resolution, no escape.
        assert _eval("file.folder", row) == "../../etc"
        # And it never matches an in-vault prefix.
        assert _eval('startsWith(file.folder, "resources")', row) is False

    def test_dunder_member_after_folder_still_refused(self):
        """[ADVERSARIAL] folder does not open a generic member surface.

        ``file.folder.__class__`` must still be refused by the closed grammar —
        adding folder support must not weaken dunder rejection.
        """
        from basic_memory.bases.errors import BasesUnsupportedError

        with pytest.raises(BasesUnsupportedError):
            parse_formula("file.folder.__class__")

    def test_unknown_member_after_folder_refused(self):
        """[ADVERSARIAL] An unknown member call chained on file.folder is refused.

        The security invariant is that the block goes inert (a typed ``BasesError``
        — parse or unsupported), never silently resolved into an executable call.
        ``evil`` is not a closed-table member, so ``file.folder.evil(...)`` is
        rejected before any evaluation.
        """
        with pytest.raises(BasesError):
            parse_formula("file.folder.evil()")

    def test_folder_resolution_never_raises_uncaught(self):
        """A degenerate row yields a typed/None result, never an uncaught error."""
        # Missing path entirely → empty folder, no crash.
        row = {"title": "No Path"}
        try:
            assert _eval("file.folder", row) == ""
        except BasesError:
            pass  # typed degradation acceptable; uncaught is not
