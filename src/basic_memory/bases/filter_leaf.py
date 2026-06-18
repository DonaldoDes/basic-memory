"""Filter-leaf compilation routed to the Phase 2 formula sandbox (US-a / ADR-005 §Axe 1).

Phase 1 compiled a filter leaf with ``leaf_parser.py`` (a 4-function closed
grammar: contains/length/lower/upper). Obsidian Bases, however, applies in a
filter exactly the same functions and property-chains as in a formula (a uniform
surface). To reach that parity the fork **routes the filter leaf to the formula
sandbox already shipped in Phase 2** (``formula_parser`` / ``formula_eval``)
rather than extending a second parser:

- The FROM/WHERE/and/or/not STRUCTURE of the filter (``parser.py``) is unchanged.
  AND/OR/NOT combination nodes remain Dataview ``BinaryOpNode`` (the proven
  Phase 1 path). Only the atomic expression — the leaf — changes.
- A leaf is parsed by ``parse_formula`` (the closed-grammar formula parser, the
  FIRST line of defense) and wrapped in :class:`FormulaLeafNode`, a thin Dataview
  ``ExpressionNode`` subclass carrying the typed formula AST. The executor walks
  this node by evaluating the wrapped formula AST through the sandboxed
  ``safe_evaluate_formula`` (``formula_eval``) — the SAME audited objects as
  Phase 2 (no re-implementation, no second evaluator).

Security (ADR-005 invariants, continuity ADR-002 → ADR-004):
- The formula parser rejects any hostile construct (function outside the closed
  whitelist, property-chain member outside the closed table, dunder access,
  over-long expression) with ``BasesUnsupportedError`` / ``BasesLimitError`` —
  the block goes inert, the expression is never evaluated.
- There is NO ``eval`` / ``exec`` / ``compile`` / ``__import__`` / dynamic
  ``getattr`` in this module: compilation is pure delegation to the closed
  formula parser; evaluation is the closed sandbox. ``file.inFolder`` is NOT a
  WHERE predicate — it is extracted as a FROM source in ``parser.py`` BEFORE a
  leaf ever reaches this module.

``leaf_parser.py`` is DEPRECATED as the leaf evaluation path by this module (its
4-function grammar is superseded by the 18-function formula surface). It is kept
only as historical/structural reference; the live filter leaf path is here.
"""

from dataclasses import dataclass

from basic_memory.bases.formula_ast import FormulaNode
from basic_memory.bases.formula_parser import parse_formula
from basic_memory.dataview.ast import ExpressionNode


@dataclass
class FormulaLeafNode(ExpressionNode):
    """A filter leaf whose evaluation is delegated to the formula sandbox.

    It is a Dataview ``ExpressionNode`` subclass so it slots into the unchanged
    FROM/WHERE/and/or/not tree (combination nodes stay ``BinaryOpNode``), but it
    carries a typed *formula* AST (``formula_ast.FormulaNode``) rather than a
    Dataview expression. The executor recognises this node type and evaluates
    ``formula`` through ``formula_eval.safe_evaluate_formula`` — the closed
    Phase 2 sandbox. No ``getattr``/``eval``/``exec`` ever runs on note content.
    """

    formula: FormulaNode


def compile_filter_leaf(leaf: str) -> FormulaLeafNode:
    """Compile a single filter-leaf expression into a :class:`FormulaLeafNode`.

    The leaf is parsed by ``parse_formula`` — the closed-grammar formula parser,
    the first line of defense. Any construct outside the closed surface (unknown
    function, property-chain member outside the closed table, dunder, over-long
    expression, malformed syntax) raises a typed ``BasesError`` here, so the
    block goes inert and the expression is never evaluated.

    Note: ``file.inFolder("...")`` is handled upstream in ``parser.py`` as a FROM
    source and never reaches this function.

    Raises:
        BasesLimitError: expression too long or AST too deep.
        BasesParseError: malformed expression (syntax).
        BasesUnsupportedError: construct outside the closed formula grammar —
            unknown function, property-chain member outside the table, dunder.
    """
    formula_ast = parse_formula(leaf)
    return FormulaLeafNode(formula=formula_ast)


__all__ = ["FormulaLeafNode", "compile_filter_leaf"]
