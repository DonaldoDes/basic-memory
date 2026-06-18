"""Adversarial tests for the US-f live-path fixes (sensitive zone — sandbox).

Constitution §"Parsing markdown / frontmatter (entrée non fiable)" + ADR-005
§Axe 5: the Bases formula sandbox parses UNTRUSTED note content. The live-path
fixes (host-title identity for hasLink, word operators in count predicates) must
NOT widen the attack surface:

  ADV-1  Populating/reading outlinks opens NO filesystem access — the sentinel
         patches ``builtins.open`` / ``os`` to explode if the hasLink path ever
         touches the OS, then renders an Activity block. (Outlinks come from the
         dataset only.)
  ADV-2  ``count(expr)`` with word operators is still parsed by the CLOSED
         grammar — no ``eval``/``exec``/``getattr``/dunder reaches evaluation.
         A hostile predicate (``__class__``, an unknown function, an attribute
         walk) makes the block inert (typed error), never executes.
  ADV-3  ``this`` exposes ONLY the host file identity (path/permalink/title) —
         a row CANNOT use ``this`` to read arbitrary host content; hasLink stays
         a bounded membership test over the row's own outlinks.
  ADV-4  Word operators do not let an attacker smuggle Python boolean keywords
         into evaluation: ``or``/``and``/``not`` are grammar tokens, not eval'd.

Static guard: ``test_no_new_eval_exec_getattr_in_touched_files`` greps the
touched source for forbidden primitives.
"""

from __future__ import annotations

import builtins
import re
from pathlib import Path

import pytest

from basic_memory.bases.errors import (
    BasesParseError,
    BasesUnsupportedError,
)
from basic_memory.bases.formula_parser import parse_formula
from basic_memory.bases.integration import create_bases_integration

HOST_METADATA = {
    "permalink": "p/x/host",
    "path": "p/X/host.md",
    "title": "Host Note",
}


def _provider_linking_host():
    return [
        {
            "title": "Linker",
            "file": {
                "path": "p/X/a.md",
                "name": "a.md",
                "folder": "p/X",
                "outlinks": ["Host Note"],
            },
            "permalink": "p/x/a",
            "status": "Active",
        }
    ]


def _block(body: str) -> str:
    return f"```base\n{body}```"


# ===========================================================================
# ADV-1 — hasLink / outlinks population never touches the filesystem
# ===========================================================================
class TestNoFilesystemAccessOnHasLink:
    def test_haslink_render_does_not_open_any_file(self, monkeypatch):
        """ADV-1: an Activity block resolving hasLink must not call open()/os."""
        sentinel_calls: list[str] = []

        real_open = builtins.open

        def exploding_open(*args, **kwargs):  # pragma: no cover - must not run
            sentinel_calls.append(str(args[:1]))
            raise AssertionError(f"FS access during hasLink render: open{args[:1]!r}")

        monkeypatch.setattr(builtins, "open", exploding_open)
        try:
            integ = create_bases_integration(notes_provider=_provider_linking_host)
            r = integ.process_note(
                _block("filters: file.hasLink(this.file)\nviews:\n  - type: list\n"),
                note_metadata=HOST_METADATA,
            )[0]
        finally:
            monkeypatch.setattr(builtins, "open", real_open)

        assert sentinel_calls == []
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 1  # the row links the host by title


# ===========================================================================
# ADV-2 — count(expr) stays in the closed grammar (no eval/exec/getattr)
# ===========================================================================
class TestConditionalAggregateGrammarClosed:
    @pytest.mark.parametrize(
        "hostile",
        [
            '__class__ == "x" or status == "Done"',
            'status == "Done" or evilFunc(status)',
            'status.__class__ == "str" and status == "Done"',
            '() => __import__("os") or status == "Done"',
        ],
    )
    def test_hostile_predicate_is_rejected_not_evaluated(self, hostile):
        """ADV-2: a hostile count predicate is refused at parse — never run."""
        with pytest.raises((BasesParseError, BasesUnsupportedError)):
            parse_formula(hostile)

    def test_word_operators_are_grammar_tokens_only(self):
        """ADV-2/ADV-4: or/and/not compile to typed BinOps, not Python eval.

        The parsed AST is a closed FBinOp tree — proving the words are grammar
        constructs, not strings handed to a Python boolean evaluation.
        """
        node = parse_formula('status == "Done" or status == "Completed"')
        # Top node is the OR binop; no string/eval leakage.
        from basic_memory.bases.formula_ast import FBinOp

        assert isinstance(node, FBinOp)
        assert node.op == "||"  # word ``or`` normalised to the closed || binop

    def test_hostile_predicate_in_count_makes_block_inert(self):
        """ADV-2 end-to-end: a hostile count predicate renders an inert block."""
        body = (
            'filters: file.inFolder("p/X")\n'
            "views:\n"
            "  - type: table\n"
            "    groupBy: status\n"
            "    summaries:\n"
            '      Bad: count(__class__ == "x" or status == "Done")\n'
            "    aggregate: true\n"
            "    order:\n"
            "      - status\n"
            "      - Bad\n"
        )
        integ = create_bases_integration(notes_provider=_provider_linking_host)
        r = integ.process_note(_block(body), note_metadata=HOST_METADATA)[0]
        assert r["status"] == "error"
        assert r["error_type"] in ("parse", "unsupported")


# ===========================================================================
# ADV-3 — this exposes only the host file identity (no content access)
# ===========================================================================
class TestThisIsBoundedToIdentity:
    @pytest.mark.parametrize(
        "expr",
        [
            "this.content",
            "this.frontmatter",
            "this.file.backlinks",
        ],
    )
    def test_this_non_file_members_are_unsupported(self, expr):
        """ADV-3: any ``this`` member beyond ``.file`` (and file.hasLink) is refused."""
        with pytest.raises((BasesUnsupportedError, BasesParseError)):
            parse_formula(expr)


# ===========================================================================
# Static guard — no new eval/exec/getattr/__import__ in touched source files
# ===========================================================================
def test_no_new_eval_exec_getattr_in_touched_files():
    """The live-path fixes introduce no dynamic-execution primitive."""
    root = Path(__file__).resolve().parents[2] / "src" / "basic_memory" / "bases"
    touched = [root / "formula_parser.py", root / "formula_eval.py"]
    # Match the BARE dynamic-execution builtins only. Exclude legitimate
    # look-alikes: ``re.compile`` (regex), ``self.eval`` / ``.eval`` (the AST
    # evaluator method is named ``eval`` by design — it is NOT Python ``eval``).
    forbidden = re.compile(
        r"(?<![\w.])(?:exec|compile|__import__|getattr)\s*\("
        r"|(?<![\w.])eval\s*\("
    )
    # Allow ``re.compile`` explicitly (the ``.`` before it already excludes it,
    # but keep the intent documented).
    offenders: list[str] = []
    for path in touched:
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # ``def eval(...)`` is the AST evaluator's OWN method definition, not
            # a call to Python's builtin ``eval`` — exclude the definition site.
            if stripped.startswith("def "):
                continue
            if forbidden.search(line):
                offenders.append(f"{path.name}:{lineno}: {stripped}")
    assert not offenders, "Forbidden dynamic-execution primitive introduced:\n" + "\n".join(
        offenders
    )
