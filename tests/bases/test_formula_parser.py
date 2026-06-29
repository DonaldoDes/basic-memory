"""Tests for the Bases Phase 2 formula AST + closed-grammar parser (US-002).

Covers nominal parse, out-of-grammar rejection, function whitelist, property
chain whitelist, lambdas, and anti-DoS bounds (length / AST depth).

Adversarial section (zone sensible — ADR-004 §2): the parser is the first line
of defense against hostile expressions from untrusted note content. It must
REJECT — never evaluate — dunder access, __import__, out-of-whitelist functions,
hostile property chains and depth-explosion payloads.
"""

import pytest

from basic_memory.bases.errors import BasesLimitError, BasesUnsupportedError
from basic_memory.bases import formula_ast as fast
from basic_memory.bases.formula_parser import parse_formula
from basic_memory.bases.schema import MAX_AST_DEPTH, MAX_FORMULA_LENGTH


# ---------------------------------------------------------------------------
# AC-1 — formula_ast nodes typed and disjoint from dataview/ast.py
# ---------------------------------------------------------------------------
class TestFormulaAstNodes:
    def test_seven_node_types_exist(self):
        assert hasattr(fast, "FormulaNode")
        for name in ("FLiteral", "FField", "FCall", "FPropChain", "FLambda", "FBinOp"):
            assert hasattr(fast, name), f"missing node {name}"

    def test_nodes_subclass_formulanode(self):
        for cls in (
            fast.FLiteral,
            fast.FField,
            fast.FCall,
            fast.FPropChain,
            fast.FLambda,
            fast.FBinOp,
        ):
            assert issubclass(cls, fast.FormulaNode)

    def test_disjoint_from_dataview_ast(self):
        """No formula node shares identity with a Phase 1 filter node."""
        from basic_memory.bases import ast as dv_ast

        dv_names = {"LiteralNode", "FieldNode", "BinaryOpNode", "FunctionCallNode"}
        formula_names = {"FLiteral", "FField", "FCall", "FPropChain", "FLambda", "FBinOp"}
        assert dv_names.isdisjoint(formula_names)
        # formula nodes are not instances of dataview nodes
        assert not issubclass(fast.FLiteral, dv_ast.ExpressionNode)
        assert not issubclass(fast.FBinOp, dv_ast.ExpressionNode)


# ---------------------------------------------------------------------------
# AC-2 — closed grammar compiles to AST (nominal)
# ---------------------------------------------------------------------------
class TestNominalParse:
    def test_string_literal(self):
        node = parse_formula('"hello"')
        assert isinstance(node, fast.FLiteral)
        assert node.value == "hello"

    def test_number_literal(self):
        assert parse_formula("42").value == 42
        assert parse_formula("3.14").value == 3.14

    def test_bool_null_literals(self):
        assert parse_formula("true").value is True
        assert parse_formula("false").value is False
        assert parse_formula("null").value is None

    def test_field_reference(self):
        node = parse_formula("status")
        assert isinstance(node, fast.FField)
        assert node.name == "status"

    def test_dotted_field_reference(self):
        node = parse_formula("file.name")
        assert isinstance(node, fast.FField)
        assert node.name == "file.name"

    def test_whitelisted_function_call(self):
        node = parse_formula('lower("HELLO")')
        assert isinstance(node, fast.FCall)
        assert node.fn == "lower"
        assert len(node.args) == 1
        assert isinstance(node.args[0], fast.FLiteral)

    def test_function_with_multiple_args(self):
        node = parse_formula('replace(title, "a", "b")')
        assert isinstance(node, fast.FCall)
        assert node.fn == "replace"
        assert len(node.args) == 3

    def test_binop_comparison(self):
        node = parse_formula("priority > 1")
        assert isinstance(node, fast.FBinOp)
        assert node.op == ">"
        assert isinstance(node.left, fast.FField)
        assert isinstance(node.right, fast.FLiteral)

    def test_binop_arithmetic(self):
        node = parse_formula("priority + 1")
        assert isinstance(node, fast.FBinOp)
        assert node.op == "+"

    def test_logical_and_or(self):
        node = parse_formula('status == "Active" && priority > 1')
        assert isinstance(node, fast.FBinOp)
        assert node.op == "&&"

    def test_property_chain_as_file_path(self):
        node = parse_formula('value.asFile().path.startsWith("projects")')
        # outermost is startsWith property-chain call
        assert isinstance(node, fast.FPropChain)
        assert node.member == "startsWith"

    def test_property_chain_simple_member(self):
        node = parse_formula("value.asFile().basename")
        assert isinstance(node, fast.FPropChain)
        assert node.member == "basename"

    def test_lambda_compiles_to_flambda(self):
        node = parse_formula('x => x.startsWith("A")')
        assert isinstance(node, fast.FLambda)
        assert node.params == ["x"]
        # body is an AST node, never a Python str / closure
        assert isinstance(node.body, fast.FormulaNode)
        assert not isinstance(node.body, str)

    def test_nested_function_calls(self):
        node = parse_formula('upper(lower("Mixed"))')
        assert isinstance(node, fast.FCall)
        assert node.fn == "upper"
        assert isinstance(node.args[0], fast.FCall)
        assert node.args[0].fn == "lower"


# ---------------------------------------------------------------------------
# AC-3 — out-of-grammar / out-of-whitelist / out-of-table → BasesUnsupportedError
# ---------------------------------------------------------------------------
class TestRejection:
    def test_function_outside_whitelist(self):
        with pytest.raises(BasesUnsupportedError):
            parse_formula('frobnicate("x")')

    def test_property_member_outside_table(self):
        with pytest.raises(BasesUnsupportedError):
            parse_formula("value.asFile().hackme")

    def test_method_member_outside_table(self):
        with pytest.raises(BasesUnsupportedError):
            parse_formula('value.asFile().exploit("x")')


# ---------------------------------------------------------------------------
# AC-4 — bounds: MAX_FORMULA_LENGTH and MAX_AST_DEPTH
# ---------------------------------------------------------------------------
class TestBounds:
    def test_max_formula_length_constant(self):
        assert MAX_FORMULA_LENGTH == 1024

    def test_max_ast_depth_constant(self):
        assert MAX_AST_DEPTH == 20

    def test_expression_too_long_raises_limit(self):
        expr = '"' + ("a" * (MAX_FORMULA_LENGTH + 10)) + '"'
        with pytest.raises(BasesLimitError):
            parse_formula(expr)

    def test_ast_too_deep_raises_limit(self):
        # build a deeply nested arithmetic expression: 1 + (1 + (1 + ...))
        depth = MAX_AST_DEPTH + 5
        expr = "1" + (" + 1" * depth)
        with pytest.raises(BasesLimitError):
            parse_formula(expr)

    def test_shallow_expression_within_bounds_ok(self):
        node = parse_formula("1 + 1")
        assert isinstance(node, fast.FBinOp)


# ---------------------------------------------------------------------------
# AC-7 — Adversarial parsing (zone sensible). Parser must REJECT, never eval.
# ---------------------------------------------------------------------------
class TestAdversarial:
    def test_dunder_class_access_rejected(self):
        """().__class__.__bases__ style escape must not parse to anything live."""
        with pytest.raises((BasesUnsupportedError,)):
            parse_formula('value.asFile().__class__')

    def test_dunder_field_access_rejected(self):
        with pytest.raises((BasesUnsupportedError,)):
            parse_formula("__class__")

    def test_import_function_rejected(self):
        with pytest.raises(BasesUnsupportedError):
            parse_formula('__import__("os")')

    def test_open_function_rejected(self):
        with pytest.raises(BasesUnsupportedError):
            parse_formula('open("/etc/passwd")')

    def test_eval_function_rejected(self):
        with pytest.raises(BasesUnsupportedError):
            parse_formula('eval("1+1")')

    def test_exec_function_rejected(self):
        with pytest.raises(BasesUnsupportedError):
            parse_formula('exec("x=1")')

    def test_getattr_function_rejected(self):
        with pytest.raises(BasesUnsupportedError):
            parse_formula('getattr(value, "path")')

    def test_subclasses_chain_rejected(self):
        with pytest.raises(BasesUnsupportedError):
            parse_formula("value.asFile().__subclasses__()")

    def test_globals_member_rejected(self):
        with pytest.raises(BasesUnsupportedError):
            parse_formula("value.asFile().__globals__")

    def test_hostile_property_chain_rejected(self):
        with pytest.raises(BasesUnsupportedError):
            parse_formula('value.asFile().path.system("rm -rf /")')

    def test_depth_explosion_rejected_not_evaluated(self):
        """A depth-explosion payload triggers the depth bound (type: limit).

        Hardened (P1 rework): the DoS bound MUST produce BasesLimitError —
        never BasesUnsupportedError, never RecursionError. The depth is bounded
        DURING descent, before the Python call stack is exhausted (ADR-004
        §2.(d)).
        """
        expr = "(" * 200 + "1" + ")" * 200
        with pytest.raises(BasesLimitError):
            parse_formula(expr)

    def test_nested_call_recursion_bounded_not_stack_overflow(self):
        """Nested whitelisted calls past the depth bound → BasesLimitError.

        Proof payload (P1): 100 nested ``lower(...)`` calls, length ~703, BELOW
        MAX_FORMULA_LENGTH. The recursive descent through _parse_call_args /
        _parse_formula must hit the parse-time depth guard and raise
        BasesLimitError — NOT RecursionError (uncaught → error_type unexpected),
        NOT BasesUnsupportedError. Regression test for the DoS recursion defect.
        """
        depth = 100
        payload = "lower(" * depth + '"x"' + ")" * depth
        assert len(payload) < MAX_FORMULA_LENGTH
        with pytest.raises(BasesLimitError):
            parse_formula(payload)

    def test_long_property_chain_bounded(self):
        """A property/field chain past the depth bound → BasesLimitError.

        A long chain (≥100 links) must be rejected with the limit type, not
        build an arbitrarily deep tree nor blow the recursive depth checker.
        """
        depth = 100
        payload = "value" + ".path" * depth
        assert len(payload) < MAX_FORMULA_LENGTH
        with pytest.raises(BasesLimitError):
            parse_formula(payload)

    def test_length_explosion_rejected(self):
        expr = "1" + (" + 1" * 5000)
        with pytest.raises(BasesLimitError):
            parse_formula(expr)

    def test_parser_has_no_eval_primitives(self):
        """Source-level guarantee: no eval/exec/compile/__import__/getattr in parser.

        ``re.compile`` (regex compilation) is the only legitimate ``compile``
        usage and is excluded — it is not the dangerous Python ``compile``
        builtin operating on untrusted source.
        """
        import inspect
        from basic_memory.bases import formula_parser

        src = inspect.getsource(formula_parser).replace("re.compile(", "")
        for forbidden in ("eval(", "exec(", "compile(", "__import__(", "getattr("):
            assert forbidden not in src, f"forbidden primitive {forbidden} in parser"

    def test_formula_ast_has_no_eval_primitives(self):
        import inspect
        from basic_memory.bases import formula_ast

        src = inspect.getsource(formula_ast).replace("re.compile(", "")
        for forbidden in ("eval(", "exec(", "compile(", "__import__(", "getattr("):
            assert forbidden not in src, f"forbidden primitive {forbidden} in ast"
