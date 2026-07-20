"""Add summary column to search_index (US-006a).

Revision ID: o8j9k0l1m2n3
Revises: n7i8j9k0l1m2
Create Date: 2026-07-20 10:00:00.000000

Adds a persisted short-summary column to search_index on both backends.

- Postgres: ``ALTER TABLE search_index ADD COLUMN summary TEXT`` (trivial).
- SQLite: FTS5 virtual tables do not support ``ALTER TABLE ADD COLUMN``, so the
  table is dropped and recreated with the new ``summary UNINDEXED`` column. The
  table is a *derived* index (rebuilt from entity/observation/relation), so the
  drop is safe and lossless.

BACKFILL (post-deploy, REQUIRED): search_index is populated by indexing, not by
this migration. After upgrading, run ``basic-memory reindex --full`` (or let the
continuous auto-sync reindex) so existing rows gain their summary value. The
impact of skipping this differs by backend:

- Postgres: ADD COLUMN preserves existing rows. Only ``summary`` is affected —
  it stays NULL until the next natural reindex, while search itself keeps
  working (partial degradation).
- SQLite: the DROP+CREATE above empties ``search_index`` entirely (not just
  ``summary``). Until a full reindex runs, search returns ZERO results, not
  just missing summaries. The reindex is not optional here — it is required
  before reopening the service to traffic.
"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import inspect


revision: str = "o8j9k0l1m2n3"
down_revision: Union[str, None] = "n7i8j9k0l1m2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Inline FTS5 DDL kept self-contained (immutable migration snapshot). Mirrors
# models.search.CREATE_SEARCH_INDEX with the added `summary UNINDEXED` column.
_SQLITE_CREATE_SEARCH_INDEX = """
CREATE VIRTUAL TABLE search_index USING fts5(
    id UNINDEXED,
    title,
    content_stems,
    content_snippet,
    summary UNINDEXED,
    permalink,
    file_path UNINDEXED,
    type UNINDEXED,
    project_id UNINDEXED,
    from_id UNINDEXED,
    to_id UNINDEXED,
    relation_type UNINDEXED,
    entity_id UNINDEXED,
    category UNINDEXED,
    metadata UNINDEXED,
    created_at UNINDEXED,
    updated_at UNINDEXED,
    tokenize='unicode61 tokenchars 0x2F',
    prefix='1,2,3,4'
);
"""

# Prior schema (without summary), for downgrade recreate.
_SQLITE_CREATE_SEARCH_INDEX_LEGACY = """
CREATE VIRTUAL TABLE search_index USING fts5(
    id UNINDEXED,
    title,
    content_stems,
    content_snippet,
    permalink,
    file_path UNINDEXED,
    type UNINDEXED,
    project_id UNINDEXED,
    from_id UNINDEXED,
    to_id UNINDEXED,
    relation_type UNINDEXED,
    entity_id UNINDEXED,
    category UNINDEXED,
    metadata UNINDEXED,
    created_at UNINDEXED,
    updated_at UNINDEXED,
    tokenize='unicode61 tokenchars 0x2F',
    prefix='1,2,3,4'
);
"""


def _table_exists(connection, table_name: str) -> bool:
    return table_name in inspect(connection).get_table_names()


def upgrade() -> None:
    connection = op.get_bind()
    dialect = connection.dialect.name

    if dialect == "sqlite":
        # FTS5 virtual table: drop + recreate with the new column. On fresh
        # installs the runtime-created table may not exist yet — recreate it so
        # the schema is correct immediately; init_search_index is a no-op after.
        if _table_exists(connection, "search_index"):
            op.execute("DROP TABLE search_index")
        op.execute(_SQLITE_CREATE_SEARCH_INDEX)
    else:
        # Postgres (and any relational backend): additive column, data preserved.
        if _table_exists(connection, "search_index"):
            op.execute("ALTER TABLE search_index ADD COLUMN IF NOT EXISTS summary TEXT")


def downgrade() -> None:
    connection = op.get_bind()
    dialect = connection.dialect.name

    if dialect == "sqlite":
        if _table_exists(connection, "search_index"):
            op.execute("DROP TABLE search_index")
        op.execute(_SQLITE_CREATE_SEARCH_INDEX_LEGACY)
    else:
        if _table_exists(connection, "search_index"):
            op.execute("ALTER TABLE search_index DROP COLUMN IF EXISTS summary")
