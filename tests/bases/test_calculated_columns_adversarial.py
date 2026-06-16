"""US-004 — Adversarial tests for calculated columns (sensitive zone).

The parser now accepts ``formulas:`` from untrusted note content (constitution
§ Zones sensibles — "Parsing markdown/frontmatter (entrée non fiable)"). These
tests assert the inert-block contract on hostile formula payloads:

- a function outside the closed whitelist -> block inert (BasesUnsupportedError),
  no execution;
- a property-chain member outside the closed table (dunder access) -> block
  inert, no getattr on content;
- more than MAX_FORMULAS_PER_BLOCK formulas -> block inert (BasesLimitError,
  type "limit");
- a hostile formula never reaches the sandbox runtime: rejection happens at
  PARSE time (first line of defense).

Reference: ADR-004 §2 (sandbox 6 pillars), skill adversarial-testing.
"""

import pytest

from basic_memory.bases.errors import (
    BasesError,
    BasesLimitError,
    BasesUnsupportedError,
)
from basic_memory.bases.parser import BasesParser


# ---------------------------------------------------------------------------
# AD-1 — function outside the closed whitelist -> block inert at parse time
# ---------------------------------------------------------------------------
class TestHostileFunction:
    def test_unknown_function_rejected_at_parse(self):
        with pytest.raises(BasesUnsupportedError):
            BasesParser.parse(
                """
formulas:
  Evil: __import__("os")
views:
  - type: table
    order: [formula.Evil]
"""
            )

    def test_exec_like_name_rejected(self):
        # 'eval' is not in the whitelist -> refused, never executed.
        with pytest.raises(BasesUnsupportedError):
            BasesParser.parse(
                """
formulas:
  Evil: eval("1+1")
views:
  - type: table
    order: [formula.Evil]
"""
            )


# ---------------------------------------------------------------------------
# AD-2 — forbidden property-chain (dunder / member outside the table)
# ---------------------------------------------------------------------------
class TestHostilePropertyChain:
    def test_dunder_property_chain_rejected_at_parse(self):
        with pytest.raises(BasesError):
            BasesParser.parse(
                """
formulas:
  Escape: status.__class__
views:
  - type: table
    order: [formula.Escape]
"""
            )

    def test_subclasses_chain_rejected(self):
        with pytest.raises(BasesError):
            BasesParser.parse(
                """
formulas:
  Escape: status.asFile().__class__.__bases__
views:
  - type: table
    order: [formula.Escape]
"""
            )

    def test_member_outside_table_rejected(self):
        with pytest.raises(BasesUnsupportedError):
            BasesParser.parse(
                """
formulas:
  Escape: status.asFile().read
views:
  - type: table
    order: [formula.Escape]
"""
            )


# ---------------------------------------------------------------------------
# AD-3 — more than MAX_FORMULAS_PER_BLOCK -> block inert (limit)
# ---------------------------------------------------------------------------
class TestFormulaCountBound:
    def test_over_bound_raises_limit(self):
        # 21 formulas > MAX_FORMULAS_PER_BLOCK (20).
        formulas_yaml = "\n".join(f"  F{i}: lower(status)" for i in range(21))
        order_yaml = ", ".join(f"formula.F{i}" for i in range(21))
        with pytest.raises(BasesLimitError):
            BasesParser.parse(
                f"""
formulas:
{formulas_yaml}
views:
  - type: table
    order: [{order_yaml}]
"""
            )

    def test_exactly_bound_accepted(self):
        formulas_yaml = "\n".join(f"  F{i}: lower(status)" for i in range(20))
        query = BasesParser.parse(
            f"""
formulas:
{formulas_yaml}
views:
  - type: table
    order: [formula.F0]
"""
        )
        assert len(query.formulas) == 20


# ---------------------------------------------------------------------------
# AD-4 — over-long formula expression -> block inert (limit)
# ---------------------------------------------------------------------------
class TestFormulaLengthBound:
    def test_over_long_expression_rejected(self):
        long_expr = "lower(" + "status" + ")" + (" + 'x'" * 500)
        with pytest.raises(BasesLimitError):
            BasesParser.parse(
                f"""
formulas:
  Long: {long_expr}
views:
  - type: table
    order: [formula.Long]
"""
            )
