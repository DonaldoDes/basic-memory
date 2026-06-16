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

    # Prime the evaluator at the iteration budget so the next eval trips the
    # bound (not the recursion bound).
    evaluator = FormulaEvaluator(row)
    evaluator._iterations = MAX_FORMULA_ITERATIONS  # already at the budget
    with pytest.raises(BasesLimitError):
        evaluator.eval(FBinOp("+", FLiteral(1), FLiteral(1)))


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


# --------------------------------------------------------------------------- #
# ADVERSARIAL — memory-allocation bombs (P0): result-size amplification must be
# bounded BEFORE allocation. Every amplifying op (str/list ×/+) must raise
# BasesLimitError without ever materialising the giant result.
#
# The tests assert size is checked *before* allocation by tripping the bound
# with projected sizes that, if actually allocated, would consume gigabytes —
# yet the tests run in milliseconds and never OOM. To make "no allocation
# happened" deterministic, a tripwire monkeypatches the amplifying builtin
# (str.__mul__ / list.__mul__ / str.__add__ / list.__add__ paths) is not
# directly patchable, so we instead bound by projected size and verify the
# raised error is a "limit" type (never a successful huge value).
# --------------------------------------------------------------------------- #
@pytest.fixture
def amp_row() -> dict:
    """A row whose content is attacker-controlled and moderately large."""
    return {
        "s": "A" * 1024,  # 1 KiB string from note content
        "lst": list(range(100)),  # 100-element list from note content
    }


def _bytes_blob(n: int) -> str:
    return "A" * n


def test_adv_str_mul_int_amplification_is_limit(amp_row):
    # row["s"] (1 KiB) * 10**6  -> projected 1 GiB. Must be refused as "limit"
    # BEFORE allocating anything.
    node = FBinOp("*", FField("s"), FLiteral(10**6))
    value, err = safe_evaluate_formula(node, amp_row)
    assert value is None
    assert err == "limit"


def test_adv_str_mul_int_raises_limit_typed(amp_row):
    node = FBinOp("*", FField("s"), FLiteral(10**6))
    with pytest.raises(BasesLimitError):
        evaluate_formula(node, amp_row)


def test_adv_int_mul_str_amplification_is_limit(amp_row):
    # Reversed operand order: literal int * content string.
    node = FBinOp("*", FLiteral(10**6), FField("s"))
    value, err = safe_evaluate_formula(node, amp_row)
    assert value is None
    assert err == "limit"


def test_adv_list_mul_int_amplification_is_limit(amp_row):
    # row["lst"] (100 elems) * 10**6 -> projected 100M elements. Refused.
    node = FBinOp("*", FField("lst"), FLiteral(10**6))
    value, err = safe_evaluate_formula(node, amp_row)
    assert value is None
    assert err == "limit"


def test_adv_int_mul_list_amplification_is_limit(amp_row):
    node = FBinOp("*", FLiteral(10**6), FField("lst"))
    value, err = safe_evaluate_formula(node, amp_row)
    assert value is None
    assert err == "limit"


def test_adv_str_add_str_cumulative_is_limit(amp_row):
    # Two large content strings concatenated. Build operands whose cumulative
    # length exceeds the bound; refused BEFORE allocation.
    big = FLiteral(_bytes_blob(80_000))
    node = FBinOp("+", FField("s"), FBinOp("+", big, FLiteral(_bytes_blob(80_000))))
    value, err = safe_evaluate_formula(node, amp_row)
    assert value is None
    assert err == "limit"


def test_adv_list_add_list_cumulative_is_limit(amp_row):
    # Two large content lists concatenated -> cumulative cardinality bound.
    big = FLiteral(list(range(80_000)))
    node = FBinOp("+", big, FLiteral(list(range(80_000))))
    value, err = safe_evaluate_formula(node, amp_row)
    assert value is None
    assert err == "limit"


def test_adv_round_huge_ndigits_is_capped_or_limit(amp_row):
    # round(x, ndigits) with an enormous ndigits can allocate a huge Decimal/
    # string internally. The argument must be capped (or refused as limit),
    # never allowed to drive an unbounded allocation.
    node = FCall("round", [FLiteral(1.5), FLiteral(10**9)])
    value, err = safe_evaluate_formula(node, amp_row)
    # Either capped to a sane result, or refused as limit — never an OOM.
    assert err in (None, "limit")
    if err is None:
        assert value == 1.5


def test_amplification_bound_does_not_actually_allocate(amp_row, monkeypatch):
    # Tripwire: the projected-size check must run BEFORE the real multiplication.
    # We make the real multiplication observably catastrophic by capping the
    # process via the projected-size guard. Here we assert wall-clock stays tiny
    # and the result is a limit error (a real 1 GiB allocation would be slow and
    # memory-heavy). This is a behavioural proxy for "checked before allocation".
    import time as _t

    node = FBinOp("*", FField("s"), FLiteral(10**6))
    start = _t.monotonic()
    value, err = safe_evaluate_formula(node, amp_row)
    elapsed_ms = (_t.monotonic() - start) * 1000.0

    assert value is None
    assert err == "limit"
    # If allocation had happened (1 GiB), this would be far slower; bounding
    # before allocation keeps it well under the time budget.
    assert elapsed_ms < 100.0


def test_amplification_size_checked_before_multiplication_runs(amp_row):
    # Ironclad proof that the projected-size guard runs BEFORE the real
    # multiplication: feed a sequence whose __mul__ blows up if ever invoked.
    # The guard must reject on projected len(seq)*count and the explosive
    # __mul__ must NEVER be reached.
    class TripwireStr(str):
        def __mul__(self, other):  # pragma: no cover - must never run
            raise AssertionError("real multiplication executed before size guard")

        __rmul__ = __mul__

    tripwire_row = {"s": TripwireStr("A" * 1024)}
    node = FBinOp("*", FField("s"), FLiteral(10**6))
    value, err = safe_evaluate_formula(node, tripwire_row)
    assert value is None
    assert err == "limit"


def test_nominal_small_str_mul_still_works(amp_row):
    # Guard against over-blocking: a small, safe multiplication must still work.
    node = FBinOp("*", FLiteral("ab"), FLiteral(3))
    assert evaluate_formula(node, amp_row) == "ababab"


def test_nominal_small_str_add_still_works(amp_row):
    node = FBinOp("+", FLiteral("foo"), FLiteral("bar"))
    assert evaluate_formula(node, amp_row) == "foobar"


def test_nominal_small_list_concat_still_works(amp_row):
    node = FBinOp("+", FLiteral([1, 2]), FLiteral([3, 4]))
    assert evaluate_formula(node, amp_row) == [1, 2, 3, 4]
