"""US-006 — Full adversarial suite (BASES-19/20/21/22/23).

YAML billion-laughs, all anti-DoS bounds, expression injection, AST depth,
hostile fields. Every case must produce an inert block (status:error) within a
bounded time, never crash, never execute arbitrary code.
"""

import time

import pytest

from basic_memory.bases.detector import BasesDetector
from basic_memory.bases.errors import (
    BasesLimitError,
    BasesParseError,
    BasesUnsupportedError,
)
from basic_memory.bases.integration import create_bases_integration
from basic_memory.bases.parser import BasesParser
from basic_memory.bases.schema import (
    MAX_BLOCK_BYTES,
    MAX_BLOCKS_PER_NOTE,
    MAX_FILTER_LEAVES,
    MAX_YAML_NODES,
)
from basic_memory.bases.yaml_loader import safe_load_no_aliases


def _notes():
    return [
        {
            "title": "Alpha",
            "file": {"path": "projects/Alpha.md", "name": "Alpha", "folder": "projects"},
            "created_at": "2026-01-01",
            "updated_at": "2026-01-10",
            "frontmatter": {"status": "Active"},
        }
    ]


def _process(block_body: str):
    integ = create_bases_integration(notes_provider=_notes)
    return integ.process_note(f"```base\n{block_body}\n```")


# ---------------------------------------------------------------------------
# BASES-19 — YAML anchors/aliases (billion-laughs)
# ---------------------------------------------------------------------------
class TestBillionLaughs:
    BILLION_LAUGHS = """
a: &a ["lol","lol","lol","lol","lol","lol","lol","lol","lol"]
b: &b [*a,*a,*a,*a,*a,*a,*a,*a,*a]
c: &c [*b,*b,*b,*b,*b,*b,*b,*b,*b]
d: [*c,*c,*c,*c,*c,*c,*c,*c,*c]
views:
  - type: list
"""

    def test_anchor_rejected_by_loader(self):
        with pytest.raises(BasesParseError):
            safe_load_no_aliases("a: &anchor value\nb: *anchor\n")

    def test_billion_laughs_rejected_and_bounded(self):
        start = time.time()
        with pytest.raises(BasesParseError):
            BasesParser.parse(self.BILLION_LAUGHS)
        elapsed = time.time() - start
        # No exponential expansion: must reject quasi-instantly.
        assert elapsed < 1.0

    def test_billion_laughs_inert_block_no_crash(self):
        results = _process(self.BILLION_LAUGHS)
        assert results[0]["status"] == "error"
        assert results[0]["error_type"] == "parse"

    def test_plain_alias_rejected(self):
        with pytest.raises(BasesParseError):
            BasesParser.parse("x: &v 1\ny: *v\nviews:\n  - type: list\n")


# ---------------------------------------------------------------------------
# BASES-20 — size / block-count / YAML-node bounds
# ---------------------------------------------------------------------------
class TestSizeBounds:
    def test_block_over_32kb_inert(self):
        # A valid-looking block padded past 32KB via a long quoted string.
        big_value = "x" * (MAX_BLOCK_BYTES + 100)
        block = f'filters: status == "{big_value}"\nviews:\n  - type: list'
        results = _process(block)
        assert results[0]["status"] == "error"
        assert results[0]["error_type"] == "limit"

    def test_more_than_max_blocks_dropped(self):
        one = "```base\nviews:\n  - type: list\n```"
        content = "\n\n".join([one] * (MAX_BLOCKS_PER_NOTE + 5))
        blocks = BasesDetector.detect_blocks(content)
        assert len(blocks) == MAX_BLOCKS_PER_NOTE

    def test_yaml_node_count_exceeded(self):
        # Build a wide mapping exceeding MAX_YAML_NODES nodes.
        big_props = "\n".join(
            f"  k{i}:\n    displayName: D{i}" for i in range(MAX_YAML_NODES)
        )
        block = f"views:\n  - type: list\nproperties:\n{big_props}"
        with pytest.raises(BasesLimitError):
            BasesParser.parse(block)


# ---------------------------------------------------------------------------
# BASES-21 — depth / leaf-count / leaf-length bounds
# ---------------------------------------------------------------------------
class TestDepthBounds:
    def test_filter_depth_over_10_inert(self):
        # Well-formed nested and: lists, deeper than MAX_FILTER_DEPTH (10).
        # Built as a dict then YAML-serialized (no anchors/aliases) so the
        # indentation is guaranteed correct.
        import yaml

        node = 'status == "x"'
        for _ in range(14):
            node = {"and": [node]}
        block = yaml.safe_dump(
            {"filters": node, "views": [{"type": "list"}]},
            default_flow_style=False,
        )
        # safe_dump never emits anchors for this acyclic structure.
        assert "&" not in block and " *" not in block
        results = _process(block)
        assert results[0]["status"] == "error"
        assert results[0]["error_type"] == "limit"

    def test_too_many_leaves_inert(self):
        leaves = "\n".join(
            f'    - status == "v{i}"' for i in range(MAX_FILTER_LEAVES + 10)
        )
        block = f"filters:\n  or:\n{leaves}\nviews:\n  - type: list\n"
        results = _process(block)
        assert results[0]["status"] == "error"
        assert results[0]["error_type"] == "limit"

    def test_leaf_expression_too_long_inert(self):
        long_value = "y" * 1100
        block = f'filters: status == "{long_value}"\nviews:\n  - type: list\n'
        results = _process(block)
        assert results[0]["status"] == "error"
        assert results[0]["error_type"] == "limit"

    def test_ast_depth_bounded(self):
        # Deeply nested && chain in a single leaf.
        leaf = " && ".join(['status == "x"'] * 30)
        with pytest.raises((BasesLimitError, BasesParseError)):
            BasesParser.parse(f'filters: "{leaf}"\nviews:\n  - type: list\n')


# ---------------------------------------------------------------------------
# BASES-22 — expression injection never evaluated
# ---------------------------------------------------------------------------
class TestInjection:
    @pytest.mark.parametrize(
        "leaf",
        [
            '__import__("os").system("echo pwned")',
            "eval(\\\"1+1\\\")",
            "exec(\\\"x=1\\\")",
            "os.system(\\\"ls\\\")",
        ],
    )
    def test_injection_payloads_rejected(self, leaf):
        block = f'filters: "{leaf}"\nviews:\n  - type: list\n'
        results = _process(block)
        # Function-call injection: a non-whitelisted call is refused (unsupported)
        # or malformed (parse). Never evaluated as code.
        assert results[0]["status"] == "error"
        assert results[0]["error_type"] in {"parse", "unsupported"}

    def test_dunder_attribute_chain_never_accessed(self):
        # status.__class__.__mro__ tokenizes as a bare dotted field ref. The
        # FieldResolver does dict lookups only — it NEVER touches Python
        # attributes — so this resolves to None and matches nothing. The key
        # security property is: no attribute access / no code execution.
        block = 'filters: status.__class__.__mro__ == "x"\nviews:\n  - type: list\n'
        results = _process(block)
        assert results[0]["status"] == "success"
        assert results[0]["result_count"] == 0

    def test_no_arbitrary_function_executed(self):
        # A function call to a non-whitelisted name must be refused, not run.
        with pytest.raises(BasesUnsupportedError):
            BasesParser.parse(
                'filters: system("rm -rf /")\nviews:\n  - type: list\n'
            )

    def test_backtick_payload_inert(self):
        block = 'filters: "`rm -rf /`"\nviews:\n  - type: list\n'
        results = _process(block)
        assert results[0]["status"] == "error"


# ---------------------------------------------------------------------------
# BASES-23 — file.inFolder path traversal stays in dataset
# ---------------------------------------------------------------------------
class TestPathTraversal:
    @pytest.mark.parametrize(
        "folder",
        ["../../etc", "/etc/passwd", "../../../", "..\\..\\windows"],
    )
    def test_traversal_no_filesystem_access(self, folder):
        block = f'filters: file.inFolder("{folder}")\nviews:\n  - type: list\n'
        results = _process(block)
        # Prefix match on the dataset only: no entity matches, no FS access.
        assert results[0]["status"] == "success"
        assert results[0]["result_count"] == 0


# ---------------------------------------------------------------------------
# Hostile fields
# ---------------------------------------------------------------------------
class TestHostileFields:
    def test_unknown_field_resolves_to_none_no_crash(self):
        block = 'filters: nonexistent_field == "x"\nviews:\n  - type: list\n'
        results = _process(block)
        # Unknown field -> None -> predicate False -> no match, no crash.
        assert results[0]["status"] == "success"
        assert results[0]["result_count"] == 0

    def test_deeply_dotted_field_is_not_a_chain(self):
        # file.name is valid; an unknown dotted path resolves to None.
        block = 'filters: file.unknown.deep == "x"\nviews:\n  - type: list\n'
        results = _process(block)
        assert results[0]["status"] == "success"
