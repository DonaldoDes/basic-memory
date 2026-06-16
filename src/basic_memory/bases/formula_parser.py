"""Closed-grammar parser for Bases Phase 2 formulas (US-002 / ADR-004 §1, §4).

Compiles a formula expression string into a typed AST (``formula_ast``) using a
closed recursive-descent grammar. The parser is the FIRST line of defense
against hostile expressions from untrusted note content: anything outside the
grammar — unknown function, property-chain member outside the closed table,
dunder access, lambda over the host env — is rejected explicitly with
``BasesUnsupportedError`` (the block goes inert), never evaluated, never falling
back to dynamic execution.

Security invariants (ADR-004 §2):
- NO ``eval`` / ``exec`` / ``compile`` / ``__import__`` / dynamic ``getattr``
  anywhere — parsing is pure string tokenization + tree construction.
- Functions limited to the closed whitelist ``ALLOWED_FUNCTIONS``.
- Property-chain members limited to the closed table ``ALLOWED_MEMBERS``.
- Bounds: expression length (``MAX_FORMULA_LENGTH``) and AST depth
  (``MAX_AST_DEPTH``) → ``BasesLimitError``.

Grammar (closed):

    formula     := lambda | or_expr
    lambda      := params "=>" or_expr
    params      := ident | "(" ident ( "," ident )* ")"
    or_expr     := and_expr ( "||" and_expr )*
    and_expr    := equality ( "&&" equality )*
    equality    := comparison ( ( "==" | "!=" ) comparison )*
    comparison  := additive ( ( "<" | ">" | "<=" | ">=" ) additive )*
    additive    := multiplicative ( ( "+" | "-" ) multiplicative )*
    multiplicative := postfix ( ( "*" | "/" ) postfix )*
    postfix     := primary ( "." member call? )*
    primary     := literal | funcall | field_ref | "(" or_expr ")"
    funcall     := name "(" args? ")"
    field_ref   := ident ( "." ident )*
    literal     := string | number | true | false | null
"""

import re
from collections.abc import Iterator
from contextlib import contextmanager

from basic_memory.bases.errors import (
    BasesLimitError,
    BasesParseError,
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
from basic_memory.bases.schema import MAX_AST_DEPTH, MAX_FORMULA_LENGTH

# Closed function whitelist (ADR-004 §2.(a)). Any function outside this set is
# rejected with BasesUnsupportedError. Closed at grooming; extension requires an
# explicit ADR-004 amendment.
ALLOWED_FUNCTIONS = frozenset(
    {
        # Text
        "lower",
        "upper",
        "length",
        "contains",
        "startsWith",
        "endsWith",
        "replace",
        "string",
        # Numeric
        "round",
        "number",
        "min",
        "max",
        # Date
        "dateformat",
        "date",
        # Logic / default
        "default",
        "if",
        # Links
        "link",
        # Aggregates (GROUP BY)
        "sum",
        "average",
        "count",
        # Property-chain converter usable in call position
        "asFile",
    }
)

# Closed property-chain member table (ADR-004 §2.(a bis)). Any member outside
# this set is rejected with BasesUnsupportedError. Resolution at eval time uses a
# closed dispatch table, never getattr on note content.
ALLOWED_MEMBERS = frozenset(
    {
        "asFile",
        "path",
        "name",
        "basename",
        "startsWith",
        "endsWith",
        "contains",
    }
)

_TOKEN_RE = re.compile(
    r"""
      \s*(
        =>                         # lambda arrow
      | ==|!=|<=|>=|<|>            # comparison operators
      | &&|\|\|                     # logical operators
      | \+|\-|\*|/                 # arithmetic operators
      | !                           # not
      | \(|\)|,|\.                  # grouping / chain dot / arg sep
      | "(?:[^"\\]|\\.)*"           # double-quoted string
      | '(?:[^'\\]|\\.)*'           # single-quoted string
      | -?\d+\.\d+|-?\d+            # numbers
      | [A-Za-z_][A-Za-z0-9_]*      # bare identifiers (single segment)
      )
    """,
    re.VERBOSE,
)

_COMPARISON_OPS = {"<", ">", "<=", ">="}
_EQUALITY_OPS = {"==", "!="}
_ADDITIVE_OPS = {"+", "-"}
_MULT_OPS = {"*", "/"}


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
                    f"Unexpected character in formula at: {text[pos:pos + 16]!r}"
                )
            self.tokens.append(m.group(1))
            pos = m.end()
        self.i = 0
        self.paren_depth = 0
        # Recursive-descent depth, bounded DURING parsing across ALL recursive
        # voices (binops, calls, property chains, parentheses, lambdas) so a
        # hostile payload can never exhaust the Python call stack before the
        # bound fires (ADR-004 §2.(d); P1 DoS rework). _check_depth on the built
        # tree remains as a coherence net, but detection happens here first.
        self.descent_depth = 0

    def peek(self) -> str | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def peek2(self) -> str | None:
        j = self.i + 1
        return self.tokens[j] if j < len(self.tokens) else None

    def next(self) -> str | None:
        tok = self.peek()
        self.i += 1
        return tok

    def expect_next(self) -> str:
        """Consume and return the next token, raising if at end of input."""
        tok = self.next()
        if tok is None:
            raise BasesParseError("Unexpected end of formula expression")
        return tok

    def eof(self) -> bool:
        return self.i >= len(self.tokens)

    @contextmanager
    def descend(self) -> "Iterator[None]":
        """Bound one level of recursive descent.

        Increments the descent counter on entry and raises BasesLimitError the
        moment it crosses MAX_AST_DEPTH — BEFORE recursing further — so no
        recursive voice (binops, calls, property chains, parentheses, lambdas)
        can blow the Python stack. Decremented on exit so sibling subtrees are
        measured by their own depth, not the cumulative node count.
        """
        self.descent_depth += 1
        if self.descent_depth > MAX_AST_DEPTH:
            raise BasesLimitError(
                f"Formula recursion exceeds depth {MAX_AST_DEPTH}"
            )
        try:
            yield
        finally:
            self.descent_depth -= 1


_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUMBER_RE = re.compile(r"-?\d+\.\d+|-?\d+")
# Dunder identifiers (__class__, __import__, __globals__, ...) are an attack
# surface for sandbox escape and are refused outright in any position.
def _reject_dunder(ident: str) -> None:
    if "__" in ident:
        raise BasesUnsupportedError(
            f"Dunder identifier '{ident}' is forbidden in formulas"
        )


def parse_formula(text: str) -> FormulaNode:
    """Parse a formula expression string into a typed AST node.

    Raises:
        BasesLimitError: expression too long or AST too deep.
        BasesParseError: malformed expression (syntax).
        BasesUnsupportedError: construct outside the closed grammar — unknown
            function, property-chain member outside the table, dunder access.
    """
    if len(text) > MAX_FORMULA_LENGTH:
        raise BasesLimitError(
            f"Formula expression exceeds {MAX_FORMULA_LENGTH} characters"
        )

    tz = _Tokenizer(text)
    if tz.eof():
        raise BasesParseError("Empty formula expression")
    node = _parse_formula(tz)
    if not tz.eof():
        raise BasesParseError(f"Trailing tokens in formula: {tz.peek()!r}")
    _check_depth(node, 0)
    return node


# ---------------------------------------------------------------------------
# Depth guard — explicit per-node-type recursion, no dynamic dispatch.
# ---------------------------------------------------------------------------
def _check_depth(node: FormulaNode, depth: int) -> None:
    if depth > MAX_AST_DEPTH:
        raise BasesLimitError(f"Formula AST exceeds depth {MAX_AST_DEPTH}")
    if isinstance(node, FBinOp):
        _check_depth(node.left, depth + 1)
        _check_depth(node.right, depth + 1)
    elif isinstance(node, FCall):
        for arg in node.args:
            _check_depth(arg, depth + 1)
    elif isinstance(node, FPropChain):
        _check_depth(node.receiver, depth + 1)
        for arg in node.args:
            _check_depth(arg, depth + 1)
    elif isinstance(node, FLambda):
        _check_depth(node.body, depth + 1)
    # FLiteral / FField are leaves.


# ---------------------------------------------------------------------------
# Grammar
# ---------------------------------------------------------------------------
def _parse_formula(tz: _Tokenizer) -> FormulaNode:
    # Bound the descent here: this is the single re-entry point used by call
    # arguments (_parse_call_args -> _parse_formula). A guard here caps nested
    # call recursion (lower(lower(... ))) before the stack is exhausted.
    with tz.descend():
        # Lambda forms: "x => body"  or  "(x, y) => body"
        lam = _try_parse_lambda(tz)
        if lam is not None:
            return lam
        return _parse_or(tz)


def _try_parse_lambda(tz: _Tokenizer) -> FormulaNode | None:
    start = tz.i
    tok = tz.peek()

    # single-param: ident =>
    if tok is not None and _IDENT_RE.fullmatch(tok) and tz.peek2() == "=>":
        param = tz.expect_next()
        _reject_dunder(param)
        tz.next()  # consume "=>"
        body = _parse_or(tz)
        return FLambda(params=[param], body=body)

    # multi-param: "(" ident ("," ident)* ")" "=>"
    if tok == "(":
        params: list[str] = []
        saved = tz.i
        tz.next()  # "("
        ok = True
        while True:
            p = tz.peek()
            if p is not None and _IDENT_RE.fullmatch(p):
                seg = tz.expect_next()
                _reject_dunder(seg)
                params.append(seg)
                nxt = tz.peek()
                if nxt == ",":
                    tz.next()
                    continue
                if nxt == ")":
                    tz.next()
                    break
                ok = False
                break
            ok = False
            break
        if ok and params and tz.peek() == "=>":
            tz.next()  # "=>"
            body = _parse_or(tz)
            return FLambda(params=params, body=body)
        # not a lambda — rewind
        tz.i = saved

    tz.i = start
    return None


def _parse_or(tz: _Tokenizer) -> FormulaNode:
    node = _parse_and(tz)
    while tz.peek() == "||":
        tz.next()
        node = FBinOp(op="||", left=node, right=_parse_and(tz))
    return node


def _parse_and(tz: _Tokenizer) -> FormulaNode:
    node = _parse_equality(tz)
    while tz.peek() == "&&":
        tz.next()
        node = FBinOp(op="&&", left=node, right=_parse_equality(tz))
    return node


def _parse_equality(tz: _Tokenizer) -> FormulaNode:
    node = _parse_comparison(tz)
    while tz.peek() in _EQUALITY_OPS:
        op = tz.expect_next()
        node = FBinOp(op=op, left=node, right=_parse_comparison(tz))
    return node


def _parse_comparison(tz: _Tokenizer) -> FormulaNode:
    node = _parse_additive(tz)
    while tz.peek() in _COMPARISON_OPS:
        op = tz.expect_next()
        node = FBinOp(op=op, left=node, right=_parse_additive(tz))
    return node


def _parse_additive(tz: _Tokenizer) -> FormulaNode:
    node = _parse_multiplicative(tz)
    while tz.peek() in _ADDITIVE_OPS:
        op = tz.expect_next()
        node = FBinOp(op=op, left=node, right=_parse_multiplicative(tz))
    return node


def _parse_multiplicative(tz: _Tokenizer) -> FormulaNode:
    node = _parse_postfix(tz)
    while tz.peek() in _MULT_OPS:
        op = tz.expect_next()
        node = FBinOp(op=op, left=node, right=_parse_postfix(tz))
    return node


def _parse_postfix(tz: _Tokenizer) -> FormulaNode:
    """primary ( "." member call? )*  — property/method chains, whitelisted."""
    node = _parse_primary(tz)
    # The chain loop is iterative, but each link nests an FPropChain one level
    # deeper. Bound the cumulative chain depth DURING parsing so a long
    # property/method chain (value.path.path...) is rejected with the limit type
    # instead of building an arbitrarily deep tree (P1 DoS rework).
    chain_depth = 0
    while tz.peek() == ".":
        chain_depth += 1
        if chain_depth > MAX_AST_DEPTH:
            raise BasesLimitError(
                f"Formula property chain exceeds depth {MAX_AST_DEPTH}"
            )
        tz.next()  # "."
        member = tz.peek()
        if member is None or not _IDENT_RE.fullmatch(member):
            raise BasesParseError("Expected member name after '.' in property chain")
        # Reject anything outside the closed property-chain table. Dunder access
        # (__class__, __subclasses__, __globals__, ...) lands here and is refused.
        if member not in ALLOWED_MEMBERS:
            raise BasesUnsupportedError(
                f"Property-chain member '{member}' is outside the closed table"
            )
        tz.next()  # member
        args: list[FormulaNode] = []
        if tz.peek() == "(":
            args = _parse_call_args(tz)
        node = FPropChain(receiver=node, member=member, args=args)
    return node


def _parse_primary(tz: _Tokenizer) -> FormulaNode:
    tok = tz.peek()
    if tok is None:
        raise BasesParseError("Expected operand, found end of formula")

    if tok == "(":
        # Parenthesised sub-expression re-enters _parse_or directly (bypassing
        # _parse_formula), so it carries its own descent guard. paren_depth is
        # kept as a redundant, parenthesis-specific net.
        tz.paren_depth += 1
        if tz.paren_depth > MAX_AST_DEPTH:
            raise BasesLimitError(
                f"Formula parenthesis nesting exceeds depth {MAX_AST_DEPTH}"
            )
        tz.next()
        with tz.descend():
            node = _parse_or(tz)
        if tz.next() != ")":
            raise BasesParseError("Unbalanced parentheses in formula")
        tz.paren_depth -= 1
        return node

    # string literal
    if (tok.startswith('"') and tok.endswith('"')) or (
        tok.startswith("'") and tok.endswith("'")
    ):
        tz.next()
        return FLiteral(value=_unquote(tok))

    # number literal
    if _NUMBER_RE.fullmatch(tok):
        tz.next()
        return FLiteral(value=float(tok) if "." in tok else int(tok))

    # keyword literals
    if tok in ("true", "false", "null"):
        tz.next()
        return FLiteral(value={"true": True, "false": False, "null": None}[tok])

    # identifier: function call or field reference
    if _IDENT_RE.fullmatch(tok):
        tz.next()
        _reject_dunder(tok)
        # function call form: name ( ... )
        if tz.peek() == "(":
            if tok not in ALLOWED_FUNCTIONS:
                raise BasesUnsupportedError(
                    f"Function '{tok}' is outside the closed formula whitelist"
                )
            args = _parse_call_args(tz)
            return FCall(fn=tok, args=args)
        # field reference: collect dotted segments greedily ONLY while the next
        # segment is followed by another "." that is NOT a whitelisted member /
        # call. To keep field refs (file.name) working while routing method/
        # property access through _parse_postfix, we treat a leading
        # `ident.ident` as a field ref unless a call follows.
        name = tok
        while tz.peek() == "." and tz.peek2() is not None and _IDENT_RE.fullmatch(
            tz.peek2() or ""
        ):
            # Look ahead: if the segment after the dot is a known member followed
            # by "(" (method call) or is part of a property chain, stop here and
            # let _parse_postfix handle it.
            seg = tz.peek2()
            after = tz.tokens[tz.i + 2] if tz.i + 2 < len(tz.tokens) else None
            if seg in ALLOWED_MEMBERS and (after == "(" or after == "."):
                break
            tz.next()  # "."
            seg_tok = tz.next()
            _reject_dunder(seg_tok or "")
            name = f"{name}.{seg_tok}"
        return FField(name=name)

    raise BasesParseError(f"Unexpected token in formula: {tok!r}")


def _parse_call_args(tz: _Tokenizer) -> list[FormulaNode]:
    if tz.next() != "(":
        raise BasesParseError("Expected '(' in call")
    args: list[FormulaNode] = []
    if tz.peek() != ")":
        while True:
            args.append(_parse_formula(tz))
            nxt = tz.peek()
            if nxt == ",":
                tz.next()
                continue
            break
    if tz.next() != ")":
        raise BasesParseError("Unbalanced parentheses in call")
    return args


def _unquote(tok: str) -> str:
    inner = tok[1:-1]
    return inner.replace('\\"', '"').replace("\\'", "'").replace("\\\\", "\\")


__all__ = ["parse_formula", "ALLOWED_FUNCTIONS", "ALLOWED_MEMBERS"]
