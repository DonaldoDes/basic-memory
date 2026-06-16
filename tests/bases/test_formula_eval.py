"""Unit + adversarial tests for the sandboxed formula interpreter (US-003).

Covers ADR-004 §2 (6 pillars) and the AC Gherkin of US-003:
- per-type isinstance dispatch (no dynamic dispatch)
- function outside whitelist -> unsupported (inert block)
- property-chain member outside the closed table -> unsupported (no getattr)
- lambda evaluated in a closed local environment (no host capture)
- asFile().path -> row["file.path"], never the filesystem
- recursion / iteration / time bounds -> limit
- unexpected error -> inert block, never an MCP crash

Plus adversarial evaluation tests: sandbox-escape attempts via property chain
(__class__/__globals__), lambda capturing host env, hostile function, deep
evaluation recursion, and a tampered asFile().path.
"""

import builtins

import pytest

from basic_memory.bases.errors import (
    BasesError,
    BasesLimitError,
    BasesUnsupportedError,
)
from basic_memory.bases.formula_ast import (
    FBinOp,
    FCall,
    FField,
    FLambda,
    FLiteral,
    FormulaNode,
    FPropChain,
)
from basic_memory.bases.formula_eval import (
    FormulaEvaluator,
    error_type_for,
    evaluate_formula,
    safe_evaluate_formula,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def row() -> dict:
    """A dataset row as produced by the executor (file.path is present)."""
    return {
        "title": "Note A",
        "file.link": "[[Note A]]",
        "file.path": "products/basic-memory/Note A.md",
        "frontmatter": {"status": "Active", "count": 3},
        "status": "Active",
    }


# --------------------------------------------------------------------------- #
# Literals / fields / binops — nominal tree-walk
# --------------------------------------------------------------------------- #
def test_literal_returns_value(row):
    assert evaluate_formula(FLiteral(value="hello"), row) == "hello"
    assert evaluate_formula(FLiteral(value=42), row) == 42
    assert evaluate_formula(FLiteral(value=None), row) is None


def test_field_resolves_from_row_only(row):
    assert evaluate_formula(FField(name="status"), row) == "Active"
    assert evaluate_formula(FField(name="file.path"), row) == ("products/basic-memory/Note A.md")
    # Unknown field -> None (never a crash, never a filesystem lookup).
    assert evaluate_formula(FField(name="does_not_exist"), row) is None


def test_binop_arithmetic_and_comparison(row):
    assert evaluate_formula(FBinOp("+", FLiteral(2), FLiteral(3)), row) == 5
    assert evaluate_formula(FBinOp("*", FLiteral(2), FLiteral(4)), row) == 8
    assert evaluate_formula(FBinOp("==", FLiteral("a"), FLiteral("a")), row) is True
    assert evaluate_formula(FBinOp("<", FLiteral(1), FLiteral(2)), row) is True
    assert evaluate_formula(FBinOp(">=", FLiteral(2), FLiteral(2)), row) is True


def test_binop_logical_short_circuit(row):
    assert evaluate_formula(FBinOp("&&", FLiteral(True), FLiteral(False)), row) is False
    assert evaluate_formula(FBinOp("||", FLiteral(False), FLiteral(True)), row) is True


def test_binop_division_by_zero_is_typed_not_python_crash(row):
    # Division by zero degrades to a BasesError (inert block), not ZeroDivisionError.
    with pytest.raises(BasesError):
        evaluate_formula(FBinOp("/", FLiteral(1), FLiteral(0)), row)


def test_binop_unknown_operator_is_unsupported(row):
    # An operator outside the whitelist must be refused explicitly.
    with pytest.raises(BasesUnsupportedError):
        evaluate_formula(FBinOp("**", FLiteral(2), FLiteral(3)), row)


# --------------------------------------------------------------------------- #
# AC-1: per-type isinstance dispatch — unknown node type rejected, never dynamic
# --------------------------------------------------------------------------- #
def test_unknown_node_type_raises_unsupported(row):
    class WeirdNode(FormulaNode):
        pass

    with pytest.raises(BasesUnsupportedError):
        evaluate_formula(WeirdNode(), row)


# --------------------------------------------------------------------------- #
# AC-3: function outside whitelist -> unsupported (inert), never executed
# --------------------------------------------------------------------------- #
def test_whitelisted_functions_evaluate(row):
    assert evaluate_formula(FCall("lower", [FLiteral("ABC")]), row) == "abc"
    assert evaluate_formula(FCall("upper", [FLiteral("abc")]), row) == "ABC"
    assert evaluate_formula(FCall("length", [FLiteral("abcd")]), row) == 4
    assert evaluate_formula(FCall("contains", [FLiteral("hello"), FLiteral("ell")]), row) is True
    assert evaluate_formula(FCall("startsWith", [FLiteral("hello"), FLiteral("he")]), row) is True
    assert evaluate_formula(FCall("default", [FLiteral(None), FLiteral("x")]), row) == "x"
    assert evaluate_formula(FCall("if", [FLiteral(True), FLiteral("y"), FLiteral("n")]), row) == "y"


def test_function_outside_whitelist_is_unsupported(row):
    # A function name absent from the closed table must be refused.
    with pytest.raises(BasesUnsupportedError):
        evaluate_formula(FCall("os_system", [FLiteral("rm -rf /")]), row)


def test_hostile_function_name_dunder_is_unsupported(row):
    with pytest.raises(BasesUnsupportedError):
        evaluate_formula(FCall("__import__", [FLiteral("os")]), row)


# --------------------------------------------------------------------------- #
# AC-3 (bis): property-chain member outside the table -> unsupported (no getattr)
# --------------------------------------------------------------------------- #
def test_propchain_path_returns_row_file_path(row):
    node = FPropChain(receiver=FCall("asFile", [FField("file.path")]), member="path")
    assert evaluate_formula(node, row) == "products/basic-memory/Note A.md"


def test_propchain_name_and_basename(row):
    base = FPropChain(receiver=FCall("asFile", [FField("file.path")]), member="path")
    name_node = FPropChain(receiver=FCall("asFile", [FField("file.path")]), member="name")
    basename_node = FPropChain(receiver=FCall("asFile", [FField("file.path")]), member="basename")
    assert base  # path resolved above
    assert evaluate_formula(name_node, row) == "Note A.md"
    assert evaluate_formula(basename_node, row) == "Note A"


def test_propchain_startswith_on_path(row):
    node = FPropChain(
        receiver=FPropChain(receiver=FCall("asFile", [FField("file.path")]), member="path"),
        member="startsWith",
        args=[FLiteral("products/")],
    )
    assert evaluate_formula(node, row) is True


def test_propchain_member_outside_table_is_unsupported(row):
    node = FPropChain(receiver=FCall("asFile", [FField("file.path")]), member="not_a_member")
    with pytest.raises(BasesUnsupportedError):
        evaluate_formula(node, row)


# --------------------------------------------------------------------------- #
# AC-4: lambda env closed + asFile().path -> row file.path, no filesystem
# --------------------------------------------------------------------------- #
def test_lambda_binds_param_to_closed_env(row):
    # (x => x.startsWith("pro"))  applied with arg = file.path string
    lam = FLambda(
        params=["x"],
        body=FPropChain(receiver=FField("x"), member="startsWith", args=[FLiteral("products/")]),
    )
    evaluator = FormulaEvaluator(row)
    fn = evaluator.eval(lam)
    assert callable(fn)
    assert fn("products/basic-memory/Note A.md") is True
    assert fn("areas/other.md") is False


def test_lambda_does_not_capture_host_globals(row):
    # The lambda body references an identifier that is NOT a row field and NOT a
    # bound param. It must resolve to None (closed env), never to a host global.
    lam = FLambda(params=["x"], body=FField(name="builtins"))
    evaluator = FormulaEvaluator(row)
    fn = evaluator.eval(lam)
    # 'builtins' is a real host module name; the sandbox must NOT expose it.
    assert fn("anything") is None


def test_asfile_path_does_not_touch_filesystem(row, monkeypatch):
    # Prove no filesystem access happens while resolving asFile().path.
    calls = {"open": 0, "os_stat": 0}

    real_open = builtins.open

    def tracking_open(*args, **kwargs):
        calls["open"] += 1
        return real_open(*args, **kwargs)

    monkeypatch.setattr(builtins, "open", tracking_open)

    import os

    real_stat = os.stat

    def tracking_stat(*args, **kwargs):
        calls["os_stat"] += 1
        return real_stat(*args, **kwargs)

    monkeypatch.setattr(os, "stat", tracking_stat)

    node = FPropChain(receiver=FCall("asFile", [FField("file.path")]), member="path")
    result = evaluate_formula(node, row)

    assert result == "products/basic-memory/Note A.md"
    assert calls["open"] == 0
    assert calls["os_stat"] == 0


# --------------------------------------------------------------------------- #
# AC-5: recursion / iteration / time bounds -> limit ; unexpected -> inert
# --------------------------------------------------------------------------- #
def _deep_binop(depth: int) -> FormulaNode:
    node: FormulaNode = FLiteral(1)
    for _ in range(depth):
        node = FBinOp("+", node, FLiteral(1))
    return node


def test_recursion_bound_raises_limit(row):
    from basic_memory.bases.schema import MAX_FORMULA_RECURSION

    deep = _deep_binop(MAX_FORMULA_RECURSION + 50)
    with pytest.raises(BasesLimitError):
        evaluate_formula(deep, row)


def test_iteration_bound_raises_limit(row):
    from basic_memory.bases.schema import MAX_FORMULA_ITERATIONS

    # A wide expression: many sibling additions, shallow depth, but huge node
    # count must trip the iteration budget (not the recursion bound).
    node: FormulaNode = FLiteral(0)
    # Build left-leaning chain wide enough; recursion bound would fire first for
    # a chain, so use a bounded-depth balanced tree via repeated small trees.
    evaluator = FormulaEvaluator(row)
    evaluator._iterations = MAX_FORMULA_ITERATIONS  # already at the budget
    with pytest.raises(BasesLimitError):
        evaluator.eval(FBinOp("+", FLiteral(1), FLiteral(1)))
    assert node is not None


def test_time_budget_raises_limit(row, monkeypatch):
    import basic_memory.bases.formula_eval as fe

    # Simulate elapsed time beyond the budget on the second clock read.
    ticks = iter([0.0, 10_000.0, 10_000.0, 10_000.0])
    monkeypatch.setattr(fe.time, "monotonic", lambda: next(ticks, 10_000.0))

    with pytest.raises(BasesLimitError):
        evaluate_formula(FBinOp("+", FLiteral(1), FLiteral(2)), row)


def test_safe_evaluate_never_raises_on_unexpected(row):
    # safe_evaluate_formula maps any failure to (None, error_type) — no crash.
    class Boom(FormulaNode):
        pass

    value, err = safe_evaluate_formula(Boom(), row)
    assert value is None
    assert err == "unsupported"


def test_safe_evaluate_returns_value_and_none_on_success(row):
    value, err = safe_evaluate_formula(FLiteral("ok"), row)
    assert value == "ok"
    assert err is None


def test_error_type_mapping(row):
    assert error_type_for(BasesUnsupportedError("x")) == "unsupported"
    assert error_type_for(BasesLimitError("x")) == "limit"
    assert error_type_for(ValueError("x")) == "unexpected"


# --------------------------------------------------------------------------- #
# ADVERSARIAL — sandbox escape attempts must all be neutralised
# --------------------------------------------------------------------------- #
def test_adv_propchain_dunder_class_is_unsupported(row):
    # value.__class__ — classic escape root. Must be refused (no getattr).
    node = FPropChain(receiver=FField("status"), member="__class__")
    with pytest.raises(BasesUnsupportedError):
        evaluate_formula(node, row)


def test_adv_propchain_globals_is_unsupported(row):
    node = FPropChain(receiver=FField("status"), member="__globals__")
    with pytest.raises(BasesUnsupportedError):
        evaluate_formula(node, row)


def test_adv_propchain_subclasses_is_unsupported(row):
    chain = FPropChain(
        receiver=FPropChain(receiver=FField("status"), member="__class__"),
        member="__subclasses__",
    )
    with pytest.raises(BasesUnsupportedError):
        evaluate_formula(chain, row)


def test_adv_getattr_is_never_called_on_content_value(row, monkeypatch):
    # Tripwire: if the interpreter ever calls getattr on a content value, fail.
    sentinel = {"hit": False}
    real_getattr = builtins.getattr

    def tripwire_getattr(obj, name, *default):
        # Only flag dynamic dunder lookups on row/content strings.
        if isinstance(obj, str) and name.startswith("__"):
            sentinel["hit"] = True
        return real_getattr(obj, name, *default)

    monkeypatch.setattr(builtins, "getattr", tripwire_getattr)

    node = FPropChain(receiver=FField("status"), member="__class__")
    with pytest.raises(BasesUnsupportedError):
        evaluate_formula(node, row)
    assert sentinel["hit"] is False


def test_adv_lambda_cannot_reach_host_builtins(row):
    # Lambda body tries to reference 'open' as a field — must be None, not the
    # host builtin.
    lam = FLambda(params=["x"], body=FField(name="open"))
    fn = FormulaEvaluator(row).eval(lam)
    assert fn("x") is None


def test_adv_hostile_function_eval_name_unsupported(row):
    for hostile in ("eval", "exec", "compile", "__import__", "system", "getattr"):
        with pytest.raises(BasesUnsupportedError):
            evaluate_formula(FCall(hostile, [FLiteral("x")]), row)


def test_adv_deep_recursion_attack_is_limit_not_stack_overflow(row):
    # A maliciously deep tree must trip the limit bound, never a RecursionError.
    deep = _deep_binop(5_000)
    with pytest.raises(BasesLimitError):
        evaluate_formula(deep, row)


def test_adv_asfile_path_tampered_field_stays_in_dataset(row, monkeypatch):
    # asFile(<arbitrary string>).path must still only echo the dataset value,
    # never resolve an absolute filesystem path.
    real_open = builtins.open
    opened = []

    def tracking_open(*args, **kwargs):
        opened.append(args[0] if args else None)
        return real_open(*args, **kwargs)

    monkeypatch.setattr(builtins, "open", tracking_open)

    node = FPropChain(receiver=FCall("asFile", [FLiteral("/etc/passwd")]), member="path")
    result = evaluate_formula(node, row)
    # The virtual file echoes its string argument; it never opens anything.
    assert result == "/etc/passwd"
    assert opened == []
