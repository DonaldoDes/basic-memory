"""US-e hardening (finding perf US-d) — aggFormulas are ORDER-INDEPENDENT.

ADR-005 §Axe 4: cross-summary post-aggregation formulas operate on the group's
summary dict (the computed ``count``/``sum`` summaries). They must NOT see one
another: two ``aggFormulas`` declared in the same view must each evaluate against
the SAME immutable snapshot of the summaries computed BEFORE the post-agg loop.

Without an immutable snapshot, a later aggFormula could resolve an earlier
aggFormula's output as if it were a summary — making the result depend on the
declaration order (a silent, order-sensitive footgun). This test asserts the
hardening: an aggFormula referencing ANOTHER aggFormula's name degrades to None
(undeclared summary), regardless of declaration order, and a legitimate pair of
aggFormulas yields identical values whatever their order.
"""

from basic_memory.bases.executor import BasesExecutor
from basic_memory.bases.parser import BasesParser


def _note(title, **fm):
    return {
        "title": title,
        "file": {"path": f"notes/{title}.md", "name": title, "folder": "notes"},
        "frontmatter": dict(fm),
    }


def _notes():
    return [
        _note("A", milestone="M1", status="Done"),
        _note("B", milestone="M1", status="Done"),
        _note("C", milestone="M1", status="Todo"),
    ]


def _block(agg_formulas_lines: str) -> str:
    return (
        "views:\n"
        "  - type: table\n"
        "    order: [milestone]\n"
        "    groupBy: milestone\n"
        "    aggregate: true\n"
        "    summaries:\n"
        '      Done: count(status == "Done")\n'
        "      Total: count(title)\n"
        "    aggFormulas:\n"
        f"{agg_formulas_lines}"
    )


class TestAggFormulasOrderIndependent:
    def test_agg_formula_cannot_see_an_earlier_agg_formula(self):
        # ``Pct`` is a valid summary-based formula; ``Doubled`` references
        # ``Pct`` — another aggFormula. With an immutable pre-loop snapshot,
        # ``Pct`` is NOT visible to ``Doubled`` → ``Doubled`` degrades to None.
        block = _block(
            "      Pct: round(Done / Total * 100)\n"
            "      Doubled: Pct * 2\n"
        )
        query = BasesParser.parse(block)
        grp = BasesExecutor(_notes()).aggregate_only(query)[0]
        assert grp.summary["Pct"] == 67  # round(2/3*100)
        assert grp.summary["Doubled"] is None  # Pct invisible to Doubled

    def test_value_is_identical_whatever_declaration_order(self):
        # Two independent aggFormulas over the SAME summaries must yield the same
        # values regardless of which is declared first — proof of order
        # independence (each sees the same frozen summary snapshot).
        forward = _block(
            "      Pct: round(Done / Total * 100)\n"
            "      Rem: Total - Done\n"
        )
        reversed_ = _block(
            "      Rem: Total - Done\n"
            "      Pct: round(Done / Total * 100)\n"
        )
        g1 = BasesExecutor(_notes()).aggregate_only(BasesParser.parse(forward))[0]
        g2 = BasesExecutor(_notes()).aggregate_only(BasesParser.parse(reversed_))[0]
        assert g1.summary["Pct"] == g2.summary["Pct"] == 67
        assert g1.summary["Rem"] == g2.summary["Rem"] == 1
