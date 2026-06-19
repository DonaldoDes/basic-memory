"""US-5 (M-Bases-P4) — LIVE reconciliation of the "acquired-in-P3" operators.

BP4-E-06 / BP4-E-07 (ADR-006 §"Stratégie de vérification", §Invariants).

The normative lesson of P3: an operator's PRESENCE in the source code is NOT
proof it works. A builder once observed ``!`` failing in LIVE rendering
(``Unexpected character '!'``) while ``_parse_not`` existed statically. The only
admissible evidence is a test on the REAL MCP render path
(``BasesIntegration.process_note`` → ``BasesExecutor`` via a provider faithful to
``knowledge_router.list_entities_for_dataview``). This file LIVE-reconciles every
P3 operator the milestone presumes acquired:

  BP4-E-06  Negation ``!`` / ``not`` — every admissible authoring form is
            LIVE-verified, AND the YAML-tag pitfall of a BARE leading ``!`` is
            pinned as a faithful live behaviour (it is a YAML semantic, not a
            Bases-executor bug — see ``TestNegationYamlTagPitfall``).
  BP4-E-07  Subscript ``[index]``, ``this`` / ``file.hasLink(this.file)``, and
            property-chains / ``link()`` in a FILTER — each LIVE-verified on the
            real path, never by static source reading.

Fidelity: the dataset is built by the canonical ``build_dataview_dataset`` (the
SAME builder as ``test_live_path_integration`` / ``test_live_path_body_relations``)
— a hand-fabricated dict bypassing the provider does NOT count for AC.

==========================================================================
RECONCILIATION FINDING (BP4-E-06) — recorded faithfully, NOT masked
==========================================================================
A BARE leading ``!`` in a YAML scalar value (``filters: !(status == "Done")``)
is intercepted by the YAML loader as a *tag* sigil BEFORE the Bases parser ever
sees it → ``Invalid YAML: could not determine a constructor for the tag '!...'``.

This is NOT the historical ``Unexpected character '!'`` (which was a Bases-leaf
tokenizer gap, since fixed — ``leaf_parser.py`` handles ``!``). It is a YAML-level
constraint: the executor is never reached. The ADMISSIBLE authoring forms — all
LIVE-verified below — are:
  - the WORD operator ``not status == "Done"``  (bare, works)
  - the ``not:`` YAML mapping                    (works)
  - a QUOTED leaf ``"!(status == \"Done\")"``    (works; quoting defeats the tag)

No skip / xfail / weakened assertion is used: the bare-``!`` pitfall is asserted
as the faithful live behaviour, and the three working forms prove negation is
live. Per the task contract, this finding is surfaced in the Self-Attestation.
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


def _run(body: str, entities: list[_Entity], host: dict | None = None) -> dict:
    integ = create_bases_integration(notes_provider=lambda: build_dataview_dataset(entities))
    results = integ.process_note(_block(body), note_metadata=host)
    assert len(results) == 1
    return results[0]


def _status_entities() -> list[_Entity]:
    """Two notes with distinct ``status`` for negation/equality reconciliation."""
    return [
        _Entity(
            title="ActiveOne",
            file_path="projects/active.md",
            permalink="projects/active",
            note_type="project",
            metadata={"status": "Active", "tags": ["alpha", "beta"]},
        ),
        _Entity(
            title="DoneOne",
            file_path="projects/done.md",
            permalink="projects/done",
            note_type="project",
            metadata={"status": "Done", "tags": ["gamma"]},
        ),
    ]


# ===========================================================================
# BP4-E-06 — negation !/not LIVE-verified on the real render path
# ===========================================================================
class TestNegationLiveReconciliation:
    """Every admissible negation form is verified through process_note."""

    def test_word_operator_not_bare_renders(self):
        """BP4-E-06: ``not status == "Done"`` (bare word operator) excludes the
        Done row on the live path — the historical ``!`` failure does not recur."""
        r = _run(
            'filters: not status == "Done"\nviews:\n  - type: list\n',
            _status_entities(),
        )
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 1
        assert {row.get("title") for row in r["results"]} == {"ActiveOne"}

    def test_not_yaml_mapping_renders(self):
        """BP4-E-06: the ``not:`` YAML mapping form negates an AND of leaves."""
        body = 'filters:\n  not:\n    - status == "Done"\nviews:\n  - type: list\n'
        r = _run(body, _status_entities())
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 1
        assert {row.get("title") for row in r["results"]} == {"ActiveOne"}

    def test_quoted_bang_leaf_renders(self):
        """BP4-E-06: a QUOTED ``"!(...)"`` leaf reaches the Bases tokenizer (the
        quotes defeat the YAML tag sigil) and the ``!`` negates correctly LIVE.

        This is the exact operator that historically failed with
        ``Unexpected character '!'`` — here it renders, proving the leaf
        tokenizer handles ``!`` on the real path."""
        body = 'filters: "!(status == \\"Done\\")"\nviews:\n  - type: list\n'
        r = _run(body, _status_entities())
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 1
        assert {row.get("title") for row in r["results"]} == {"ActiveOne"}


class TestNegationYamlTagPitfall:
    """BP4-E-06 finding: a BARE leading ``!`` is a YAML-tag pitfall, pinned."""

    def test_bare_bang_is_yaml_tag_error_not_executor_reached(self):
        """BP4-E-06 (recorded finding): ``filters: !(status == "Done")`` with a
        BARE leading ``!`` is intercepted by YAML as a tag → an inert parse error
        envelope (``error_type`` parse). The executor is NEVER reached, so this is
        a YAML authoring constraint, NOT a Bases negation bug.

        Pinned faithfully (no skip/xfail): the live behaviour is an inert block
        with a YAML-tag diagnostic, and the WORKING forms above prove negation is
        live. Authoring guidance: quote the leaf or use ``not`` / the ``not:``
        mapping."""
        r = _run(
            'filters: !(status == "Done")\nviews:\n  - type: list\n',
            _status_entities(),
        )
        assert r["status"] == "error"
        # The diagnostic is a YAML-tag error (constructor for tag '!...'),
        # distinct from the historical Bases ``Unexpected character '!'``.
        err = (r.get("error") or "").lower()
        assert "tag" in err or "yaml" in err
        assert "unexpected character" not in err


# ===========================================================================
# BP4-E-07 — subscript / this·hasLink / property-chains·link() LIVE-verified
# ===========================================================================
class TestSubscriptFilterLiveReconciliation:
    """Subscript ``[index]`` in a FILTER, on the real render path."""

    def test_subscript_index_filter_selects_row(self):
        """BP4-E-07: ``tags[0] == "alpha"`` selects only the row whose first tag
        is ``alpha`` — subscript in a filter is live."""
        r = _run(
            'filters: tags[0] == "alpha"\nviews:\n  - type: list\n',
            _status_entities(),
        )
        assert r["status"] == "success", r.get("error")
        assert {row.get("title") for row in r["results"]} == {"ActiveOne"}

    def test_subscript_out_of_bounds_excludes_row_no_crash(self):
        """BP4-E-07: an out-of-bounds subscript in a filter yields None → the row
        is excluded, never a crash, on the live path."""
        r = _run(
            'filters: tags[5] == "alpha"\nviews:\n  - type: list\n',
            _status_entities(),
        )
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 0


class TestThisHasLinkLiveReconciliation:
    """``this`` / ``file.hasLink(this.file)`` on the real render path."""

    def _backlink_entities(self) -> list[_Entity]:
        return [
            _Entity(
                title="Linker",
                file_path="areas/Area/Linker.md",
                permalink="areas/area/linker",
                outgoing_relations=[
                    _Rel(to_name=HOST_TITLE, to_permalink=HOST_PERMALINK, relation_type="links_to")
                ],
            ),
            _Entity(
                title="NonLinker",
                file_path="areas/Area/NonLinker.md",
                permalink="areas/area/nonlinker",
                outgoing_relations=[
                    _Rel(to_name="Other", to_permalink="other/node", relation_type="links_to")
                ],
            ),
        ]

    def test_haslink_this_file_renders_backlinks_live(self):
        """BP4-E-07: ``file.hasLink(this.file)`` renders the rows linking the host
        (by permalink or title) — ``this`` is wired through process_note live."""
        r = _run(
            "filters: file.hasLink(this.file)\nviews:\n  - type: list\n",
            self._backlink_entities(),
            host=HOST_METADATA,
        )
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 1
        assert {row.get("title") for row in r["results"]} == {"Linker"}

    def test_haslink_without_host_does_not_silently_match(self):
        """BP4-E-07: with NO host metadata, ``this.file`` cannot resolve → the
        block is inert OR matches zero rows, never a silent wrong match."""
        r = _run(
            "filters: file.hasLink(this.file)\nviews:\n  - type: list\n",
            self._backlink_entities(),
            host=None,
        )
        assert r["status"] == "error" or r["result_count"] == 0


class TestPropertyChainAndLinkFilterLiveReconciliation:
    """Property-chains and ``link()`` in a FILTER, on the real render path."""

    def _folder_entities(self) -> list[_Entity]:
        return [
            _Entity(
                title="ProjNote",
                file_path="projects/p.md",
                permalink="projects/p",
                note_type="project",
            ),
            _Entity(
                title="AreaNote",
                file_path="areas/a.md",
                permalink="areas/a",
                note_type="area",
            ),
        ]

    def test_property_chain_startswith_filter_live(self):
        """BP4-E-07: ``file.path.startsWith("projects")`` selects the projects row
        — a property-chain function in a filter evaluates live."""
        r = _run(
            'filters: file.path.startsWith("projects")\nviews:\n  - type: list\n',
            self._folder_entities(),
        )
        assert r["status"] == "success", r.get("error")
        assert {row.get("title") for row in r["results"]} == {"ProjNote"}

    def test_subscript_on_chain_result_filter_live(self):
        """BP4-E-07: ``file.folder[0] == "p"`` — subscript on a property-chain
        result, in a filter, evaluates live (projects → "p")."""
        r = _run(
            'filters: file.folder[0] == "p"\nviews:\n  - type: list\n',
            self._folder_entities(),
        )
        assert r["status"] == "success", r.get("error")
        assert {row.get("title") for row in r["results"]} == {"ProjNote"}

    def test_link_function_in_filter_evaluates_without_crash_live(self):
        """BP4-E-07: ``link(...)`` is a closed-grammar function; used in a filter
        it evaluates on the live path WITHOUT a crash (it renders a wikilink
        string, so a ``hasLink(link(...))`` membership yields a defined boolean,
        not an exception).

        ``link()`` is a DISPLAY function (``[[name]]``), so ``hasLink`` against it
        does not match a host identity — the admissible live evidence is that the
        block parses and evaluates to a typed success/empty result, never a
        crash or an inert parse error."""
        r = _run(
            'filters: file.hasLink(link("projects/p"))\nviews:\n  - type: list\n',
            self._folder_entities(),
        )
        # The grammar accepts link() and the executor runs it: a typed success
        # envelope (membership semantics may legitimately yield 0). The point is
        # the live path neither crashes nor reports a parse/unsupported error.
        assert r["status"] == "success", r.get("error")
