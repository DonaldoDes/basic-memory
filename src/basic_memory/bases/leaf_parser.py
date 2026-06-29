"""Closed-grammar parser for Bases filter-leaf expressions.

.. deprecated:: M-Bases-P3 (US-a, ADR-005 §Axe 1)
    This 4-function leaf grammar (``contains/length/lower/upper``) is NO LONGER
    the filter-leaf evaluation path. Filter leaves are now routed to the Phase 2
    formula sandbox (``filter_leaf.compile_filter_leaf`` → ``formula_parser`` →
    ``FormulaLeafNode`` evaluated by ``formula_eval``), reaching parity with
    Obsidian Bases (18 functions + property-chains + lambdas in filters). This
    module is kept for historical/structural reference only; ``parse_leaf`` is no
    longer called by ``parser.BasesParser``. Do not extend it — extend the
    formula whitelist (with an ADR-004 amendment) instead.

Grammar (ADR-003 §3.2):

    expr        := or_expr
    or_expr     := and_expr ( "||" and_expr )*
    and_expr    := unary ( "&&" unary )*
    unary       := [ "!" ] comparison
    comparison  := operand [ op operand ]
    op          := "==" | "!=" | "<=" | ">=" | "<" | ">"
    operand     := funcall | field_ref | literal
    funcall     := field_ref "." name "(" args? ")"     # method form
                 | name "(" args? ")"                    # global form
    field_ref   := ident ( "." ident )*
    literal     := string_quoted | number | true | false | null

Leaves are compiled to the existing Dataview AST nodes so ExpressionEvaluator
is reused unchanged. Normalization: == -> =, && -> AND, || -> OR, method-form
``f.fn(args)`` -> FunctionCallNode("fn", [FieldNode(f), args...]).

Anything outside this grammar (unknown function, property chain, lambda) is
rejected explicitly — never evaluated, never falls back to eval.
"""

import re

from basic_memory.bases.errors import (
    BasesLimitError,
    BasesParseError,
    BasesUnsupportedError,
)
from basic_memory.bases.schema import MAX_AST_DEPTH, MAX_LEAF_EXPR_CHARS
from basic_memory.bases.ast import (
    BinaryOpNode,
    ExpressionNode,
    FieldNode,
    FunctionCallNode,
    LiteralNode,
)

# Whitelisted parity functions (closed). file.inFolder is handled separately as
# a FROM source, not as a WHERE predicate.
ALLOWED_FUNCTIONS = {"contains", "length", "lower", "upper"}

_TOKEN_RE = re.compile(
    r"""
      \s*(
        ==|!=|<=|>=|<|>         # comparison operators
      | &&|\|\|                  # logical operators
      | !                        # not
      | \(|\)|,|\.               # grouping / chain dot
      | "(?:[^"\\]|\\.)*"        # double-quoted string
      | '(?:[^'\\]|\\.)*'        # single-quoted string
      | -?\d+\.\d+|-?\d+         # numbers
      | [A-Za-z_][A-Za-z0-9_.]*  # identifiers / dotted field refs
      )
    """,
    re.VERBOSE,
)


class _Tokenizer:
    def __init__(self, text: str):
        self.tokens: list[str] = []
        pos = 0
        n = len(text)
        while pos < n:
            if text[pos].isspace():
                pos += 1
                continue
            m = _TOKEN_RE.match(text, pos)
            if not m or m.group(1) is None:
                raise BasesParseError(
                    f"Unexpected character in filter expression at: {text[pos:pos + 16]!r}"
                )
            tok = m.group(1)
            self.tokens.append(tok)
            pos = m.end()
        self.i = 0

    def peek(self):
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def next(self):
        tok = self.peek()
        self.i += 1
        return tok

    def eof(self):
        return self.i >= len(self.tokens)


def parse_leaf(text: str) -> ExpressionNode:
    """Parse a single filter-leaf expression into a Dataview AST node.

    Raises:
        BasesLimitError: leaf too long or AST too deep.
        BasesParseError: malformed expression.
        BasesUnsupportedError: construct outside the Phase 1 surface.
    """
    if len(text) > MAX_LEAF_EXPR_CHARS:
        raise BasesLimitError(
            f"Filter leaf exceeds {MAX_LEAF_EXPR_CHARS} characters"
        )

    tz = _Tokenizer(text)
    if tz.eof():
        raise BasesParseError("Empty filter leaf expression")
    node = _parse_or(tz)
    if not tz.eof():
        raise BasesParseError(
            f"Trailing tokens in filter expression: {tz.peek()!r}"
        )
    _check_depth(node, 0)
    return node


def _check_depth(node: ExpressionNode, depth: int) -> None:
    if depth > MAX_AST_DEPTH:
        raise BasesLimitError(f"Expression AST exceeds depth {MAX_AST_DEPTH}")
    if isinstance(node, BinaryOpNode):
        _check_depth(node.left, depth + 1)
        _check_depth(node.right, depth + 1)
    elif isinstance(node, FunctionCallNode):
        for arg in node.arguments:
            _check_depth(arg, depth + 1)


def _parse_or(tz: _Tokenizer) -> ExpressionNode:
    node = _parse_and(tz)
    while tz.peek() == "||":
        tz.next()
        right = _parse_and(tz)
        node = BinaryOpNode(operator="OR", left=node, right=right)
    return node


def _parse_and(tz: _Tokenizer) -> ExpressionNode:
    node = _parse_unary(tz)
    while tz.peek() == "&&":
        tz.next()
        right = _parse_unary(tz)
        node = BinaryOpNode(operator="AND", left=node, right=right)
    return node


def _parse_unary(tz: _Tokenizer) -> ExpressionNode:
    if tz.peek() == "!":
        tz.next()
        operand = _parse_comparison(tz)
        # Normalize !x  ->  (x = False)
        return BinaryOpNode(operator="=", left=operand, right=LiteralNode(value=False))
    return _parse_comparison(tz)


_NORMALIZE_OP = {"==": "=", "!=": "!=", "<": "<", ">": ">", "<=": "<=", ">=": ">="}


def _parse_comparison(tz: _Tokenizer) -> ExpressionNode:
    left = _parse_operand(tz)
    op = tz.peek()
    if op in _NORMALIZE_OP:
        tz.next()
        right = _parse_operand(tz)
        return BinaryOpNode(operator=_NORMALIZE_OP[op], left=left, right=right)
    return left


def _parse_operand(tz: _Tokenizer) -> ExpressionNode:
    tok = tz.peek()
    if tok is None:
        raise BasesParseError("Expected operand, found end of expression")

    if tok == "(":
        tz.next()
        node = _parse_or(tz)
        if tz.next() != ")":
            raise BasesParseError("Unbalanced parentheses in filter expression")
        return node

    # literal: string
    if (tok.startswith('"') and tok.endswith('"')) or (
        tok.startswith("'") and tok.endswith("'")
    ):
        tz.next()
        return LiteralNode(value=_unquote(tok))

    # literal: number
    if re.fullmatch(r"-?\d+\.\d+|-?\d+", tok):
        tz.next()
        return LiteralNode(value=float(tok) if "." in tok else int(tok))

    # keywords
    if tok in ("true", "false", "null"):
        tz.next()
        return LiteralNode(value={"true": True, "false": False, "null": None}[tok])

    # identifier (field_ref) possibly followed by call or method-call
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", tok):
        tz.next()
        return _parse_after_ident(tz, tok)

    raise BasesParseError(f"Unexpected token in filter expression: {tok!r}")


def _parse_after_ident(tz: _Tokenizer, ident: str) -> ExpressionNode:
    # Global function form: name(...)
    if tz.peek() == "(":
        return _parse_call(tz, function_name=ident, receiver=None)

    # Method form requires the LAST dotted segment to be the function name:
    #   status.contains("x")  -> ident == "status.contains"
    # A bare field ref (status, file.name) has no following "(".
    if "." in ident:
        # Could be a method call if followed by "(" — but "(" was already
        # checked above. If ident contains dots and no "(", it's a field ref;
        # property chains like value.asFile().path are caught here because the
        # "(" would not immediately follow a dotted ident in our tokenization.
        pass
    return FieldNode(field_name=ident)


def _parse_call(tz: _Tokenizer, function_name: str, receiver) -> ExpressionNode:
    # function_name may be dotted for the method form: "status.contains"
    if "." in function_name:
        field_part, _, fn = function_name.rpartition(".")
        receiver = FieldNode(field_name=field_part)
        function_name = fn

    if function_name not in ALLOWED_FUNCTIONS:
        raise BasesUnsupportedError(
            f"Function '{function_name}' is outside the Phase 1 surface"
        )

    if tz.next() != "(":
        raise BasesParseError("Expected '(' in function call")

    args: list[ExpressionNode] = []
    if receiver is not None:
        args.append(receiver)

    if tz.peek() != ")":
        while True:
            args.append(_parse_or(tz))
            nxt = tz.peek()
            if nxt == ",":
                tz.next()
                continue
            break
    if tz.next() != ")":
        raise BasesParseError("Unbalanced parentheses in function call")

    # Reject property chains: a call result followed by ".something"
    if tz.peek() == ".":
        raise BasesUnsupportedError(
            "Property/method chains are outside the Phase 1 surface"
        )

    return FunctionCallNode(function_name=function_name, arguments=args)


def _unquote(tok: str) -> str:
    inner = tok[1:-1]
    return inner.replace('\\"', '"').replace("\\'", "'").replace("\\\\", "\\")
