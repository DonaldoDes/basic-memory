"""Obsidian Bases executor (Phase 1, parity-first).

Renders ```` ```base ```` fenced blocks (TABLE/LIST views) to markdown. The
shared filter AST, FieldResolver and ResultFormatter primitives live in this
package (``basic_memory.bases.ast`` / ``field_resolver`` / ``result_formatter``).

See ADR-003 and specs/bases-executor/spec.md.
"""

from basic_memory.bases.detector import BasesBlock, BasesDetector
from basic_memory.bases.errors import (
    BasesError,
    BasesExecutionError,
    BasesLimitError,
    BasesParseError,
    BasesUnsupportedError,
)
from basic_memory.bases.executor import BasesExecutor
from basic_memory.bases.integration import (
    BasesIntegration,
    create_bases_integration,
)
from basic_memory.bases.parser import BasesParser
from basic_memory.bases.schema import BasesQuery

__all__ = [
    "BasesBlock",
    "BasesDetector",
    "BasesError",
    "BasesExecutionError",
    "BasesLimitError",
    "BasesParseError",
    "BasesUnsupportedError",
    "BasesExecutor",
    "BasesIntegration",
    "BasesParser",
    "BasesQuery",
    "create_bases_integration",
]
