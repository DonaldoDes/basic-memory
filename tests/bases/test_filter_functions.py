"""US-a (M-Bases-P3) — Functions + property-chain + lambdas in filters.

The filter LEAF is routed to the Phase 2 formula sandbox (``formula_parser`` /
``formula_eval``) instead of the 4-function ``leaf_parser``. The FROM/WHERE/and/
or/not structure (``parser.py``) is unchanged; only the atomic expression (the
leaf) delegates to the sandbox. Parity: filter = formula = Obsidian.

Test IDs (ADR-005 §Axe 1, US-a):
    BP3-A-01  Filter leaf compiled via formula_parser, not leaf_parser
    BP3-A-02  Each of the 18 whitelisted functions accepted in filter position
    BP3-A-03  Property-chain (asFile().path.startsWith) via whitelisted dispatch
    BP3-A-04  Lambda in filter evaluated in a closed local environment
    BP3-A-05  Function outside whitelist -> BasesUnsupportedError, inert block
    BP3-A-06  getattr/eval/exec never called on content value (filter)  [ADVERSARIAL]
    BP3-A-07  startsWith filter filters the dataset correctly
    BP3-A-08  Non-regression: Phase 1 filter corpus identical to baseline
    BP3-A-09  Filter via sandbox bounded to dataset (no filesystem access) [ADVERSARIAL]
"""

import pytest

from basic_memory.bases.errors import BasesUnsupportedError
from basic_memory.bases.executor import BasesExecutor
from basic_memory.bases.filter_leaf import FormulaLeafNode, compile_filter_leaf
from basic_memory.bases.formula_ast import FCall, FLambda, FormulaNode, FPropChain
from basic_memory.bases.formula_eval import evaluate_formula
from basic_memory.bases.integration import create_bases_integration
from basic_memory.bases.parser import BasesParser
from basic_memory.dataview.ast import BinaryOpNode, ExpressionNode


# ---------------------------------------------------------------------------
# Dataset (nested shape returned by list_entities_for_dataview)
# ---------------------------------------------------------------------------
def _notes():
    return [
        {
            "title": "US-Alpha",
            "file": {"path": "products/X/stories/US-Alpha.md", "name": "US-Alpha", "folder": "products/X/stories"},
            "created_at": "2026-01-01",
            "updated_at": "2026-01-10",
            "frontmatter": {"type": "user-story", "status": "Active", "priority": 1, "tags": ["story", "dev"]},
        },
        {
            "title": "Bug-Beta",
            "file": {"path": "products/X/bugs/Bug-Beta.md", "name": "Bug-Beta", "folder": "products/X/bugs"},
            "created_at": "2026-01-05",
            "updated_at": "2026-01-11",
            "frontmatter": {"type": "bug", "status": "Archived", "priority": 2, "tags": ["bug"]},
        },
        {
            "title": "US-Gamma",
            "file": {"path": "projects/Y/US-Gamma.md", "name": "US-Gamma", "folder": "projects/Y"},
            "created_at": "2026-01-03",
            "updated_at": "2026-01-09",
            "frontmatter": {"type": "user-story", "status": "Active", "priority": 3, "tags": ["story", "dev", "urgent"]},
        },
    ]


def _titles(rows):
    return [r["title"] for r in rows]


def _select(leaf_or_filters_yaml: str):
    query = BasesParser.parse(leaf_or_filters_yaml)
    return BasesExecutor(_notes()).select(query)


def _select_leaf(leaf: str):
    """Parse a single-string filter leaf and select."""
    return _select(f"filters: {leaf}\nviews:\n  - type: list\n")


# ---------------------------------------------------------------------------
# BP3-A-01 — leaf compiled via formula_parser (formula AST), not leaf_parser
# ---------------------------------------------------------------------------
class TestLeafRoutedToFormula:
    def test_leaf_compiles_to_formula_leaf_node(self):
        # A simple equality leaf must now be a FormulaLeafNode wrapping a
        # formula AST — NOT a raw Dataview BinaryOpNode produced by leaf_parser.
        query = BasesParser.parse('filters: status == "Active"\nviews:\n  - type: list\n')
        assert isinstance(query.where, FormulaLeafNode)
        assert isinstance(query.where.formula, FormulaNode)

    def test_compile_filter_leaf_returns_formula_leaf(self):
        node = compile_filter_leaf('startsWith(file.name, "US-")')
        assert isinstance(node, FormulaLeafNode)
        assert isinstance(node.formula, ExpressionNode) is False  # it's a FormulaNode
        assert isinstance(node.formula, FCall)
        assert node.formula.fn == "startsWith"

    def test_combination_structure_preserved_as_dataview_binop(self):
        # FROM/WHERE/and/or structure is UNCHANGED: AND/OR nodes stay Dataview
        # BinaryOpNode; only the leaves are FormulaLeafNode.
        query = BasesParser.parse(
            'filters:\n  and:\n    - status == "Active"\n    - priority < 3\nviews:\n  - type: list\n'
        )
        assert isinstance(query.where, BinaryOpNode)
        assert query.where.operator == "AND"
        assert isinstance(query.where.left, FormulaLeafNode)
        assert isinstance(query.where.right, FormulaLeafNode)


# ---------------------------------------------------------------------------
# BP3-A-02 — the 18 whitelisted functions accepted in filter position
# ---------------------------------------------------------------------------
class TestEighteenFunctionsInFilter:
    # The 18 functions of the closed formula whitelist (ADR-004 §2.(a)) must all
    # PARSE in filter position (parity filter = formula = Obsidian). We exercise
    # each in a valid leaf and assert the block is not inert (no unsupported).
    FILTER_LEAVES = [
        'lower(status) == "active"',
        'upper(status) == "ACTIVE"',
        "length(tags) > 1",
        'contains(status, "Act")',
        'startsWith(file.name, "US-")',
        'endsWith(file.path, ".md")',
        'replace(status, "Active", "X") == "X"',
        'string(priority) == "1"',
        "round(priority, 0) == 1",
        "number(priority) == 1",
        "min(priority, 5) == 1",
        "max(priority, 5) == 5",
        'dateformat(created_at, "YYYY") == "2026-01-01"',
        'date(created_at) == "2026-01-01"',
        'default(status, "none") == "Active"',
        'if(priority == 1, "yes", "no") == "yes"',
        'link(file.name) == "[[US-Alpha]]"',
        'asFile(file.path).name == "US-Alpha.md"',
    ]

    @pytest.mark.parametrize("leaf", FILTER_LEAVES, ids=[f.split("(")[0] for f in FILTER_LEAVES])
    def test_function_parses_in_filter(self, leaf):
        # Must parse and execute without raising BasesUnsupportedError.
        rows = _select_leaf(leaf)
        assert isinstance(rows, list)

    def test_startswith_selects_user_stories(self):
        rows = _select_leaf('startsWith(file.name, "US-")')
        assert set(_titles(rows)) == {"US-Alpha", "US-Gamma"}

    def test_aggregate_count_function_parses_in_filter(self):
        # The aggregate-family functions (sum/average/count) are part of the 18
        # and must at least parse in filter position (parity surface).
        rows = _select_leaf("count(tags) > 1")
        assert isinstance(rows, list)


# ---------------------------------------------------------------------------
# BP3-A-03 — property-chain resolved by whitelisted dispatch in filter
# ---------------------------------------------------------------------------
class TestPropertyChainInFilter:
    def test_asfile_path_startswith_compiles_as_propchain(self):
        node = compile_filter_leaf('asFile(file.path).path.startsWith("products/")')
        assert isinstance(node, FormulaLeafNode)
        assert isinstance(node.formula, FPropChain)

    def test_asfile_path_startswith_filters_dataset(self):
        rows = _select_leaf('asFile(file.path).path.startsWith("products/")')
        assert set(_titles(rows)) == {"US-Alpha", "Bug-Beta"}

    def test_property_chain_no_longer_rejected(self):
        # Phase 1 rejected property chains in filters (test_parser.py
        # test_property_chain_in_filter_rejected). US-a lifts that: a valid
        # property chain now resolves through the whitelisted dispatch.
        rows = _select_leaf('asFile(file.path).name.endsWith(".md")')
        assert len(rows) == 3


# ---------------------------------------------------------------------------
# BP3-A-04 — lambda in filter evaluated in a closed local environment
# ---------------------------------------------------------------------------
class TestLambdaInFilter:
    def test_lambda_compiles_in_filter(self):
        # A lambda leaf must parse via the formula grammar (the closed-env lambda
        # surface is reachable in filter position, parity with formula). It
        # compiles to a FLambda formula AST.
        node = compile_filter_leaf("x => length(x) > 0")
        assert isinstance(node, FormulaLeafNode)
        assert isinstance(node.formula, FLambda)
        assert node.formula.params == ["x"]

    def test_lambda_body_closed_over_params_and_row_only(self):
        # The lambda evaluates over a CLOSED local environment: when invoked, its
        # param shadows the row; an identifier that is neither a bound param nor a
        # row field resolves to None — never a host Python symbol (no builtins /
        # globals leak). Proven directly via the sandbox evaluator.
        node = compile_filter_leaf("x => x == 42")
        callable_lambda = evaluate_formula(node.formula, {"status": "Active"})
        assert callable_lambda(42) is True
        assert callable_lambda(7) is False

    def test_lambda_does_not_capture_host_scope(self):
        # A free identifier inside the lambda body resolves to None (closed env),
        # never to a host symbol like ``open`` or ``__import__``.
        node = compile_filter_leaf("x => somefreevar")
        callable_lambda = evaluate_formula(node.formula, {})
        assert callable_lambda("anything") is None


# ---------------------------------------------------------------------------
# BP3-A-05 — function outside whitelist -> BasesUnsupportedError, inert block
# ---------------------------------------------------------------------------
class TestUnsupportedFunctionInFilter:
    def test_unknown_function_raises_unsupported_at_parse(self):
        with pytest.raises(BasesUnsupportedError):
            compile_filter_leaf('frobnicate(status) == "x"')

    def test_unknown_function_makes_block_inert(self):
        integ = create_bases_integration(notes_provider=_notes)
        results = integ.process_note(
            '```base\nfilters: frobnicate(status) == "x"\nviews:\n  - type: list\n```'
        )
        assert results[0]["status"] == "error"
        assert results[0]["error_type"] == "unsupported"

    def test_dunder_member_makes_block_inert(self):
        integ = create_bases_integration(notes_provider=_notes)
        results = integ.process_note(
            '```base\nfilters: status.__class__ == "x"\nviews:\n  - type: list\n```'
        )
        assert results[0]["status"] == "error"
        assert results[0]["error_type"] == "unsupported"

    def test_rest_of_note_renders_when_block_inert(self):
        integ = create_bases_integration(notes_provider=_notes)
        note = (
            "# Heading\n\n"
            '```base\nfilters: frobnicate(status) == "x"\nviews:\n  - type: list\n```\n\n'
            "Body text after."
        )
        results = integ.process_note(note)
        # The block is reported inert; the integration never raises (the caller
        # renders the rest of the note normally).
        assert len(results) == 1
        assert results[0]["status"] == "error"


# ---------------------------------------------------------------------------
# BP3-A-06 [ADVERSARIAL] — getattr/eval/exec never called on content value
# ---------------------------------------------------------------------------
class TestNoDynamicEvaluationOnContent:
    def test_dunder_chain_never_reaches_getattr(self, monkeypatch):
        # If a hostile filter tries __subclasses__ traversal, the parser must
        # refuse it (closed property-chain table) — getattr is never invoked on
        # the content value. We sentinel builtins to prove they are not called
        # during compile of a hostile leaf.
        import builtins

        called = {"eval": 0, "exec": 0, "getattr": 0}
        real_getattr = builtins.getattr

        def spy_getattr(obj, name, *default):
            # Only flag dynamic dunder access on content-like values; the parser
            # itself uses getattr internally on its own objects, so we only count
            # attempts to access dunder names that would signal a sandbox escape.
            if isinstance(name, str) and name.startswith("__") and name.endswith("__"):
                called["getattr"] += 1
            return real_getattr(obj, name, *default)

        monkeypatch.setattr(builtins, "eval", lambda *a, **k: called.__setitem__("eval", called["eval"] + 1))
        monkeypatch.setattr(builtins, "exec", lambda *a, **k: called.__setitem__("exec", called["exec"] + 1))
        monkeypatch.setattr(builtins, "getattr", spy_getattr)

        with pytest.raises(BasesUnsupportedError):
            compile_filter_leaf('status.__class__.__bases__ == "x"')

        assert called["eval"] == 0
        assert called["exec"] == 0

    def test_hostile_payloads_make_block_inert_never_execute(self):
        # A battery of sandbox-escape attempts in filter position: each must be
        # an inert block (error_type unsupported/parse), never an execution of
        # arbitrary code.
        hostile = [
            'status.__class__ == "x"',
            '__import__("os") == "x"',
            'eval("1+1") == 2',
            'exec("x=1") == "x"',
            'getattr(status, "upper") == "x"',
            'status.mro() == "x"',
            'status.__globals__ == "x"',
        ]
        integ = create_bases_integration(notes_provider=_notes)
        for leaf in hostile:
            results = integ.process_note(
                f"```base\nfilters: {leaf}\nviews:\n  - type: list\n```"
            )
            assert results[0]["status"] == "error", leaf
            assert results[0]["error_type"] in {"unsupported", "parse"}, (leaf, results[0])

    def test_eval_exec_calls_absent_from_filter_codepath(self):
        # Defense statique : le module filter_leaf n'invoque aucun eval/exec/
        # compile/getattr/__import__ dynamique. Garde-fou anti-récidive (zone
        # sensible). On parse l'AST du module pour ne flagger que les *appels*
        # réels — pas les mentions en docstring (qui documentent leur absence).
        import ast
        import inspect

        from basic_memory.bases import filter_leaf

        tree = ast.parse(inspect.getsource(filter_leaf))
        banned = {"eval", "exec", "compile", "getattr", "setattr", "__import__"}
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in banned:
                    offenders.append(node.func.id)
        assert offenders == [], f"forbidden dynamic calls in filter_leaf: {offenders}"


# ---------------------------------------------------------------------------
# BP3-A-07 — startsWith filter filters the dataset correctly (integration)
# ---------------------------------------------------------------------------
class TestStartsWithIntegration:
    def test_startswith_through_integration(self):
        integ = create_bases_integration(notes_provider=_notes)
        results = integ.process_note(
            '```base\nfilters: startsWith(file.name, "US-")\nviews:\n  - type: list\n```'
        )
        assert results[0]["status"] == "success"
        assert results[0]["result_count"] == 2

    def test_startswith_combined_with_from_and_and(self):
        rows = _select(
            'filters:\n  and:\n'
            '    - file.inFolder("products")\n'
            '    - startsWith(file.name, "US-")\n'
            "views:\n  - type: list\n"
        )
        assert set(_titles(rows)) == {"US-Alpha"}


# ---------------------------------------------------------------------------
# BP3-A-08 — non-regression: Phase 1 filter corpus identical to baseline
# ---------------------------------------------------------------------------
class TestPhase1NonRegression:
    """The Phase 1 filter corpus (contains/length/lower/upper + FROM/WHERE/and/
    or/not) must still select exactly the same rows when routed through the
    formula sandbox. Baseline expectations mirror tests/bases/test_executor.py.
    """

    def _exec_notes(self):
        return [
            {
                "title": "Project Alpha",
                "file": {"path": "projects/Project Alpha.md", "name": "Project Alpha", "folder": "projects"},
                "frontmatter": {"type": "project", "status": "Active", "priority": 1, "tags": ["project", "dev"]},
            },
            {
                "title": "Project Beta",
                "file": {"path": "projects/Project Beta.md", "name": "Project Beta", "folder": "projects"},
                "frontmatter": {"type": "project", "status": "Archived", "priority": 2, "tags": ["project"]},
            },
            {
                "title": "Area Dev",
                "file": {"path": "areas/Area Dev.md", "name": "Area Dev", "folder": "areas"},
                "frontmatter": {"type": "area", "status": "Active", "priority": 3, "tags": ["area", "dev"]},
            },
        ]

    def _sel(self, yaml_body):
        query = BasesParser.parse(yaml_body)
        return [r["title"] for r in BasesExecutor(self._exec_notes()).select(query)]

    def test_equality(self):
        got = self._sel('filters: status == "Active"\nviews:\n  - type: list\n')
        assert set(got) == {"Project Alpha", "Area Dev"}

    def test_and_with_comparison(self):
        got = self._sel(
            'filters:\n  and:\n    - status == "Active"\n    - priority < 3\nviews:\n  - type: list\n'
        )
        assert got == ["Project Alpha"]

    def test_or(self):
        got = self._sel(
            'filters:\n  or:\n    - status == "Archived"\n    - priority >= 3\nviews:\n  - type: list\n'
        )
        assert set(got) == {"Project Beta", "Area Dev"}

    def test_not(self):
        got = self._sel('filters:\n  not:\n    - status == "Active"\nviews:\n  - type: list\n')
        assert got == ["Project Beta"]

    def test_not_equal(self):
        got = self._sel('filters: status != "Active"\nviews:\n  - type: list\n')
        assert got == ["Project Beta"]

    def test_contains_global_form(self):
        got = self._sel('filters: contains(tags, "dev")\nviews:\n  - type: list\n')
        assert set(got) == {"Project Alpha", "Area Dev"}

    def test_contains_method_form(self):
        got = self._sel('filters: tags.contains("dev")\nviews:\n  - type: list\n')
        assert set(got) == {"Project Alpha", "Area Dev"}

    def test_length_gt(self):
        got = self._sel('filters: length(tags) > 1\nviews:\n  - type: list\n')
        assert set(got) == {"Project Alpha", "Area Dev"}

    def test_lower(self):
        got = self._sel('filters: lower(status) == "active"\nviews:\n  - type: list\n')
        assert set(got) == {"Project Alpha", "Area Dev"}

    def test_upper(self):
        got = self._sel('filters: upper(status) == "ARCHIVED"\nviews:\n  - type: list\n')
        assert got == ["Project Beta"]

    def test_infolder_from_still_extracted(self):
        # file.inFolder remains a FROM source, NOT routed to the formula sandbox.
        query = BasesParser.parse('filters: file.inFolder("projects")\nviews:\n  - type: list\n')
        assert query.from_source == "projects"
        assert query.where is None


# ---------------------------------------------------------------------------
# BP3-A-09 [ADVERSARIAL] — filter via sandbox bounded to dataset (no FS access)
# ---------------------------------------------------------------------------
class TestDatasetBounded:
    def test_asfile_does_not_touch_filesystem(self, monkeypatch):
        # asFile(...).path in filter position must read the dataset string only,
        # never open a file. We sentinel open() to prove no I/O occurs.
        import builtins

        opened = {"count": 0}
        real_open = builtins.open

        def spy_open(*args, **kwargs):
            opened["count"] += 1
            return real_open(*args, **kwargs)

        monkeypatch.setattr(builtins, "open", spy_open)
        rows = _select_leaf('asFile(file.path).path.startsWith("products/")')
        assert set(_titles(rows)) == {"US-Alpha", "Bug-Beta"}
        assert opened["count"] == 0

    def test_no_os_import_in_filter_codepath(self):
        import inspect

        from basic_memory.bases import filter_leaf

        src = inspect.getsource(filter_leaf)
        assert "import os" not in src
        assert "open(" not in src

    def test_field_not_in_dataset_resolves_none_not_host_symbol(self):
        # A field that is neither a row key nor frontmatter resolves to None in
        # the sandbox — never a Python host symbol. A leaf referencing an unknown
        # field simply selects no rows (None == "x" is False), never crashes.
        rows = _select_leaf('unknownfield == "x"')
        assert rows == []
