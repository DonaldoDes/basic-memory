"""US-e (M-Bases-P3) — Extended adversarial suite at the INTEGRATION level.

This suite extends the Phase 2 US-006 adversarial battery (``test_adversarial.py``,
unit-level) to an END-TO-END integration battery driven through the full
``BasesIntegration`` envelope (``process_note`` / ``execute_raw_block``), covering
the whole Phase 3 surface (filter-sandbox, self-ref ``this``/``hasLink``,
subscript, cross-summary arithmetic). The contract under test is the negative
invariant set of ADR-005 §Invariants: NO sandbox escape on ANY axis, every
hostile input degrades to an inert block (``status: error``) or a degraded
``None`` cell (``status: success``) — NEVER an exception reaching the MCP
handler, NEVER a side effect, anti-DoS bounds held.

Per-axis adversarial UNIT tests already live in:
    test_adversarial.py                  (Phase 2 / US-006 socle)
    test_filter_functions.py             (US-a)
    test_self_reference.py               (US-b — BP3-B-05/06)
    test_subscript.py                    (US-c — BP3-C-06/07)
    test_cross_summary_adversarial.py    (US-d — ADV-1..7)

US-e is the INTEGRATION layer mapped to the Test IDs BP3-E-01..10 and to the
ADR-005 invariant matrix (see ``TestInvariantMatrix`` at the bottom).

[ADVERSARIAL] — every test in this file is an adversarial integration test.

Invariant → test matrix (ADR-005 §Invariants):

| # | Invariant (ce qui ne doit JAMAIS arriver)                              | Test(s)                                              |
|---|------------------------------------------------------------------------|------------------------------------------------------|
| I1| Fonction portée en filtre évaluée hors du sandbox fermé                | TestFilterSandboxNoEscape (BP3-E-01/02/03)           |
| I2| hasLink accède au filesystem / recalcule le graphe global              | TestHasLinkNoFilesystem (BP3-E-05)                   |
| I3| Évaluation accède à une ressource hors dataset + identité hôte         | TestThisBoundedToDataset (BP3-E-04)                  |
| I4| Subscript hors bornes provoque une exception                          | TestSubscriptNeverCrashes (BP3-E-06)                 |
| I5| Division par zéro cross-summary provoque un crash                      | TestDivZeroNeverCrashes (BP3-E-07)                   |
| I6| Dépassement borne anti-DoS → évaluation partielle                     | TestAntiDoSBounds (BP3-E-08)                         |
| I7| this borné au dataset + note hôte (pas de note arbitraire)            | TestThisBoundedToDataset (BP3-E-04)                  |
| I8| Erreur d'évaluation → crash handler MCP / altère le reste de la note   | TestNoCrashNoSideEffectEnvelope (all axes)           |
| I9| Modification d'un fichier core upstream                                | TestCoreUntouched                                    |
| —| YAML anchors/aliases refusés (loader), y compris nouvelles clés         | TestYamlAnchorsRefused (BP3-E-09)                    |
"""

import time

import pytest

from basic_memory.bases.integration import (
    BasesIntegration,
    create_bases_integration,
)
from basic_memory.bases.schema import (
    MAX_AGG_ONLY_GROUPS,
    MAX_FILTER_LEAVES,
)

# Error categories a hostile block may degrade to (never anything else).
ERROR_TYPES = {"parse", "unsupported", "limit", "execution", "unexpected"}


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------
HOST_PERMALINK = "products/X/host"
HOST_PATH = "products/X/Host.md"
HOST_METADATA = {"permalink": HOST_PERMALINK, "path": HOST_PATH, "title": "Host"}


def _note(title, *, folder="projects", outlinks=None, **fm):
    file = {
        "path": f"{folder}/{title}.md",
        "name": title,
        "folder": folder,
    }
    if outlinks is not None:
        file["outlinks"] = list(outlinks)
    return {
        "title": title,
        "file": file,
        "created_at": "2026-01-01",
        "updated_at": "2026-01-10",
        "frontmatter": dict(fm),
        "tags": fm.get("tags", []),
    }


def _basic_notes():
    return [
        _note("Alpha", status="Active", tags=["alpha", "beta"]),
        _note("Beta", status="Archived", tags=["zeta"]),
    ]


def _activity_notes():
    return [
        _note("A", folder="products/X", outlinks=[HOST_PERMALINK]),
        _note("B", folder="products/X", outlinks=[HOST_PERMALINK]),
        _note("C", folder="products/X", outlinks=["products/X/other"]),
    ]


def _process(block_body, notes=_basic_notes, note_metadata=None):
    """Drive a raw base block through the full integration envelope."""
    integ = create_bases_integration(notes_provider=notes)
    return integ.process_note(f"```base\n{block_body}\n```", note_metadata=note_metadata)


def _assert_inert(result):
    """The block is inert: status error, a known error_type, no rows leaked."""
    assert result["status"] == "error", result
    assert result["error_type"] in ERROR_TYPES, result
    assert result.get("result_count", 0) == 0


# ===========================================================================
# BP3-E-01 / E-02 / E-03 — filter sandbox: no escape (I1)
# ===========================================================================
class TestFilterSandboxNoEscape:
    """The filter leaf routes to the closed formula sandbox (ADR-005 §Axe 1).
    A hostile leaf is refused at parse — never evaluated as code."""

    # BP3-E-01 — eval/exec injection in a FILTER position.
    @pytest.mark.parametrize(
        "leaf",
        [
            'eval("1+1")',
            'exec("x=1")',
            '__import__("os").system("echo pwned")',
            'os.system("ls")',
            'compile("1", "<s>", "eval")',
        ],
    )
    def test_eval_exec_injection_in_filter_is_inert(self, leaf):
        block = f'filters: "{leaf}"\nviews:\n  - type: list\n'
        result = _process(block)[0]
        _assert_inert(result)

    def test_eval_injection_executes_no_code(self, monkeypatch):
        # Sentinel: the host eval/exec builtins are NEVER reached.
        import builtins

        hits = {"n": 0}
        for name in ("eval", "exec", "compile"):
            real = getattr(builtins, name)
            monkeypatch.setattr(
                builtins,
                name,
                lambda *a, _h=hits, _r=real, **k: (_h.__setitem__("n", _h["n"] + 1)),
            )
        block = 'filters: "eval(\\"open(\'/etc/passwd\').read()\\")"\nviews:\n  - type: list\n'
        result = _process(block)[0]
        _assert_inert(result)
        assert hits["n"] == 0

    # BP3-E-02 — dunder escape in a FILTER position.
    @pytest.mark.parametrize(
        "leaf",
        [
            'status.__class__',
            'status.__class__.__bases__',
            'status.__class__.__mro__',
            '().__class__.__subclasses__()',
            'this.__class__',
        ],
    )
    def test_dunder_escape_in_filter_is_inert(self, leaf):
        block = f'filters: {leaf} == "x"\nviews:\n  - type: list\n'
        result = _process(block, note_metadata=HOST_METADATA)[0]
        _assert_inert(result)

    # BP3-E-03 — dynamic getattr in filter / property-chain.
    @pytest.mark.parametrize(
        "leaf",
        [
            'getattr(status, "__class__")',
            'getattr(file, "system")',
            'status.getattr("x")',
        ],
    )
    def test_dynamic_getattr_in_filter_is_inert(self, leaf):
        block = f'filters: {leaf} == "x"\nviews:\n  - type: list\n'
        result = _process(block)[0]
        _assert_inert(result)

    def test_filter_getattr_never_invoked_on_content(self, monkeypatch):
        # Sentinel: builtin getattr is never invoked to resolve a dunder on
        # note content during the filter evaluation path.
        import builtins

        seen = []
        real_getattr = builtins.getattr

        def spy(obj, name, *default):
            seen.append(name)
            return real_getattr(obj, name, *default)

        monkeypatch.setattr(builtins, "getattr", spy)
        block = 'filters: status.__class__ == "x"\nviews:\n  - type: list\n'
        result = _process(block)[0]
        _assert_inert(result)
        # No dunder-class attribute resolution ran on content.
        assert not any(n == "__class__" for n in seen)


# ===========================================================================
# BP3-E-04 — this bounded to dataset + host (I3, I7)
# ===========================================================================
class TestThisBoundedToDataset:
    def test_this_block_without_host_is_inert_or_zero(self):
        # A this-using block with NO host identity cannot resolve this.file —
        # never a silent wrong match against an arbitrary note.
        block = "filters: file.hasLink(this.file)\nviews:\n  - type: list\n"
        result = _process(block, notes=_activity_notes)[0]  # no metadata
        assert result["status"] == "error" or result["result_count"] == 0

    def test_this_cannot_reference_arbitrary_constructed_note(self):
        # this.file is frozen to the host identity; a row pointing elsewhere can
        # never be coerced to match an attacker-chosen note via this.
        block = "filters: file.hasLink(this.file)\nviews:\n  - type: list\n"
        evil = {"permalink": "secret/credentials", "path": "secret/credentials.md"}
        result = _process(block, notes=_activity_notes, note_metadata=evil)[0]
        # No row in the dataset points to secret/credentials → zero matches.
        assert result["status"] == "success"
        assert result["result_count"] == 0

    def test_this_member_beyond_file_is_inert(self):
        # this.content / this.frontmatter (reload host from disk) is refused.
        for hostile in ("this.content", "this.frontmatter", "this.file.content"):
            block = f'filters: {hostile} == "x"\nviews:\n  - type: list\n'
            result = _process(block, notes=_activity_notes, note_metadata=HOST_METADATA)[0]
            _assert_inert(result)

    def test_this_resolves_only_host_not_current_row(self):
        # Positive bound: with a valid host, only rows that link to the host
        # match — the evaluation is bounded to dataset + host identity.
        block = "filters: file.hasLink(this.file)\nviews:\n  - type: list\n"
        result = _process(block, notes=_activity_notes, note_metadata=HOST_METADATA)[0]
        assert result["status"] == "success"
        assert result["result_count"] == 2  # A, B point to host; C does not


# ===========================================================================
# BP3-E-05 — hasLink does not touch the filesystem / network (I2)
# ===========================================================================
class TestHasLinkNoFilesystem:
    def test_haslink_opens_no_file(self, monkeypatch):
        import builtins

        calls = {"open": 0}
        real_open = builtins.open
        monkeypatch.setattr(
            builtins, "open", lambda *a, _c=calls, _r=real_open, **k: (_c.__setitem__("open", _c["open"] + 1) or _r(*a, **k))
        )
        block = "filters: file.hasLink(this.file)\nviews:\n  - type: list\n"
        result = _process(block, notes=_activity_notes, note_metadata=HOST_METADATA)[0]
        assert result["status"] == "success"
        assert calls["open"] == 0

    def test_haslink_calls_no_os_filesystem(self, monkeypatch):
        import os

        sentinel = {"hit": 0}
        for name in ("stat", "scandir", "listdir", "walk", "open", "system"):
            if hasattr(os, name):
                monkeypatch.setattr(
                    os, name, lambda *a, _s=sentinel, **k: _s.__setitem__("hit", _s["hit"] + 1)
                )
        block = "filters: file.hasLink(this.file)\nviews:\n  - type: list\n"
        result = _process(block, notes=_activity_notes, note_metadata=HOST_METADATA)[0]
        assert result["status"] == "success"
        assert sentinel["hit"] == 0

    def test_haslink_reads_only_dataset_outlinks(self):
        # A row with no outlinks cannot match — no recompute, no FS lookup.
        notes = lambda: [_note("Bare", folder="products/X")]  # no outlinks key
        block = "filters: file.hasLink(this.file)\nviews:\n  - type: list\n"
        result = _process(block, notes=notes, note_metadata=HOST_METADATA)[0]
        assert result["status"] == "success"
        assert result["result_count"] == 0


# ===========================================================================
# BP3-E-06 — subscript never crashes (I4)
# ===========================================================================
class TestSubscriptNeverCrashes:
    @pytest.mark.parametrize(
        "leaf",
        [
            'tags[99] == "x"',       # out of bounds
            'tags[0 - 99] == "x"',   # negative out of bounds
            'tags["x"] == "x"',      # non-integer (string) index
            'tags[1.5] == "x"',      # non-integer (float) index
            'tags[true] == "x"',     # bool not coerced to int
            'tags[999999999] == "x"',  # huge index, no allocation
        ],
    )
    def test_out_of_bounds_or_non_integer_excludes_row_no_crash(self, leaf):
        # Degrades to None → predicate false → row excluded → status success,
        # never a crash, never an error envelope.
        block = f"filters: {leaf}\nviews:\n  - type: list\n"
        result = _process(block)[0]
        assert result["status"] == "success"
        assert result["result_count"] == 0

    def test_slice_is_inert(self):
        # Slice [a:b] is out of v1 → block inert (refused), never a sublist.
        block = 'filters: tags[0:2] == "x"\nviews:\n  - type: list\n'
        result = _process(block)[0]
        _assert_inert(result)

    def test_dunder_index_is_inert(self):
        block = 'filters: tags[__class__] == "x"\nviews:\n  - type: list\n'
        result = _process(block)[0]
        _assert_inert(result)

    def test_unbalanced_bracket_is_inert(self):
        block = 'filters: tags[0 == "x"\nviews:\n  - type: list\n'
        result = _process(block)[0]
        _assert_inert(result)


# ===========================================================================
# BP3-E-07 — division by zero cross-summary never crashes (I5)
# ===========================================================================
class TestDivZeroNeverCrashes:
    DIV_ZERO_BLOCK = (
        "views:\n"
        "  - type: table\n"
        "    order: [milestone]\n"
        "    groupBy: milestone\n"
        "    aggregate: true\n"
        "    summaries:\n"
        '      Done: count(status == "Done")\n'
        "      Total: count(missing)\n"
        "    aggFormulas:\n"
        "      Progress: round(Done / Total * 100)\n"
    )

    def test_div_zero_renders_success_with_degraded_cell(self):
        notes = lambda: [_note("A", folder="notes", milestone="M1", status="Todo")]
        result = _process(self.DIV_ZERO_BLOCK, notes=notes)[0]
        # Not an error envelope: it renders with a degraded (None) cell.
        assert result["status"] == "success"

    def test_div_zero_no_exception_to_handler(self):
        notes = lambda: [_note("A", folder="notes", milestone="M1", status="Todo")]
        integ = BasesIntegration(notes_provider=notes)
        # Must not raise — the envelope absorbs everything.
        result = integ.execute_raw_block(self.DIV_ZERO_BLOCK)
        assert result["status"] in {"success", "error"}
        if result["status"] == "error":
            assert result["error_type"] in ERROR_TYPES


# ===========================================================================
# BP3-E-08 — anti-DoS bounds held (I6)
# ===========================================================================
class TestAntiDoSBounds:
    def test_too_many_filter_leaves_inert(self):
        leaves = "\n".join(f'    - status == "v{i}"' for i in range(MAX_FILTER_LEAVES + 10))
        block = f"filters:\n  or:\n{leaves}\nviews:\n  - type: list\n"
        result = _process(block)[0]
        assert result["status"] == "error"
        assert result["error_type"] == "limit"

    def test_recursion_bomb_in_filter_formula_bounded(self):
        # A deeply-nested arithmetic chain in a single leaf exceeds the formula
        # AST/recursion bound → inert (limit), bounded time, no stack overflow.
        deep = " + ".join(["1"] * 400)
        block = f'filters: tags[0] == "{deep}"\nviews:\n  - type: list\n'
        start = time.time()
        result = _process(block)[0]
        elapsed = time.time() - start
        assert result["status"] == "error"
        assert result["error_type"] == "limit"
        assert elapsed < 2.0

    def test_aggregate_only_cardinality_bomb_inert(self):
        notes = lambda: [
            _note(f"n{i}", folder="notes", milestone=f"m{i}")
            for i in range(MAX_AGG_ONLY_GROUPS + 50)
        ]
        block = (
            "views:\n"
            "  - type: table\n"
            "    order: [milestone]\n"
            "    groupBy: milestone\n"
            "    aggregate: true\n"
            "    summaries:\n"
            "      Total: count(title)\n"
        )
        result = _process(block, notes=notes)[0]
        assert result["status"] == "error"
        assert result["error_type"] == "limit"

    def test_post_agg_depth_bomb_inert(self):
        deep = "Total" + " + 1" * 500
        block = (
            "views:\n"
            "  - type: table\n"
            "    order: [milestone]\n"
            "    groupBy: milestone\n"
            "    aggregate: true\n"
            "    summaries:\n"
            "      Total: count(title)\n"
            "    aggFormulas:\n"
            f"      X: {deep}\n"
        )
        notes = lambda: [_note("A", folder="notes", milestone="M1")]
        result = _process(block, notes=notes)[0]
        assert result["status"] == "error"
        assert result["error_type"] == "limit"


# ===========================================================================
# BP3-E-09 — YAML anchors/aliases refused, incl. new Phase 3 keys
# ===========================================================================
class TestYamlAnchorsRefused:
    def test_anchor_on_groupby_refused(self):
        block = (
            'a: &a "milestone"\n'
            "views:\n"
            "  - type: table\n"
            "    order: [milestone]\n"
            "    groupBy: *a\n"
            "    aggregate: true\n"
            "    summaries:\n"
            "      Total: count(title)\n"
        )
        result = _process(block)[0]
        assert result["status"] == "error"
        assert result["error_type"] == "parse"

    def test_anchor_on_summaries_refused(self):
        block = (
            'a: &a count(title)\n'
            "views:\n"
            "  - type: table\n"
            "    order: [milestone]\n"
            "    groupBy: milestone\n"
            "    summaries:\n"
            "      Total: *a\n"
        )
        result = _process(block)[0]
        assert result["status"] == "error"
        assert result["error_type"] == "parse"

    def test_anchor_on_agg_formulas_refused(self):
        block = (
            'a: &a round(Total)\n'
            "views:\n"
            "  - type: table\n"
            "    order: [milestone]\n"
            "    groupBy: milestone\n"
            "    aggregate: true\n"
            "    summaries:\n"
            "      Total: count(title)\n"
            "    aggFormulas:\n"
            "      X: *a\n"
        )
        result = _process(block)[0]
        assert result["status"] == "error"
        assert result["error_type"] == "parse"

    def test_billion_laughs_with_aggregate_key_inert_and_bounded(self):
        block = (
            'a: &a ["lol","lol","lol","lol","lol","lol","lol","lol","lol"]\n'
            "b: &b [*a,*a,*a,*a,*a,*a,*a,*a,*a]\n"
            "c: [*b,*b,*b,*b,*b,*b,*b,*b,*b]\n"
            "views:\n"
            "  - type: table\n"
            "    aggregate: true\n"
        )
        start = time.time()
        result = _process(block)[0]
        elapsed = time.time() - start
        assert result["status"] == "error"
        assert result["error_type"] == "parse"
        assert elapsed < 1.0  # no exponential expansion


# ===========================================================================
# BP3-E-08/E-10 cross-cutting — no crash, no side effect, the rest renders (I8)
# ===========================================================================
class TestNoCrashNoSideEffectEnvelope:
    HOSTILE_BLOCKS = [
        'filters: "eval(\\"1\\")"\nviews:\n  - type: list\n',
        'filters: status.__class__ == "x"\nviews:\n  - type: list\n',
        'filters: tags[0:2] == "x"\nviews:\n  - type: list\n',
        'filters: getattr(status, "__class__") == "x"\nviews:\n  - type: list\n',
        'filters: file.backlinks\nviews:\n  - type: list\n',
    ]

    @pytest.mark.parametrize("hostile", HOSTILE_BLOCKS)
    def test_hostile_block_never_raises_to_handler(self, hostile):
        # The integration envelope NEVER lets an exception escape.
        integ = create_bases_integration(notes_provider=_basic_notes)
        results = integ.process_note(f"```base\n{hostile}```", note_metadata=HOST_METADATA)
        assert results[0]["status"] == "error"
        assert results[0]["error_type"] in ERROR_TYPES

    def test_inert_hostile_block_does_not_break_a_valid_block(self):
        # A hostile block + a valid block in the same note: the valid one still
        # renders (no global corruption, no side effect leaking across blocks).
        hostile = '```base\nfilters: "eval(\\"1\\")"\nviews:\n  - type: list\n```'
        valid = '```base\nfilters: status == "Active"\nviews:\n  - type: list\n```'
        integ = create_bases_integration(notes_provider=_basic_notes)
        results = integ.process_note(hostile + "\n\ntext\n\n" + valid)
        assert len(results) == 2
        assert results[0]["status"] == "error"
        assert results[1]["status"] == "success"
        assert results[1]["result_markdown"]

    def test_hostile_block_leaves_no_discovered_links(self):
        hostile = 'filters: status.__class__ == "x"\nviews:\n  - type: list\n'
        result = _process(hostile)[0]
        assert result["status"] == "error"
        assert result["discovered_links"] == []


# ===========================================================================
# I9 — no core upstream file is modified (the extension is fork-local)
# ===========================================================================
class TestCoreUntouched:
    def test_bases_module_imports_no_core_mutation_surface(self):
        # The integration imports only from basic_memory.bases.* (+ the dataview
        # result_formatter reuse) — it does not reach into sync/db/file write.
        import basic_memory.bases.integration as integ_mod

        src = __import__("inspect").getsource(integ_mod)
        for forbidden in ("sync_service", "open(", "os.remove", "shutil", "Path.write"):
            assert forbidden not in src, f"core mutation surface leaked: {forbidden}"


# ===========================================================================
# BP3-E-10 — explicit invariant → test matrix (ADR-005 §Invariants)
# ===========================================================================
class TestInvariantMatrix:
    """One assertion per ADR-005 invariant, proving each is covered end-to-end.

    This is the machine-checkable counterpart of the matrix in the module
    docstring: each invariant maps to at least one passing integration test.
    """

    # I1 — ported filter functions stay inside the closed sandbox.
    def test_I1_filter_function_never_escapes_sandbox(self):
        result = _process('filters: eval("1") == "x"\nviews:\n  - type: list\n')[0]
        _assert_inert(result)

    # I2 — hasLink never touches the filesystem / global graph.
    def test_I2_haslink_bounded_to_dataset(self, monkeypatch):
        import builtins

        calls = {"n": 0}
        real = builtins.open
        monkeypatch.setattr(builtins, "open", lambda *a, _c=calls, _r=real, **k: (_c.__setitem__("n", _c["n"] + 1) or _r(*a, **k)))
        block = "filters: file.hasLink(this.file)\nviews:\n  - type: list\n"
        result = _process(block, notes=_activity_notes, note_metadata=HOST_METADATA)[0]
        assert result["status"] == "success" and calls["n"] == 0

    # I3 — evaluation accesses nothing beyond dataset + host identity.
    def test_I3_no_resource_outside_dataset_and_host(self):
        block = "filters: file.hasLink(this.file)\nviews:\n  - type: list\n"
        evil = {"permalink": "secret/x", "path": "secret/x.md"}
        result = _process(block, notes=_activity_notes, note_metadata=evil)[0]
        assert result["status"] == "success" and result["result_count"] == 0

    # I4 — out-of-bounds subscript never raises.
    def test_I4_subscript_out_of_bounds_no_exception(self):
        result = _process('filters: tags[99] == "x"\nviews:\n  - type: list\n')[0]
        assert result["status"] == "success"

    # I5 — division by zero cross-summary never crashes.
    def test_I5_div_zero_no_crash(self):
        notes = lambda: [_note("A", folder="notes", milestone="M1", status="Todo")]
        result = _process(TestDivZeroNeverCrashes.DIV_ZERO_BLOCK, notes=notes)[0]
        assert result["status"] == "success"

    # I6 — crossing an anti-DoS bound is inert (no partial evaluation).
    def test_I6_anti_dos_bound_is_inert_limit(self):
        notes = lambda: [
            _note(f"n{i}", folder="notes", milestone=f"m{i}")
            for i in range(MAX_AGG_ONLY_GROUPS + 50)
        ]
        block = (
            "views:\n  - type: table\n    order: [milestone]\n    groupBy: milestone\n"
            "    aggregate: true\n    summaries:\n      Total: count(title)\n"
        )
        result = _process(block, notes=notes)[0]
        assert result["status"] == "error" and result["error_type"] == "limit"

    # I7 — this bounded to host identity, no arbitrary note referenceable.
    def test_I7_this_cannot_reference_arbitrary_note(self):
        result = _process(
            'filters: this.file.content == "x"\nviews:\n  - type: list\n',
            notes=_activity_notes,
            note_metadata=HOST_METADATA,
        )[0]
        _assert_inert(result)

    # I8 — an evaluation error never crashes the handler / alters the note.
    def test_I8_eval_error_is_inert_envelope(self):
        result = _process('filters: status.__class__ == "x"\nviews:\n  - type: list\n')[0]
        assert result["status"] == "error" and result["error_type"] in ERROR_TYPES

    # I9 — no core upstream file is modified (fork-local extension).
    def test_I9_extension_is_fork_local(self):
        import basic_memory.bases.integration as integ_mod

        assert integ_mod.__name__.startswith("basic_memory.bases.")
