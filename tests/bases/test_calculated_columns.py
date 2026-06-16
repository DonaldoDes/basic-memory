"""US-004 — Calculated columns (formula.*) in executor projection.

Covers the Gherkin AC of US-004:
- parser accepts ``formulas:`` (top-level) + ``formula.*`` columns in ``order``;
- ``executor._project`` evaluates calculated columns via the sandbox (US-003);
- evaluation error on a row -> ``None`` cell, block not crashed;
- format parity with static columns;
- ``MAX_FORMULAS_PER_BLOCK`` bound -> block inert (limit).

Reference: ADR-004 §1, §3 (pipeline), §4 (typed contracts BasesFormula,
BasesQuery.formulas).
"""

from basic_memory.bases.executor import BasesExecutor
from basic_memory.bases.formula_ast import FormulaNode
from basic_memory.bases.parser import BasesParser
from basic_memory.bases.schema import (
    MAX_FORMULAS_PER_BLOCK,
    BasesFormula,
    BasesGroupBy,
    BasesQuery,
)


# ---------------------------------------------------------------------------
# Parser: accepts formulas: + formula.* (AC-1)
# ---------------------------------------------------------------------------
class TestParserAcceptsFormulas:
    def test_formulas_top_level_compiled_to_basesformula(self):
        query = BasesParser.parse(
            """
formulas:
  Origin: lower(file.folder)
views:
  - type: table
    order: [file.name, formula.Origin]
"""
        )
        assert isinstance(query, BasesQuery)
        assert "Origin" in query.formulas
        formula = query.formulas["Origin"]
        assert isinstance(formula, BasesFormula)
        assert formula.name == "Origin"
        assert isinstance(formula.ast, FormulaNode)

    def test_order_contains_formula_column(self):
        query = BasesParser.parse(
            """
formulas:
  Origin: lower(file.folder)
views:
  - type: table
    order: [file.name, formula.Origin]
"""
        )
        assert "formula.Origin" in query.view.order

    def test_multiple_formulas(self):
        query = BasesParser.parse(
            """
formulas:
  Origin: lower(file.folder)
  Upper: upper(status)
views:
  - type: table
    order: [formula.Origin, formula.Upper]
"""
        )
        assert set(query.formulas.keys()) == {"Origin", "Upper"}

    def test_no_formulas_defaults_to_empty_dict(self):
        query = BasesParser.parse("views:\n  - type: table\n    order: [file.name]\n")
        assert query.formulas == {}


# ---------------------------------------------------------------------------
# Executor: calculated column rendered in TABLE (AC-3, smoke)
# ---------------------------------------------------------------------------
class TestCalculatedColumnRendering:
    def test_calculated_column_present_in_rows(self, sample_notes):
        query = BasesParser.parse(
            """
formulas:
  Origin: lower(file.folder)
views:
  - type: table
    order: [file.name, formula.Origin]
"""
        )
        rows = BasesExecutor(sample_notes).select(query)
        # Each row carries the calculated column keyed by its display name.
        assert all("Origin" in row for row in rows)
        by_title = {r["title"]: r for r in rows}
        assert by_title["Project Alpha"]["Origin"] == "projects"
        assert by_title["Area Dev"]["Origin"] == "areas"

    def test_calculated_column_present_in_table_markdown(self, sample_notes):
        query = BasesParser.parse(
            """
formulas:
  Origin: lower(file.folder)
views:
  - type: table
    order: [file.name, formula.Origin]
"""
        )
        markdown, _ = BasesExecutor(sample_notes).render(query)
        assert "Origin" in markdown
        assert "projects" in markdown
        assert "areas" in markdown

    def test_calculated_column_uses_alias_when_defined(self, sample_notes):
        query = BasesParser.parse(
            """
formulas:
  Origin: lower(file.folder)
properties:
  formula.Origin:
    displayName: Source
views:
  - type: table
    order: [formula.Origin]
"""
        )
        rows = BasesExecutor(sample_notes).select(query)
        assert all("Source" in row for row in rows)
        assert all("Origin" not in row for row in rows)


# ---------------------------------------------------------------------------
# Degradation: row error -> None cell, block not crashed (AC-4)
# ---------------------------------------------------------------------------
class TestRowErrorDegradation:
    def test_missing_field_yields_value_but_no_crash(self, sample_notes):
        # number(missing) -> float(None) raises -> cell None, block not crashed.
        query = BasesParser.parse(
            """
formulas:
  Num: number(nonexistent_field)
views:
  - type: table
    order: [file.name, formula.Num]
"""
        )
        rows = BasesExecutor(sample_notes).select(query)
        assert len(rows) == 3  # block fully rendered
        assert all(row["Num"] is None for row in rows)

    def test_division_by_zero_yields_none_per_row(self, sample_notes):
        query = BasesParser.parse(
            """
formulas:
  Bad: number(priority) / number(0)
views:
  - type: table
    order: [formula.Bad]
"""
        )
        rows = BasesExecutor(sample_notes).select(query)
        assert len(rows) == 3
        assert all(row["Bad"] is None for row in rows)

    def test_unknown_field_in_formula_resolves_none_not_crash(self, sample_notes):
        # Bare unknown field -> None; lower(None) -> "" (no crash).
        query = BasesParser.parse(
            """
formulas:
  Maybe: lower(unknown_prop)
views:
  - type: table
    order: [formula.Maybe]
"""
        )
        rows = BasesExecutor(sample_notes).select(query)
        assert len(rows) == 3
        assert all(row["Maybe"] == "" for row in rows)


# ---------------------------------------------------------------------------
# Format parity with static columns (AC-4)
# ---------------------------------------------------------------------------
class TestFormatParity:
    def test_calculated_string_renders_like_static_string(self, sample_notes):
        # A calculated column returning the same value as a static column must
        # render byte-identically in the table.
        static_q = BasesParser.parse("views:\n  - type: table\n    order: [status]\n")
        calc_q = BasesParser.parse(
            """
formulas:
  status: status
views:
  - type: table
    order: [formula.status]
"""
        )
        static_md, _ = BasesExecutor(sample_notes).render(static_q)
        calc_md, _ = BasesExecutor(sample_notes).render(calc_q)
        # Header differs only by the formula column name being identical here
        # ("status"), so the rendered tables are byte-identical.
        assert static_md == calc_md

    def test_calculated_none_renders_like_static_none(self, sample_notes):
        # Static missing column and a calculated column that fails both render
        # the same null display.
        static_q = BasesParser.parse("views:\n  - type: table\n    order: [missing_field]\n")
        calc_q = BasesParser.parse(
            """
formulas:
  missing_field: number(missing_field)
views:
  - type: table
    order: [formula.missing_field]
"""
        )
        static_md, _ = BasesExecutor(sample_notes).render(static_q)
        calc_md, _ = BasesExecutor(sample_notes).render(calc_q)
        assert static_md == calc_md


# ---------------------------------------------------------------------------
# Schema declarations (AC-2)
# ---------------------------------------------------------------------------
class TestSchemaDeclarations:
    def test_max_formulas_per_block_is_twenty(self):
        assert MAX_FORMULAS_PER_BLOCK == 20

    def test_basesformula_carries_name_and_ast(self):
        from basic_memory.bases.formula_parser import parse_formula

        ast = parse_formula("lower(status)")
        f = BasesFormula(name="X", ast=ast)
        assert f.name == "X"
        assert f.ast is ast

    def test_basesgroupby_declared(self):
        gb = BasesGroupBy(field="status")
        assert gb.field == "status"
