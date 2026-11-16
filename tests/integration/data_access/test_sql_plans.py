"""T-067, the plan integration test (`84-testing.md` section 2.11; `11-api.md` acceptance
criterion 26; `80-data-model.md` section 2).

The session's SQL Server database is seeded with the production-shaped volumes of section 2.11:
10,000 users, 2,000 projects, 100,000 memberships, 200 logical and 220 physical indexes, 50,000
documents, 100,000 versions, 100,000 chunk sets with the current ones served, 2,000 runs and
20,000 run items. The rows every registered repository function's `example_params` name (fixed
identifiers, read from the registry itself) are seeded beside them, so each function finds the
rows its statements look up. Then every `@request_path` function runs once as the `docchat_api`
role, in an order where each one finds the state it needs (reads before the writes that change
their rows), and `data_audit.PlanAudit` reads each statement's plan under `SET SHOWPLAN_XML ON`:
a `Table Scan`, `Clustered Index Scan` or `Index Scan` that can read more than 1,000 rows fails
the test. Index seeks pass, and so do the bounded range scans of T-067: a scan a `TOP` of at most
1,000 rows reads from directly (a keyset list's first page, read from the start of its index) and
a scan of a filtered index whose filter admits at most 1,000 rows (`IX_projects_default` in the
provisioning insert). The full `app.ops_config` read isn't a registered function, so it is never
audited.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from itertools import islice
from typing import Any, Final
from uuid import UUID

import pytest
from sqlalchemy import Connection, Engine, Table, insert, select, update

from docchat_core.ingestion.models import IngestionRefusedError
from docchat_core.sql.engine import SqlEngine
from docchat_core.sql.repositories.indexes import IndexSettingsRefusedError
from docchat_core.sql.repositories.uploads import UploadRefusedError
from docchat_core.sql.tables.access import project_indexes, project_members, projects, users
from docchat_core.sql.tables.ingestion import (
    chunk_sets,
    document_versions,
    documents,
    ingestion_run_items,
    ingestion_runs,
    physical_indexes,
    search_indexes,
    served_chunk_sets,
)
from docchat_testing.builders import (
    project_index_row,
    project_member_row,
    project_row,
    search_index_row,
    user_row,
)
from docchat_testing.builders.ingestion import (
    chunk_set_row,
    document_row,
    document_version_row,
    ingestion_run_item_row,
    ingestion_run_row,
    physical_index_row,
    served_chunk_set_row,
)
from docchat_testing.builders.rows import sql_now
from docchat_testing.data_audit import PlanAudit, format_plan_findings, request_paths
from docchat_testing.fixtures.sql import SqlTestDatabase

USERS: Final = 10_000
PROJECTS: Final = 2_000
MEMBERS_PER_PROJECT: Final = 50
INDEXES: Final = 200
RETIRED_PHYSICAL: Final = 20
DOCUMENTS_PER_INDEX: Final = 250
RUNS_PER_INDEX: Final = 10
ITEMS_PER_RUN: Final = 10
INSERT_CHUNK: Final = 2_000
PACKAGE: Final = "docchat_core.sql.repositories"
TOLERATED: Final = (IngestionRefusedError, UploadRefusedError, IndexSettingsRefusedError)
"""The refusals of a state rule an example row doesn't meet; the statements sent before it are
still audited."""

REGISTRY: Final = request_paths()

ORDER: Final = tuple(
    f"{PACKAGE}.{name}"
    for name in (
        "access.read_active_project_ids",
        "access.read_all_active_projects",
        "access.read_member_project_ids",
        "access.read_member_projects",
        "access.read_project_for_chat",
        "access.read_index_choice",
        "access.read_index_record",
        "retrieval.read_served_chunk_sets",
        "citations.read_cited_document",
        "citations.read_document_pdf",
        "documents.read_documents",
        "documents.read_document",
        "documents.reingest_document",
        "documents.delete_document",
        "indexes.read_indexes",
        "indexes.read_index",
        "indexes.read_index_options",
        "indexes.update_index",
        "indexes.create_index",
        "indexes.start_reingest",
        "indexes.delete_index",
        "projects.create_project",
        "projects.project_exists",
        "projects.read_admin_projects",
        "projects.read_admin_project",
        "projects.update_project",
        "projects.add_member",
        "projects.read_project_members",
        "projects.map_index",
        "projects.read_project_mappings",
        "projects.remove_member",
        "projects.unmap_index",
        "runs.read_runs",
        "runs.read_run",
        "uploads.read_upload_gate",
        "uploads.read_preflight_matches",
        "uploads.check_project_mapped",
        "uploads.read_upload_version",
        "uploads.reserve_upload",
        "uploads.commit_upload",
        "uploads.submit_uploads",
        "uploads.fail_upload",
        "users.read_users",
        "users.provision_user",
    )
)


def example(name: str, parameter: str) -> Any:
    """A registered function's example parameter: the fixed identifiers the seed must hold."""
    return REGISTRY[f"{PACKAGE}.{name}"].example_params[parameter]


def chunked(
    rows: Iterator[dict[str, Any]], size: int = INSERT_CHUNK
) -> Iterator[list[dict[str, Any]]]:
    while batch := list(islice(rows, size)):
        yield batch


def bulk(connection: Connection, table: Table, rows: Iterator[dict[str, Any]]) -> None:
    for batch in chunked(rows):
        connection.execute(insert(table), batch)


# --- The volumes ----------------------------------------------------------------------------------


def seed_access(connection: Connection) -> list[UUID]:
    """10,000 users, 2,000 projects of 50 members each (each user in 10 projects); the project
    IDs."""
    people = [user_row() for _ in range(USERS)]
    bulk(connection, users, iter(people))
    groups = [project_row() for _ in range(PROJECTS)]
    bulk(connection, projects, iter(groups))
    bulk(
        connection,
        project_members,
        (
            project_member_row(
                people[(number * MEMBERS_PER_PROJECT + member) % USERS]["object_id"], group["id"]
            )
            for number, group in enumerate(groups)
            for member in range(MEMBERS_PER_PROJECT)
        ),
    )
    return [group["id"] for group in groups]


def seed_ingestion(connection: Connection, project_ids: Sequence[UUID]) -> None:
    """200 ready logical indexes, each the default of 10 projects with an active physical index
    (and 20 with a retired one beside it); 250 documents per index, each with a superseded
    version 1 whose chunk set is retired and a current version 2 whose chunk set is served; 10
    completed runs per index of 10 succeeded items each."""
    now = sql_now()
    indexes = [search_index_row(status="ready") for _ in range(INDEXES)]
    bulk(connection, search_indexes, iter(indexes))
    active = [
        physical_index_row(index["id"], generation=2, state="active", provisioned_at=now)
        for index in indexes
    ]
    retired = [
        physical_index_row(index["id"], generation=1, state="deleted", provisioned_at=now)
        for index in indexes[:RETIRED_PHYSICAL]
    ]
    bulk(connection, physical_indexes, iter([*active, *retired]))
    connection.execute(
        update(search_indexes)
        .where(search_indexes.c.active_physical_index_id.is_(None))
        .values(
            active_physical_index_id=select(physical_indexes.c.id)
            .where(
                physical_indexes.c.search_index_id == search_indexes.c.id,
                physical_indexes.c.state == "active",
            )
            .scalar_subquery()
        )
    )
    bulk(
        connection,
        project_indexes,
        (
            project_index_row(project, indexes[number % INDEXES]["id"], is_default=True)
            for number, project in enumerate(project_ids)
        ),
    )
    for index, physical, project in zip(indexes, active, project_ids, strict=False):
        seed_documents(connection, index["id"], physical["id"], project)
        runs = [
            ingestion_run_row(index["id"], state="completed", completed_at=now)
            for _ in range(RUNS_PER_INDEX)
        ]
        bulk(connection, ingestion_runs, iter(runs))
        bulk(
            connection,
            ingestion_run_items,
            (
                ingestion_run_item_row(run["id"], index["id"], state="succeeded")
                for run in runs
                for _ in range(ITEMS_PER_RUN)
            ),
        )
    connection.execute(
        update(documents)
        .where(documents.c.current_version_id.is_(None))
        .values(
            current_version_id=select(document_versions.c.id)
            .where(
                document_versions.c.document_id == documents.c.id,
                document_versions.c.state == "ingested",
            )
            .scalar_subquery(),
            next_version_number=3,
        )
    )


def seed_documents(connection: Connection, index: UUID, physical: UUID, project: UUID) -> None:
    now = sql_now()
    rows = [document_row(index) for _ in range(DOCUMENTS_PER_INDEX)]
    bulk(connection, documents, iter(rows))
    older = [
        document_version_row(
            row["id"],
            index,
            project,
            state="superseded",
            committed_at=now,
            ingested_at=now,
            superseded_at=now,
        )
        for row in rows
    ]
    current = [
        document_version_row(
            row["id"],
            index,
            project,
            version_number=2,
            state="ingested",
            committed_at=now,
            ingested_at=now,
        )
        for row in rows
    ]
    bulk(connection, document_versions, iter([*older, *current]))
    retired = [
        chunk_set_row(version["document_id"], version["id"], physical, state="retired")
        for version in older
    ]
    served = [
        chunk_set_row(
            version["document_id"], version["id"], physical, state="served", served_at=now
        )
        for version in current
    ]
    bulk(connection, chunk_sets, iter([*retired, *served]))
    bulk(
        connection,
        served_chunk_sets,
        (
            served_chunk_set_row(
                chunk["document_id"], physical, chunk["id"], chunk["document_version_id"]
            )
            for chunk in served
        ),
    )


# --- The rows the examples name -------------------------------------------------------------------


def put(connection: Connection, *rows: tuple[Table, Mapping[str, Any]]) -> None:
    for table, row in rows:
        connection.execute(insert(table), dict(row))


def ready_index(connection: Connection, index_id: UUID, physical_id: UUID | None = None) -> UUID:
    """A ready index with an active physical index; the physical index's ID."""
    put(connection, (search_indexes, search_index_row(id=index_id, status="ready")))
    physical = physical_index_row(index_id, state="active", provisioned_at=sql_now())
    if physical_id is not None:
        physical["id"] = physical_id
    put(connection, (physical_indexes, physical))
    connection.execute(
        update(search_indexes)
        .where(search_indexes.c.id == index_id)
        .values(active_physical_index_id=physical["id"])
    )
    return UUID(str(physical["id"]))


def served_document(
    connection: Connection,
    *,
    index: UUID,
    physical: UUID,
    project: UUID,
    document_id: UUID | None = None,
    version_id: UUID | None = None,
    chunk_set_id: UUID | None = None,
) -> None:
    """A live document of `index` whose ingested version 1 is current and served."""
    now = sql_now()
    document = document_row(index, **({"id": document_id} if document_id else {}))
    version = document_version_row(
        document["id"],
        index,
        project,
        state="ingested",
        committed_at=now,
        ingested_at=now,
        page_count=1,
        document_pdf_status="built",
    )
    if version_id is not None:
        version["id"] = version_id
    chunk = chunk_set_row(
        document["id"],
        version["id"],
        physical,
        state="served",
        served_at=now,
        pdf_sha256=version["sha256"],
        chunk_count=1,
    )
    if chunk_set_id is not None:
        chunk["id"] = chunk_set_id
    put(
        connection,
        (documents, document),
        (document_versions, version),
        (chunk_sets, chunk),
        (
            served_chunk_sets,
            served_chunk_set_row(document["id"], physical, chunk["id"], version["id"]),
        ),
    )
    connection.execute(
        update(documents)
        .where(documents.c.id == document["id"])
        .values(current_version_id=version["id"], next_version_number=2)
    )


def seed_examples(connection: Connection) -> None:
    oid = example("access.read_index_choice", "oid")
    access_project = example("access.read_index_choice", "project_id")
    access_index = example("access.read_index_record", "search_index_id")
    access_physical = example("retrieval.read_served_chunk_sets", "physical_index_id")
    cited_project = example("citations.read_cited_document", "project_id")
    cited_index = example("citations.read_document_pdf", "index_id")
    upload = example("uploads.commit_upload", "version")
    upload_project = example("uploads.check_project_mapped", "project_id")
    upload_index = example("uploads.read_upload_gate", "index_id")
    put(
        connection,
        (users, user_row(object_id=oid)),
        (users, user_row(object_id=example("projects.add_member", "user_id"))),
        (users, user_row(object_id=upload.uploaded_by)),
        (projects, project_row(id=access_project)),
        (projects, project_row(id=cited_project)),
        (projects, project_row(id=upload_project)),
        (projects, project_row(id=example("users.read_users", "project_id"))),
        (project_members, project_member_row(oid, access_project)),
    )
    ready_index(connection, access_index, access_physical)
    cited_physical = ready_index(connection, cited_index)
    ready_index(connection, example("projects.map_index", "index_id"))
    ready_index(connection, upload_index)
    put(
        connection,
        (project_indexes, project_index_row(access_project, access_index, is_default=True)),
        (project_indexes, project_index_row(cited_project, cited_index, is_default=True)),
        (project_indexes, project_index_row(upload_project, upload_index, is_default=True)),
    )
    served_document(
        connection,
        index=access_index,
        physical=access_physical,
        project=access_project,
        document_id=example("documents.read_document", "document_id"),
    )
    served_document(
        connection,
        index=cited_index,
        physical=cited_physical,
        project=cited_project,
        document_id=example("citations.read_cited_document", "document_id"),
        version_id=example("citations.read_cited_document", "document_version_id"),
        chunk_set_id=example("citations.read_cited_document", "chunk_set_id"),
    )
    uploaded = document_row(upload_index, id=upload.document_id)
    put(
        connection,
        (documents, uploaded),
        (
            document_versions,
            document_version_row(
                upload.document_id,
                upload_index,
                upload_project,
                id=upload.id,
                sha256=upload.sha256,
                size_bytes=upload.size_bytes,
                uploaded_by=upload.uploaded_by,
            ),
        ),
        (
            ingestion_runs,
            ingestion_run_row(
                access_index,
                id=example("runs.read_run", "run_id"),
                state="completed",
                completed_at=sql_now(),
            ),
        ),
    )


@pytest.fixture
def seeded(sql_database: SqlTestDatabase) -> Iterator[Engine]:
    """`sa` on the seeded database; the SQL fixture's clean-up empties it after the test."""
    engine = sql_database.engine()
    with engine.begin() as connection:
        project_ids = seed_access(connection)
        seed_ingestion(connection, project_ids)
        seed_examples(connection)
    yield engine
    engine.dispose()


def test_t067_the_order_runs_every_registered_function_once() -> None:
    assert sorted(ORDER) == sorted(REGISTRY)
    assert len(set(ORDER)) == len(ORDER)


@pytest.mark.timeout(1_800)
async def test_t067_every_request_path_statement_seeks_on_the_large_tables(
    seeded: Engine, api_sql_engine: SqlEngine
) -> None:
    findings = []
    try:
        for key in ORDER:
            audit = PlanAudit(seeded, registry={key: REGISTRY[key]}, tolerated=TOLERATED)
            findings.extend(await audit.run(api_sql_engine))
    finally:
        await api_sql_engine.dispose()

    assert findings == [], format_plan_findings(findings)
