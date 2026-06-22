"""Adversarial tests for US-7 (M-Bases-P4): negated function-call filter leaf.

Sensitive zone (constitution §"Parsing markdown / frontmatter (entrée non
fiable)" + US-7 invariants): the negation path parses UNTRUSTED note content.
It must NOT widen the attack surface:

  ADV-1  The negated-inFolder rewrite introduces NO new whitelisted function:
         ``inFolder`` is never admitted to the formula grammar; a bare /
         non-FROM inFolder call stays inert.
  ADV-2  A hostile construct smuggled into a negated function leaf (dunder,
         unknown function, property walk) makes the block inert (typed error),
         never evaluated — the dispatch stays closed.
  ADV-3  The negated subtree predicate evaluates on the DATASET ROW only — no
         filesystem access is introduced by the negation path (sentinel patches
         ``open`` to explode if the render ever touches the OS).
  ADV-4  ``not`` is a grammar token, never a Python keyword handed to eval: the
         compiled WHERE is a closed typed node tree.

Static guard: ``test_no_new_eval_exec_getattr_in_touched_files`` greps the
touched source (``parser.py``, ``filter_leaf.py``) for forbidden primitives.

Marker: [ADVERSARIAL] — these run BEFORE [TDD-GREEN] on this sensitive task.
"""

from __future__ import annotations

import builtins
import re
from pathlib import Path

import pytest

from basic_memory.bases.errors import BasesParseError, BasesUnsupportedError
from basic_memory.bases.integration import create_bases_integration
from basic_memory.bases.parser import BasesParser


def _block(leaf: str) -> str:
    return (
        "filters:\n"
        "  and:\n"
        '    - file.inFolder("products")\n'
        f"    - '{leaf}'\n"
        "views:\n"
        "  - type: table\n"
        "    order:\n"
        "      - file.name\n"
    )


def _dataset() -> list[dict]:
    return [
        {
            "file": {
                "path": "products/X/a.md",
                "name": "a.md",
                "folder": "products/X",
                "outlinks": [],
            },
            "title": "a",
            "type": "note",
        }
    ]


# ===========================================================================
# ADV-1 / ADV-2 — dispatch stays closed: hostile negated leaves are inert
# ===========================================================================
class TestNegationDispatchClosed:
    @pytest.mark.parametrize(
        "leaf",
        [
            "not file.inFolder(__class__)",
            "not file.inFolder(status.__class__)",
            'not evilFunc(file.path, "x")',
            "!evilFunc(file.path)",
            "not contains(file.path, __import__)",
            'not file.inFolder("a").__class__',
            "not file.backlinks",
        ],
    )
    def test_hostile_negated_leaf_is_refused(self, leaf):
        """ADV-1/ADV-2: a hostile negated leaf is refused at parse (block inert)."""
        with pytest.raises((BasesParseError, BasesUnsupportedError)):
            BasesParser.parse(_block(leaf))

    def test_infolder_never_admitted_to_formula_whitelist(self):
        """ADV-1: the negation rewrite does not add ``inFolder`` to the grammar."""
        from basic_memory.bases.formula_parser import ALLOWED_FUNCTIONS

        assert "inFolder" not in ALLOWED_FUNCTIONS

    def test_negated_infolder_compiles_to_closed_typed_nodes(self):
        """ADV-4: a negated inFolder compiles to closed typed nodes only.

        The result is a FormulaLeafNode carrying a closed formula AST whose top
        node is the negation ``(<subtree-test> == False)`` — no raw string, no
        eval surface, ``inFolder`` never re-admitted to the grammar.
        """
        from basic_memory.bases.filter_leaf import FormulaLeafNode
        from basic_memory.bases.formula_ast import FBinOp, FLiteral

        query = BasesParser.parse(_block('not file.inFolder("resources/daily")'))
        node = query.where
        assert isinstance(node, FormulaLeafNode)
        formula = node.formula
        assert isinstance(formula, FBinOp)
        assert formula.op == "=="
        assert isinstance(formula.right, FLiteral)
        assert formula.right.value is False


# ===========================================================================
# ADV-3 — the negated subtree predicate never touches the filesystem
# ===========================================================================
class TestNoFilesystemAccessOnNegatedInFolder:
    def test_negated_infolder_render_does_not_open_any_file(self, monkeypatch):
        """ADV-3: rendering a negated inFolder must not call open()/os.

        ``inFolder`` semantics are a PATH-PREFIX test on the dataset row — never
        a filesystem lookup. The sentinel explodes if the render touches the OS.
        """
        sentinel: list[str] = []
        real_open = builtins.open

        def exploding_open(*args, **kwargs):  # pragma: no cover - must not run
            sentinel.append(str(args[:1]))
            raise AssertionError(f"FS access during negated-inFolder render: {args[:1]!r}")

        monkeypatch.setattr(builtins, "open", exploding_open)
        try:
            integ = create_bases_integration(notes_provider=_dataset)
            r = integ.process_note(
                '```base\nfilters: not file.inFolder("resources/daily")\n'
                "views:\n  - type: table\n    order:\n      - file.path\n```",
                note_metadata=None,
            )[0]
        finally:
            monkeypatch.setattr(builtins, "open", real_open)

        assert sentinel == []
        assert r["status"] == "success", r.get("error")
        # The single products note is not under resources/daily → it survives.
        assert r["result_count"] == 1


# ===========================================================================
# Static guard — no new eval/exec/getattr/__import__ in touched source files
# ===========================================================================
def test_no_new_eval_exec_getattr_in_touched_files():
    """The negation fix introduces no dynamic-execution primitive."""
    root = Path(__file__).resolve().parents[2] / "src" / "basic_memory" / "bases"
    touched = [root / "parser.py", root / "filter_leaf.py"]
    forbidden = re.compile(
        r"(?<![\w.])(?:exec|compile|__import__|getattr)\s*\("
        r"|(?<![\w.])eval\s*\("
    )
    offenders: list[str] = []
    for path in touched:
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if forbidden.search(line):
                offenders.append(f"{path.name}:{lineno}: {stripped}")
    assert offenders == [], "Forbidden dynamic-execution primitive introduced:\n" + "\n".join(
        offenders
    )
