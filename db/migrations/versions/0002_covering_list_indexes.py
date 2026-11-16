"""Covering list indexes: the ordering indexes of Q5's administrator form and of the run list
include every column those pages select.

Rebuilds, in place and under their names, the access area's `IX_projects_status_name` with
`description` and `example_questions_json`, and the ingestion area's `IX_ingestion_runs_created`
and `IX_ingestion_runs_index_created` with every run column the run list selects
(`80-data-model.md` sections 2.3 and 2.16). A page then reads its rows in index order with no key
lookup and no sort, so neither statement needs a query hint (`84-testing.md` T-067).

Each `CREATE INDEX … WITH (DROP_EXISTING = ON)` replaces one index in one statement with the same
name, keys and rows, adding only included columns: an expand revision (section 2.24), and every
plan of the release serving `prod` still finds its index. The revision holds its own copy of every
name; `alembic check` compares the key columns, and the schema inventory test the included ones.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from typing import Final

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None

PROJECTS_STATUS_NAME: Final = (
    "CREATE INDEX [IX_projects_status_name] ON app.projects (status, name) "
    "INCLUDE (description, example_questions_json) WITH (DROP_EXISTING = ON)"
)
"""Q5's administrator form selects `id` (the clustering key), `name`, `description` and
`example_questions_json` of the active projects (`11-api.md` section 1.4.4)."""

INGESTION_RUNS_CREATED: Final = (
    "CREATE INDEX [IX_ingestion_runs_created] ON app.ingestion_runs (created_at DESC, id DESC) "
    "INCLUDE (kind, search_index_id, priority, state, requested_by, started_at, completed_at) "
    "WITH (DROP_EXISTING = ON)"
)
"""The run list selects every run column but the trace context and the finalisation counts."""

INGESTION_RUNS_INDEX_CREATED: Final = (
    "CREATE INDEX [IX_ingestion_runs_index_created] ON app.ingestion_runs "
    "(search_index_id, created_at DESC, id DESC) "
    "INCLUDE (kind, priority, state, requested_by, started_at, completed_at) "
    "WITH (DROP_EXISTING = ON)"
)
"""The run list filtered by index: the same columns, `search_index_id` being a key column."""


def cover_access_lists() -> None:
    op.execute(PROJECTS_STATUS_NAME)


def cover_ingestion_lists() -> None:
    op.execute(INGESTION_RUNS_CREATED)
    op.execute(INGESTION_RUNS_INDEX_CREATED)


def upgrade() -> None:
    cover_access_lists()
    cover_ingestion_lists()


def downgrade() -> None:
    raise NotImplementedError("roll forward")
