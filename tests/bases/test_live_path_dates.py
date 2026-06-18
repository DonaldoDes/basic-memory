"""Live-path integration tests for real date arithmetic (US-2 / BP4-B-06).

These tests go through the REAL MCP render path (``process_note`` →
``BasesExecutor`` via the provider), with a fixture faithful to the production
provider ``knowledge_router.list_entities_for_dataview`` — including the
``file.mtime`` / ``file.ctime`` (created_at/updated_at) fields the dashboards
compare against. Per ADR-006 §"Stratégie de vérification" (normative), no
synthetic dataset divergent from the provider is used.

Two real vault patterns are reproduced:
- Vault Lint Dashboard filter ``file.mtime > date(today) - dur("7 days")`` —
  recent notes filtered in, old notes filtered out (BP4-B-06).
- a ``dateformat(file.mtime, "yyyy-MM-dd")`` calculated column rendering a real
  formatted date (cells non-empty) (BP4-B-06).

Determinism (ADR-006 §Invariants): the host's ``now`` is injected through
``note_metadata`` so ``date(today)`` resolves against a FIXED instant — no
assertion on the system clock.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

from basic_memory.bases.integration import create_bases_integration


# A FIXED reference instant threaded into the host metadata.
FIXED_NOW = _dt.datetime(2026, 6, 18, 12, 0, 0, tzinfo=_dt.timezone.utc)


@dataclass
class _Entity:
    title: str
    file_path: str
    permalink: str | None = None
    note_type: str = "note"
    updated_at: str | None = None  # ISO mtime
    created_at: str | None = None  # ISO ctime
    metadata: dict = field(default_factory=dict)


def build_dataset(entities: list[_Entity]) -> list[dict]:
    """Mirror list_entities_for_dataview, including mtime/ctime exposure (US-2)."""
    from pathlib import PurePosixPath

    notes: list[dict] = []
    for e in entities:
        note: dict = {
            "file": {
                "path": e.file_path,
                "name": PurePosixPath(e.file_path).name,
                "folder": str(PurePosixPath(e.file_path).parent),
                "outlinks": [],
            },
            "title": e.title,
            "type": e.note_type,
        }
        if e.permalink:
            note["permalink"] = e.permalink
        # file.mtime / file.ctime are resolved by FieldResolver from these keys.
        if e.updated_at is not None:
            note["updated_at"] = e.updated_at
        if e.created_at is not None:
            note["created_at"] = e.created_at
        for k, v in e.metadata.items():
            note.setdefault(k, v)
        notes.append(note)
    return notes


def _entities() -> list[_Entity]:
    return [
        _Entity(
            title="Recent Note",
            file_path="projects/recent.md",
            permalink="projects/recent",
            note_type="project",
            updated_at="2026-06-17T09:00:00",  # 1 day before FIXED_NOW
            created_at="2026-06-01T09:00:00",
            metadata={"status": "Active"},
        ),
        _Entity(
            title="Old Note",
            file_path="projects/old.md",
            permalink="projects/old",
            note_type="project",
            updated_at="2026-01-01T09:00:00",  # months old
            created_at="2025-12-01T09:00:00",
            metadata={"status": "Active"},
        ),
    ]


def _provider():
    return build_dataset(_entities())


def _block(body: str) -> str:
    return f"```base\n{body}```"


def _run(body: str) -> dict:
    integ = create_bases_integration(notes_provider=_provider)
    host = {"now": FIXED_NOW}
    results = integ.process_note(_block(body), note_metadata=host)
    assert len(results) == 1
    return results[0]


class TestVaultLintDashboardTemporalFilter:
    def test_mtime_within_7_days_filters_recent_only(self):
        """BP4-B-06: file.mtime > date(today) - dur("7 days") keeps recent notes."""
        body = (
            'filters: file.mtime > date(today) - dur("7 days")\n'
            "views:\n"
            "  - type: list\n"
        )
        r = _run(body)
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 1  # only the recent note

    def test_mtime_older_than_90_days_filters_old_only(self):
        """The inverse dashboard pattern: stale notes (mtime < today - 90 days)."""
        body = (
            'filters: file.mtime < date(today) - dur("90 days")\n'
            "views:\n"
            "  - type: list\n"
        )
        r = _run(body)
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 1  # only the old note


class TestDateFormatColumnLivePath:
    def test_dateformat_mtime_column_renders_formatted_date(self):
        """BP4-B-06: a dateformat(file.mtime, "yyyy-MM-dd") column → non-empty cells."""
        body = (
            'filters: file.inFolder("projects")\n'
            "formulas:\n"
            '  Modified: dateformat(file.mtime, "yyyy-MM-dd")\n'
            "views:\n"
            "  - type: table\n"
            "    order:\n"
            "      - file.name\n"
            "      - formula.Modified\n"
        )
        r = _run(body)
        assert r["status"] == "success", r.get("error")
        assert r["result_count"] == 2
        formatted = {row.get("Modified") for row in r["results"]}
        # Real formatted dates, cells non-empty (the silent-empty-cell trap).
        assert formatted == {"2026-06-17", "2026-01-01"}
        assert all(v for v in formatted)
