"""
Integration layer for Dataview in MCP tools — DEPRECATED (no-op).

The Dataview→Bases migration is complete: the vault is 100% Obsidian Bases and
the fork's Dataview *executor* is no longer used. This integration layer is the
single entry point the MCP read tools (``read_note`` / ``search_notes`` /
``build_context``) used to invoke. It is now **neutralised**:

  * ``process_note`` returns ``[]`` — no ``​```dataview​`` block is ever
    executed; the block is left inert (raw markdown) in the rendered note.
  * ``execute_raw_query`` returns an inert ``status="deprecated"`` payload.

The class, the factory ``create_dataview_integration`` and their signatures are
kept so the public surface stays stable (backward compatibility), but the
underlying engine (lexer / parser / executor) is never reached from here.

Note: the shared AST / detector / executor primitives under
``basic_memory.dataview`` are intentionally *retained* — the Bases executor
(``basic_memory.bases``) reuses them. This module only deprecates the Dataview
*rendering path*, not those primitives.
"""

from typing import Any, Dict, List, Optional


class DataviewIntegration:
    """Deprecated no-op Dataview integration.

    Retained for backward compatibility of the import surface; performs no query
    execution. ``enable_dataview`` on the MCP tools is therefore a no-op.
    """

    def __init__(self, notes_provider: Optional[callable] = None):
        """Accept the historical ``notes_provider`` argument; it is unused."""
        self.notes_provider = notes_provider

    def process_note(
        self, note_content: str, note_metadata: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """No-op: the Dataview executor is deprecated, nothing is executed.

        Always returns an empty result list so callers render the ``​```dataview​``
        block inertly (as raw markdown), exactly like a ``​```base​`` block when
        ``enable_bases=False``.
        """
        return []

    def execute_raw_query(self, query_text: str, query_id: str = "dv-1") -> Dict[str, Any]:
        """No-op: return an inert ``deprecated`` payload, run no query."""
        return {
            "query_id": query_id,
            "query_type": "unknown",
            "line_number": 0,
            "status": "deprecated",
            "result_markdown": "",
            "result_count": 0,
            "discovered_links": [],
            "results": [],
        }


def create_dataview_integration(notes_provider: Optional[callable] = None) -> DataviewIntegration:
    """Factory kept for backward compatibility — returns the no-op integration."""
    return DataviewIntegration(notes_provider)
