"""Typed AST nodes for Bases Phase 2 formulas (ADR-004 §4).

These nodes are **disjoint** from the Phase 1 filter AST (``dataview/ast.py``):
they describe formulas, lambdas and property chains, which have no equivalent in
the Dataview expression AST. Keeping them separate protects the proven Phase 1
filtering path from any grammatical ambiguity (ADR-004 §1 decision).

The nodes are pure data containers — no evaluation logic lives here (that is
US-003, ``formula_eval.py``). The parser (``formula_parser.py``) produces these
nodes from a closed grammar; the future interpreter walks them with explicit
per-type dispatch and a whitelist of operations — never ``eval``/``exec``.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FormulaNode:
    """Base class for all formula AST nodes."""

    pass


@dataclass
class FLiteral(FormulaNode):
    """A literal value (string, number, bool, null)."""

    value: Any


@dataclass
class FField(FormulaNode):
    """A field reference, resolved against the dataset row only.

    ``name`` may be dotted (e.g. ``file.name``); resolution happens at eval time
    via the dataset's ``FieldResolver`` — never via filesystem or dynamic attr.
    """

    name: str


@dataclass
class FCall(FormulaNode):
    """A call to a whitelisted formula function.

    ``fn`` is guaranteed by the parser to be a member of the closed function
    whitelist (ADR-004 §2.(a)); the parser rejects any other name explicitly.
    """

    fn: str
    args: list["FormulaNode"] = field(default_factory=list)


@dataclass
class FPropChain(FormulaNode):
    """A whitelisted property/method access on a receiver expression.

    ``member`` is guaranteed by the parser to be a member of the closed
    property-chain table (ADR-004 §2.(a bis)); resolution at eval time uses a
    closed dispatch table, never ``getattr`` on note content.
    """

    receiver: "FormulaNode"
    member: str
    args: list["FormulaNode"] = field(default_factory=list)


@dataclass
class FLambda(FormulaNode):
    """A lambda with bound parameters and a formula body.

    ``body`` is always a ``FormulaNode`` (never a Python string or a closure over
    the host environment). At eval time the lambda receives a closed local
    environment = its bound params + the current dataset values only.
    """

    params: list[str]
    body: "FormulaNode"


@dataclass
class FThis(FormulaNode):
    """The self-reference ``this`` — the host note containing the base block.

    Carries NO data of its own: it resolves at eval time to a closed host
    wrapper that exposes ONLY ``this.file`` (the host's path/permalink identity),
    bounded to the injected host metadata (ADR-005 §Axe 2, invariant: ``this`` is
    bounded to the dataset + the host note, never a handle to reload the host
    from disk nor to reference an arbitrary note). With no host injected, ``this``
    resolves to ``None`` and the block goes inert downstream.
    """

    pass


@dataclass
class FBinOp(FormulaNode):
    """A binary operation.

    ``op`` is one of the whitelisted operators:
    ``+ - * / == != < > <= >= && ||``.
    """

    op: str
    left: "FormulaNode"
    right: "FormulaNode"


@dataclass
class FSubscript(FormulaNode):
    """A bounded subscript access ``receiver[index]`` (US-c / ADR-005 §Axe 3).

    ``receiver`` is any formula expression (typically a list-valued field, a
    method/property-chain result, or another subscript). ``index`` is a formula
    expression that MUST evaluate to a plain integer at eval time; anything else
    (string, float, bool, None) degrades to ``None``. There is NO slice form:
    ``[a:b]`` is rejected at parse time (out of v1). Evaluation is bounded —
    out-of-range indices yield ``None``, never an exception, never arbitrary
    memory access (ADR-005 invariant, continuity with the Phase 2 row-level
    degradation contract).
    """

    receiver: "FormulaNode"
    index: "FormulaNode"


__all__ = [
    "FormulaNode",
    "FLiteral",
    "FField",
    "FCall",
    "FPropChain",
    "FLambda",
    "FBinOp",
    "FThis",
    "FSubscript",
]
