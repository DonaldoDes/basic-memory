"""Integration layer for Bases in MCP tools.

Mirror of dataview/integration.py: bridges MCP tools (read_note, build_context,
search) and the Bases execution engine. Per ``base`` block it produces a result
envelope; any error yields ``status: error`` with a typed ``error_type`` and the
block is left inert — no exception ever reaches the MCP handler, and the rest of
the note renders normally.

Error type mapping (ADR-003 §3.3):
    BasesParseError       -> "parse"
    BasesUnsupportedError -> "unsupported"
    BasesLimitError       -> "limit"
    BasesExecutionError   -> "execution"
    (any other Exception) -> "unexpected"
"""

import time
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from basic_memory.bases.detector import BasesDetector
from basic_memory.bases.errors import (
    BasesExecutionError,
    BasesLimitError,
    BasesParseError,
    BasesUnsupportedError,
)
from basic_memory.bases.executor import BasesExecutor
from basic_memory.bases.parser import BasesParser
from basic_memory.bases.schema import MAX_BLOCK_BYTES


class BasesIntegration:
    """Integrate Bases execution into MCP tools."""

    def __init__(self, notes_provider: Optional[Callable[[], list]] = None):
        self.notes_provider = notes_provider
        self.detector = BasesDetector()

    def process_note(
        self, note_content: str, note_metadata: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """Process a note and execute all ```base``` blocks found in it."""
        blocks = self.detector.detect_blocks(note_content)
        if not blocks:
            return []

        logger.debug(f"Found {len(blocks)} base blocks in note")

        results = []
        for idx, block in enumerate(blocks, 1):
            result = self._execute_block(
                query_id=f"base-{idx}",
                block_body=block.body,
                line_number=block.start_line + 1,
            )
            results.append(result)
        return results

    def _execute_block(
        self, query_id: str, block_body: str, line_number: int
    ) -> Dict[str, Any]:
        start_time = time.time()

        try:
            # Size bound first (anti-DoS): an oversized block is inert (limit).
            if len(block_body.encode("utf-8")) > MAX_BLOCK_BYTES:
                raise BasesLimitError(
                    f"Base block exceeds {MAX_BLOCK_BYTES} bytes"
                )

            query = BasesParser.parse(block_body)
            notes = self._get_notes_for_query()

            executor = BasesExecutor(notes)
            result_markdown, rows = executor.render(query)

            execution_time_ms = int((time.time() - start_time) * 1000)
            return {
                "query_id": query_id,
                "query_type": query.view.view_type.value,
                "query_source": self._format_block_source(block_body),
                "line_number": line_number,
                "status": "success",
                "result_markdown": result_markdown,
                "result_count": len(rows),
                "discovered_links": self._extract_discovered_links(rows),
                "execution_time_ms": execution_time_ms,
                "results": rows,
            }

        except BasesParseError as e:
            return self._error_envelope(
                query_id, block_body, line_number, e, "parse", start_time
            )
        except BasesUnsupportedError as e:
            return self._error_envelope(
                query_id, block_body, line_number, e, "unsupported", start_time
            )
        except BasesLimitError as e:
            return self._error_envelope(
                query_id, block_body, line_number, e, "limit", start_time
            )
        except BasesExecutionError as e:
            return self._error_envelope(
                query_id, block_body, line_number, e, "execution", start_time
            )
        except Exception as e:  # pragma: no cover - safety net
            logger.error(
                f"Unexpected error executing base block {query_id}: {e}",
                exc_info=True,
            )
            return self._error_envelope(
                query_id,
                block_body,
                line_number,
                e,
                "unexpected",
                start_time,
                prefix="Unexpected error: ",
            )

    def _error_envelope(
        self,
        query_id: str,
        block_body: str,
        line_number: int,
        error: Exception,
        error_type: str,
        start_time: float,
        prefix: str = "",
    ) -> Dict[str, Any]:
        execution_time_ms = int((time.time() - start_time) * 1000)
        logger.warning(f"Base block {query_id} {error_type} error: {error}")
        return {
            "query_id": query_id,
            "query_type": "unknown",
            "query_source": self._format_block_source(block_body),
            "line_number": line_number,
            "status": "error",
            "error": f"{prefix}{error}",
            "error_type": error_type,
            "discovered_links": [],
            "result_count": 0,
            "execution_time_ms": execution_time_ms,
        }

    def _get_notes_for_query(self) -> List[Dict[str, Any]]:
        if self.notes_provider:
            try:
                return self.notes_provider()
            except Exception as e:  # pragma: no cover
                logger.warning(f"Failed to get notes from provider: {e}")
                return []
        return []

    def _format_block_source(self, block_body: str) -> str:
        return f"```base\n{block_body}\n```"

    def _extract_discovered_links(
        self, rows: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Extract note references from rendered rows for graph traversal."""
        links = []
        for row in rows:
            target = row.get("file.path") or ""
            if not target:
                link_val = row.get("file.link", "")
                if link_val.startswith("[[") and link_val.endswith("]]"):
                    target = link_val[2:-2]
            if not target:
                target = row.get("title", "")
            if target:
                links.append(
                    {
                        "target": target,
                        "type": "note",
                        "metadata": {},
                    }
                )
        return links

    def execute_raw_block(
        self, block_body: str, query_id: str = "base-1"
    ) -> Dict[str, Any]:
        """Public API: execute a raw base block body (used by build_context)."""
        return self._execute_block(
            query_id=query_id, block_body=block_body, line_number=0
        )


def create_bases_integration(
    notes_provider: Optional[Callable[[], list]] = None,
) -> BasesIntegration:
    """Factory mirroring create_dataview_integration."""
    return BasesIntegration(notes_provider)
