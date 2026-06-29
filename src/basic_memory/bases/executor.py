"""Bases executor — FROM/WHERE/SORT/LIMIT evaluation against entities.

Adapted from dataview/executor/executor.py. Reuses ExpressionEvaluator (filter
evaluation), FieldResolver (field resolution) and ResultFormatter (US-004
rendering) by direct import, guaranteeing byte-identical semantics with
Dataview on the shared surface.

The executor's ``select`` method produces structured rows; ``render`` (US-004)
turns them into TABLE/LIST markdown. FROM is a path-prefix match on the entity
dataset (``file.inFolder`` ≡ FROM) — never a filesystem access.
"""

from datetime import datetime
from typing import Any

from basic_memory.bases.aggregate import (
    AGGREGATE_WHITELIST,
    AggregatedGroup,
    GroupResult,
    aggregate,
    flatten,
    group_by,
)
from basic_memory.bases.errors import BasesExecutionError
from basic_memory.bases.filter_leaf import FormulaLeafNode
from basic_memory.bases.formula_eval import safe_evaluate_formula
from basic_memory.bases.schema import (
    MAX_AGG_ONLY_GROUPS,
    MAX_FLATTEN_CARDINALITY,
    MAX_RENDERED_ROWS,
    BasesFlatten,
    BasesQuery,
    BasesSortClause,
    ViewType,
)
from basic_memory.bases.errors import BasesLimitError
from basic_memory.bases.ast import BinaryOpNode, ExpressionNode, LiteralNode, SortDirection
from basic_memory.bases.field_resolver import FieldResolver
from basic_memory.bases.result_formatter import ResultFormatter

# Prefix marking a calculated column in ``order`` (``formula.<name>``). The
# suffix is the key into ``BasesQuery.formulas``.
_FORMULA_PREFIX = "formula."

# Private row key carrying the source entity for sort-field resolution. Never a
# rendered column (the formatter only emits the declared ``order`` columns).
_SORT_SOURCE_KEY = "__sort_source__"


class BasesExecutor:
    """Executes a parsed BasesQuery against a collection of entities."""

    def __init__(self, notes: list[dict[str, Any]], host: dict[str, Any] | None = None):
        self.notes = notes
        # US-b (ADR-005 §Axe 2): the host-note identity threaded into the formula
        # sandbox so ``this`` / ``file.hasLink(this.file)`` resolve. ``None`` when
        # no host is wired (block without ``this`` is non-regressed; a ``this``
        # block with no host degrades each leaf to falsy → empty result).
        self.host = host
        # US-2 (ADR-006 §Gap #4): the reference instant for ``today``/``now`` in
        # date formulas. Threaded from the host metadata when present (tests inject
        # a fixed clock for determinism); ``None`` → the sandbox falls back to the
        # real wall clock at evaluation time. Captured ONCE so every row of the
        # block sees the same ``today``.
        now_val = host.get("now") if isinstance(host, dict) else None
        self._now: datetime | None = now_val if isinstance(now_val, datetime) else None
        self.field_resolver = FieldResolver()
        self.formatter = ResultFormatter()

    # ------------------------------------------------------------------ select
    def select(self, query: BasesQuery) -> list[dict[str, Any]]:
        """Run the Phase 2 pipeline, returning the flat list of projected rows.

        Pipeline (ADR-004 §3): ``filter -> flatten -> [group_by] -> project ->
        sort -> limit``. When a ``group_by`` is set, ``select`` returns the
        rows of *all* groups concatenated (the grouped structure is produced by
        ``render``/``_grouped`` for sectioned output). ``flatten`` runs before
        grouping and may raise ``BasesLimitError`` (block inert) if a row
        expands beyond ``MAX_FLATTEN_CARDINALITY``.

        Each row carries ``title``, ``file.link``, ``file.path`` plus the
        projected columns (TABLE ``order``) keyed by alias-or-field-name.
        """
        entities = self._pipeline_rows(query)
        rows = self._project(entities, query)

        if query.view.sort:
            rows = self._apply_sort(rows, query.view.sort, query)

        # Hard cap then user LIMIT.
        rows = rows[:MAX_RENDERED_ROWS]
        if query.view.limit is not None:
            rows = rows[: query.view.limit]

        return self._strip_internal_keys(rows)

    @staticmethod
    def _strip_internal_keys(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop the private sort back-reference before rows cross the public
        boundary (returned to the integration as ``results``). Sorting is done;
        the source-entity carrier must never leak into the MCP output."""
        for row in rows:
            row.pop(_SORT_SOURCE_KEY, None)
        return rows

    def _pipeline_rows(self, query: BasesQuery) -> list[dict[str, Any]]:
        """filter -> flatten — the entity-level stages shared by select/render.

        FLATTEN may raise ``BasesLimitError`` here (a row over the cardinality
        bound makes the block inert); the integration envelope catches it.
        """
        filtered = self._filter_by_from(query.from_source)
        if query.where is not None:
            filtered = self._filter_by_where(filtered, query.where)

        if query.view.flatten is not None:
            flatten_clause = query.view.flatten
            if isinstance(flatten_clause, BasesFlatten):
                # FLATTEN on a computed list EXPRESSION (US-3 / ADR-006 §Gap #5):
                # unfold ``expression`` (a closed file.* list) under ``alias``.
                filtered = flatten(
                    filtered,
                    flatten_clause.expression,
                    MAX_FLATTEN_CARDINALITY,
                    alias=flatten_clause.alias,
                )
            else:
                # Phase 2 plain-field FLATTEN (string field name).
                filtered = flatten(filtered, flatten_clause, MAX_FLATTEN_CARDINALITY)

        return filtered

    def _filter_by_from(self, from_source: str | None) -> list[dict[str, Any]]:
        if not from_source:
            return self.notes
        filtered = []
        for note in self.notes:
            path = note.get("path")
            if path is None:
                path = note.get("file", {}).get("path", "")
            # Prefix match on the dataset — never a filesystem lookup.
            if path.startswith(from_source) or from_source in path:
                filtered.append(note)
        return filtered

    def _filter_by_where(
        self, notes: list[dict[str, Any]], where: ExpressionNode
    ) -> list[dict[str, Any]]:
        filtered = []
        for note in notes:
            try:
                if self._eval_where(where, note):
                    filtered.append(note)
            except Exception:
                # Degradation: a note that raises during evaluation is skipped,
                # never crashes the whole block (mirror DataviewExecutor).
                continue
        return filtered

    def _eval_where(self, node: ExpressionNode, note: dict[str, Any]) -> bool:
        """Evaluate the WHERE tree against one note.

        US-a / ADR-005 §Axe 1: the FROM/WHERE/and/or/not STRUCTURE is unchanged —
        AND/OR/NOT combinators are Dataview ``BinaryOpNode`` (and a ``=`` against
        ``False`` for ``not``). Only the LEAVES changed: each is a
        :class:`FormulaLeafNode` whose typed formula AST is evaluated by the
        closed Phase 2 sandbox (``safe_evaluate_formula``) — the same audited
        objects, never ``getattr``/``eval``/``exec`` on note content.

        ``safe_evaluate_formula`` never raises: a per-leaf evaluation error
        degrades that leaf to ``None`` (falsy), the block is not crashed.
        """
        # Leaf: delegate to the sandbox (host threaded for ``this``/``hasLink``).
        if isinstance(node, FormulaLeafNode):
            value, _err = safe_evaluate_formula(node.formula, note, host=self.host, now=self._now)
            return bool(value)

        # Combination node: AND / OR (and the `= False` shape used by `not`).
        if isinstance(node, BinaryOpNode):
            op = (node.operator or "").upper()
            if op == "AND":
                return self._eval_where(node.left, note) and self._eval_where(node.right, note)
            if op == "OR":
                return self._eval_where(node.left, note) or self._eval_where(node.right, note)
            if node.operator == "=" and isinstance(node.right, LiteralNode):
                # `not <leaf>` is normalised to (<leaf> = False) by the parser.
                left = self._eval_where(node.left, note)
                return bool(left) == bool(node.right.value)
            raise BasesExecutionError(f"Unsupported WHERE combinator: {node.operator!r}")

        raise BasesExecutionError(f"Unsupported WHERE node: {type(node).__name__}")

    @staticmethod
    def _summary_names(query: BasesQuery) -> set[str]:
        """Output names of every GROUP-level aggregate declared on the view.

        Union of plain ``summaries:``, CONDITIONAL summaries
        (``summary_predicates``, e.g. ``count(status == "Done")``) and
        post-aggregation ``aggFormulas:`` (e.g. ``round(Done/Total*100)``). These
        names are NOT item frontmatter — they are computed once per group. Used
        by ``_project`` to materialise the group value into each item row's cell
        (BUG-019), the same set the aggregate-only path already injects.
        """
        names: set[str] = set()
        names.update(query.view.summaries.keys())
        names.update(query.view.summary_predicates.keys())
        names.update(query.view.agg_formulas.keys())
        return names

    def _project(
        self,
        notes: list[dict[str, Any]],
        query: BasesQuery,
        group_summary: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Project item rows for the (flat or per-group) table.

        ``group_summary`` (BUG-019): when the rows belong to a GROUP BY group,
        this is that group's computed summary dict (plain + conditional + post-agg
        values). A column whose raw token is a summary OUTPUT NAME resolves to the
        group value here — a group aggregate is constant across the group's items,
        so it repeats per-row (Obsidian Bases / Dataview grouped-table semantics).
        The group value is AUTHORITATIVE: it wins over any item frontmatter field
        that happens to collide with a summary name (BUG-019 ADV-1/ADV-3), and it
        is read from the trusted summary dict, never from item content.
        """
        order = query.view.order
        summary_names = self._summary_names(query) if group_summary else set()
        rows: list[dict[str, Any]] = []
        for note in notes:
            title = note.get("title", "Untitled")
            row: dict[str, Any] = {
                "title": title,
                "file.link": f"[[{title}]]",
                "file.path": note.get("file", {}).get("path", note.get("path", "")),
                # Private back-reference to the source entity so ``_apply_sort``
                # can resolve a sort field that was NOT projected into ``order``
                # (the displayName-aliased / off-order column sort bug). The key
                # is dunder-prefixed; the formatter only renders the declared
                # ``order`` columns, so this never leaks into the output.
                _SORT_SOURCE_KEY: note,
            }
            # Project TABLE columns (key by alias when defined).
            for col in order:
                alias = self._column_key(query, col)
                # Group-level summary column (BUG-019): the value is the group's
                # computed aggregate, NOT a per-item field. Resolved BEFORE the
                # static path so an item frontmatter field colliding with a
                # summary name can never override the trusted group value.
                if col in summary_names:
                    row[alias] = group_summary.get(col)  # type: ignore[union-attr]
                    continue
                if col.startswith(_FORMULA_PREFIX):
                    # Calculated column: evaluate the formula AST in the sandbox
                    # against the (nested-shape) note, so field references like
                    # ``file.folder`` / frontmatter resolve via FieldResolver.
                    # safe_evaluate_formula NEVER raises: a per-row evaluation
                    # error degrades the cell to None, the block is not crashed
                    # (ADR-004 §2.(e); extends executor.py static-column None
                    # degradation to formulas).
                    formula = query.formulas.get(col[len(_FORMULA_PREFIX) :])
                    if formula is None:
                        # Column references a formula not declared in formulas:.
                        row[alias] = None
                        continue
                    value, _err = safe_evaluate_formula(
                        formula.ast, note, host=self.host, now=self._now
                    )
                    row[alias] = value
                    continue
                # Static column (Phase 1 path, unchanged).
                try:
                    row[alias] = FieldResolver.resolve_field(note, col)
                except Exception:
                    row[alias] = None
            rows.append(row)
        return rows

    @staticmethod
    def _column_key(query: BasesQuery, col: str) -> str:
        """Return the row/header key for a projected column.

        Alias wins when the user declared one in ``properties:`` (keyed by the
        raw column token, e.g. ``formula.Origin``). Otherwise a calculated
        column keys by its formula name (the suffix after ``formula.``); a
        static column keys by the field name itself. Used by both ``_project``
        (row keys) and ``render`` (header names) so they never drift.
        """
        if col in query.aliases:
            return query.aliases[col]
        if col.startswith(_FORMULA_PREFIX):
            return col[len(_FORMULA_PREFIX) :]
        return col

    def _apply_sort(
        self,
        rows: list[dict[str, Any]],
        sort_clauses: list[BasesSortClause],
        query: BasesQuery | None = None,
    ) -> list[dict[str, Any]]:
        """Stable multi-key sort, resolving the sort field through ALL the keys
        a projected row may use for it.

        The bug this guards: after projection a column is keyed by its
        displayName ALIAS (``Date``), not the raw field (``date``), so a naive
        ``row.get(clause.field)`` resolved ``None`` for every row and the stable
        sort silently kept discovery order. The sort field is now resolved, in
        order: (1) the raw field key, (2) the alias declared for it in
        ``properties:`` (via ``_column_key``), (3) the underlying entity through
        ``FieldResolver`` (so a field NOT in ``order`` still sorts). ``ASC``/
        ``DESC`` both honoured via ``reverse``; a missing value sorts last.
        """
        for clause in reversed(sort_clauses):
            field = clause.field
            alias = self._column_key(query, field) if query is not None else field
            reverse = clause.direction == SortDirection.DESC

            def sort_key(row, _field=field, _alias=alias):
                value = self._resolve_sort_value(row, _field, _alias)
                if value is None:
                    return (1, "")
                return (0, value)

            rows = sorted(rows, key=sort_key, reverse=reverse)
        return rows

    @staticmethod
    def _resolve_sort_value(row: dict[str, Any], field: str, alias: str) -> Any:
        """Resolve a row's value for a sort field across raw key, alias, entity.

        Tried in order so the column sorts whether it was projected under its
        raw name, under a displayName alias, or not projected at all:
          1. the raw field key on the projected row,
          2. the alias key (the displayName the column was projected under),
          3. the source entity (``__sort_source__``) via ``FieldResolver`` —
             covers a sort on a column absent from ``order``.
        Returns ``None`` when none resolve (the row sorts last).
        """
        if field in row and row[field] is not None:
            return row[field]
        if alias != field and alias in row and row[alias] is not None:
            return row[alias]
        source = row.get(_SORT_SOURCE_KEY)
        if isinstance(source, dict):
            if field in source and source[field] is not None:
                return source[field]
            try:
                return FieldResolver.resolve_field(source, field)
            except Exception:
                return None
        return None

    # ------------------------------------------------------------------ render
    def render(self, query: BasesQuery) -> tuple[str, list[dict[str, Any]]]:
        """Render the query result as markdown + structured rows.

        Returns (markdown, rows). Without ``group_by`` the output is a single
        TABLE/LIST (Phase 1 path, unchanged). With ``group_by`` the output is
        one section per group, each with its own TABLE/LIST and (optionally) a
        summary line; a visible truncation marker is appended when more than
        ``MAX_GROUP_BY_GROUPS`` groups exist (ADR-004 §2.(d)).
        """
        if query.view.group_by is not None:
            if query.view.aggregate_only:
                return self._render_aggregate_only(query)
            return self._render_grouped(query)

        rows = self.select(query)
        return self._render_flat(query, rows), rows

    def _render_flat(self, query: BasesQuery, rows: list[dict[str, Any]]) -> str:
        """Render a single (ungrouped) TABLE/LIST."""
        if query.view.view_type == ViewType.TABLE:
            field_names = [self._column_key(query, col) for col in query.view.order]
            return self.formatter.format_table(rows, field_names)
        if query.view.view_type == ViewType.LIST:
            return self.formatter.format_list(rows)
        # Should not reach here: unsupported view types are rejected at parse.
        raise BasesExecutionError(f"Unsupported view type: {query.view.view_type}")

    def _render_grouped(self, query: BasesQuery) -> tuple[str, list[dict[str, Any]]]:
        """Render one section per GROUP BY group + truncation marker.

        Pipeline: filter -> flatten -> group_by -> aggregate -> project. Group
        keys and aggregate sources are resolved on the *entities* (which carry
        frontmatter + ``file.*``), then each group's entities are projected and
        sorted for display. Each group becomes a ``### <key>`` section. The flat
        list of all projected rows (across groups, capped) is returned as the
        second element for link discovery, mirroring the ungrouped contract.
        """
        group_by_clause = query.view.group_by
        assert group_by_clause is not None  # guaranteed by render() dispatch
        entities = self._pipeline_rows(query)

        # GROUP BY and aggregate operate on entities (frontmatter/file.* present).
        group_result = group_by(entities, group_by_clause.field)
        aggregated = self._aggregate_groups(group_result, query)

        sections: list[str] = []
        all_rows: list[dict[str, Any]] = []
        for grp in aggregated:
            # Project (and sort) the entities of this group for display. The
            # group's summary dict is threaded in so summary/aggFormula columns
            # referenced in ``order:`` render the GROUP value in each item cell
            # (BUG-019) instead of an empty static-field lookup.
            grp_rows = self._project(grp.rows, query, group_summary=grp.summary)
            if query.view.sort:
                grp_rows = self._apply_sort(grp_rows, query.view.sort, query)
            sections.append(self._render_group_section(query, grp, grp_rows))
            all_rows.extend(grp_rows)

        if group_result.truncated:
            sections.append(
                f"_{group_result.omitted} groupes non affichés "
                f"({group_result.total_groups} groupes au total, "
                f"limite {len(group_result.groups)})_"
            )

        # Apply the hard cap on the flat row list returned for link discovery.
        all_rows = all_rows[:MAX_RENDERED_ROWS]
        return "\n\n".join(sections), all_rows

    def _render_group_section(
        self, query: BasesQuery, grp: "AggregatedGroup", grp_rows: list[dict[str, Any]]
    ) -> str:
        """Render one group: a heading, an optional summary line, and a body."""
        key_label = grp.key if grp.key is not None else "(none)"
        lines = [f"### {key_label}"]

        if grp.summary:
            summary_parts = [f"{name}: {value}" for name, value in grp.summary.items()]
            lines.append("_" + " · ".join(summary_parts) + "_")

        lines.append(self._render_flat(query, grp_rows))
        return "\n\n".join(lines)

    # ----------------------------------------------- Phase 3 aggregation (US-d)
    def _aggregate_groups(
        self, group_result: "GroupResult", query: BasesQuery
    ) -> list["AggregatedGroup"]:
        """Compute each group's full summary: plain + conditional + post-agg.

        Pipeline per group (US-d / ADR-005 §Axe 4):
          1. plain summaries (Phase 2 ``aggregate`` over the closed whitelist).
          2. CONDITIONAL aggregates (``count``/``sum`` over a predicate): the
             predicate AST is evaluated in the Phase 2 SANDBOX
             (``safe_evaluate_formula``) per group row; count = #truthy rows,
             sum = Σ(1 per truthy row). The sandbox NEVER eval/exec/getattr; a
             per-row predicate error degrades that row to falsy (not counted).
          3. POST-AGGREGATION formulas (cross-summary arithmetic): each formula
             AST is evaluated in the sandbox against a SYNTHETIC row = the
             group's summary dict (so ``Done``/``Total`` resolve to the computed
             summary values). Division by zero raises ``BasesExecutionError`` in
             the sandbox → ``safe_evaluate_formula`` maps it to ``None`` (cell
             degraded, block still renders — BP3-D-03 invariant).

        Steps 2 and 3 run THROUGH the audited Phase 2 sandbox — no new
        unsandboxed evaluation path is introduced (ADR-005 §Axe 5).
        """
        # Step 1: plain summaries via the closed-whitelist Phase 2 aggregate.
        aggregated = aggregate(group_result, query.view.summaries, AGGREGATE_WHITELIST)

        predicates = query.view.summary_predicates
        agg_formulas = query.view.agg_formulas
        if not predicates and not agg_formulas:
            return aggregated

        for grp in aggregated:
            # Step 2: conditional aggregates over predicates (sandboxed).
            for name, (fn_name, predicate_ast) in predicates.items():
                matches = 0
                for row in grp.rows:
                    value, _err = safe_evaluate_formula(
                        predicate_ast, row, host=self.host, now=self._now
                    )
                    if value:
                        matches += 1
                # count == sum(1 per truthy) for a predicate aggregate.
                grp.summary[name] = matches

            # Step 3: post-aggregation cross-summary formulas (sandboxed). The
            # synthetic row IS the group's summary dict, so summary names resolve
            # as fields (FieldResolver/row lookup in _eval_field). Division by
            # zero degrades to None (safe_evaluate maps the sandbox exception).
            #
            # ORDER INDEPENDENCE (US-e hardening, ADR-005 §Axe 4): every
            # aggFormula evaluates against the SAME immutable snapshot of the
            # summaries computed BEFORE this loop (plain + conditional only).
            # Without the frozen snapshot, a later aggFormula could resolve an
            # earlier aggFormula's output as a summary, making the result depend
            # on declaration order. The snapshot excludes the agg-formula outputs,
            # so aggFormulas never see one another (an aggFormula referencing
            # another's name degrades to None — an undeclared summary).
            summary_snapshot = dict(grp.summary)
            for name, formula_ast in agg_formulas.items():
                value, _err = safe_evaluate_formula(
                    formula_ast, summary_snapshot, host=self.host, now=self._now
                )
                grp.summary[name] = value
        return aggregated

    def aggregate_only(self, query: BasesQuery) -> list["AggregatedGroup"]:
        """Return one ``AggregatedGroup`` per group with its full summary (US-d).

        The aggregate-only contract: 1 row per group, no row-items. The group's
        ``summary`` carries plain + conditional + post-agg values. The group key
        is exposed via ``AggregatedGroup.key``.

        Anti-DoS (ADR-005 §Axe 4): more than ``MAX_AGG_ONLY_GROUPS`` distinct
        groups makes the block INERT (``BasesLimitError`` → error_type "limit")
        — NOT a truncation. An aggregate-only rollup that silently dropped groups
        would mislead a reader into trusting a partial Progression table.
        """
        group_by_clause = query.view.group_by
        assert group_by_clause is not None  # guaranteed by render() dispatch
        entities = self._pipeline_rows(query)
        group_result = group_by(entities, group_by_clause.field)
        # Aggregate-only does not truncate: over the bound the block is inert.
        if group_result.total_groups > MAX_AGG_ONLY_GROUPS:
            raise BasesLimitError(
                f"Aggregate-only view over {MAX_AGG_ONLY_GROUPS} groups "
                f"({group_result.total_groups} groups) — block inert"
            )
        return self._aggregate_groups(group_result, query)

    def _render_aggregate_only(self, query: BasesQuery) -> tuple[str, list[dict[str, Any]]]:
        """Render the aggregate-only view: 1 row per group, no items (US-d).

        Each rendered row carries the group key (under the groupBy field name)
        plus every summary/agg-formula value (keyed by its output name). The
        ``order`` columns select which of these appear (the groupBy field and the
        summary names). The flat row list is returned for link discovery
        symmetry, though aggregate-only rows have no per-note identity.
        """
        groups = self.aggregate_only(query)
        group_field = query.view.group_by.field  # type: ignore[union-attr]

        rows: list[dict[str, Any]] = []
        for grp in groups:
            row: dict[str, Any] = {
                "title": str(grp.key) if grp.key is not None else "(none)",
                "file.link": "",
                "file.path": "",
                # Group key keyed by the groupBy field name AND its alias (when
                # declared via ``properties:``) so the column resolves whichever
                # the header uses (alias-aware, mirroring _project).
                group_field: grp.key,
                self._column_key(query, group_field): grp.key,
            }
            # Summary/agg-formula values keyed by output name AND alias, so an
            # aliased summary column ("Progress %") resolves against the row.
            for sname, svalue in grp.summary.items():
                row[sname] = svalue
                row[self._column_key(query, sname)] = svalue
            rows.append(row)

        if query.view.sort:
            rows = self._apply_sort(rows, query.view.sort, query)

        # Header columns = the declared order (group field + summary names).
        if query.view.view_type == ViewType.TABLE:
            field_names = [self._column_key(query, col) for col in query.view.order]
            markdown = self.formatter.format_table(rows, field_names)
        else:
            markdown = self.formatter.format_list(rows)
        return markdown, rows
