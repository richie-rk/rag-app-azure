"""The administrator index operations against SQL Server (`11-api.md` section 3.10 and
acceptance criteria 6 and 21; `14-ingestion.md` sections 8.3 to 8.7; T-021, T-049, T-054,
T-056, T-063's index half and T-198's API half).

The app runs as the `docchat_api` role with the permissive limiters and a recording ingestion
queue; rows are arranged and read back as `sa`, because the API role can't read
`app.audit_events`. Each test builds its own indexes; the session's database is emptied after
every test.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, Table, func, insert, select, update

from docchat_api.http.queue import ingestion_queue
from docchat_core.ingestion.methods import RECURSIVE_V1
from docchat_core.models import encode_row_version
from docchat_core.sql.repositories import indexes as repo
from docchat_core.sql.tables.access import audit_events, project_indexes, projects
from docchat_core.sql.tables.ingestion import (
    document_versions,
    documents,
    ingestion_runs,
    physical_indexes,
    search_indexes,
)
from docchat_testing.builders import project_index_row, project_row
from docchat_testing.builders.ingestion import (
    document_row,
    document_version_row,
    physical_index_row,
    search_index_row,
)
from docchat_testing.builders.rows import compact_json, sql_now
from docchat_testing.fakes import RecordingQueue
from docchat_testing.fixtures.api import ApiUnderTest
from docchat_testing.fixtures.identities import SharedPrincipal
from docchat_testing.fixtures.sql import SqlTestDatabase

pytestmark = pytest.mark.integration

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
TRACEPARENT = f"00-{TRACE_ID}-00f067aa0ba902b7-01"
STALE = '"AAAAAAAAAAE="'
SEARCH_MAX_INDEXES = 15
"""`DOCCHAT_SEARCH_MAX_INDEXES` in the test settings (`docchat_testing.fixtures.api`)."""
CREATE_BODY = {
    "displayName": "Handbook",
    "methodKey": "recursive",
    "embeddingModel": "text-embedding-3-large",
}
"""An `IndexCreate` with its required fields only (`11-api.md` section 3.10.3)."""


@pytest.fixture
def admin(sql_database: SqlTestDatabase) -> Iterator[Engine]:
    engine = sql_database.engine()
    yield engine
    engine.dispose()


@pytest.fixture
def queue(api: ApiUnderTest) -> RecordingQueue:
    recording = RecordingQueue()
    api.app.dependency_overrides[ingestion_queue] = lambda: recording
    return recording


def put(engine: Engine, *rows: tuple[Any, dict[str, Any]]) -> None:
    with engine.begin() as connection:
        for table, row in rows:
            connection.execute(insert(table), row)


def count(engine: Engine, table: Table, *conditions: Any) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                select(func.count()).select_from(table).where(*conditions)
            ).scalar_one()
        )


def version_of(engine: Engine, table: Table, row_id: UUID) -> str:
    with engine.connect() as connection:
        value = connection.execute(
            select(table.c.row_version).where(table.c.id == row_id)
        ).scalar_one()
    return encode_row_version(bytes(value))


def if_match(caller: SharedPrincipal, version: str) -> dict[str, str]:
    return caller.authorization() | {"If-Match": f'"{version}"'}


def audit_of(engine: Engine, target_id: UUID) -> list[Any]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                select(audit_events)
                .where(audit_events.c.target_id == str(target_id))
                .order_by(audit_events.c.id)
            ).all()
        )


def status_of(engine: Engine, index_id: UUID) -> str:
    with engine.connect() as connection:
        return str(
            connection.execute(
                select(search_indexes.c.status).where(search_indexes.c.id == index_id)
            ).scalar_one()
        )


def ready_index(engine: Engine, **overrides: Any) -> UUID:
    """A `ready` index of the recursive method at its default parameters, with its active,
    provisioned generation 1, mapped to no project."""
    parameters = RECURSIVE_V1.default_parameters()
    index = search_index_row(
        method_key="recursive",
        method_version=1,
        method_parameters=compact_json(parameters.dumped()),
        method_parameter_hash=RECURSIVE_V1.parameter_hash(parameters),
        status="ready",
        **overrides,
    )
    physical = physical_index_row(index["id"], state="active", provisioned_at=sql_now())
    put(engine, (search_indexes, index), (physical_indexes, physical))
    with engine.begin() as connection:
        connection.execute(
            update(search_indexes)
            .where(search_indexes.c.id == index["id"])
            .values(active_physical_index_id=physical["id"])
        )
    return UUID(str(index["id"]))


def mapped_to_new_project(engine: Engine, index_id: UUID) -> UUID:
    project = project_row()
    put(
        engine,
        (projects, project),
        (project_indexes, project_index_row(project["id"], index_id, is_default=True)),
    )
    return UUID(str(project["id"]))


def ingested_document(engine: Engine, index_id: UUID) -> UUID:
    """A live document of the index whose ingested version 1 is current, uploaded through a
    project of its own."""
    project = project_row()
    document = document_row(index_id)
    version = document_version_row(
        document["id"],
        index_id,
        project["id"],
        state="ingested",
        committed_at=sql_now(),
        ingested_at=sql_now(),
    )
    put(engine, (projects, project), (documents, document), (document_versions, version))
    with engine.begin() as connection:
        connection.execute(
            update(documents)
            .where(documents.c.id == document["id"])
            .values(current_version_id=version["id"])
        )
    return UUID(str(document["id"]))


async def fill_search_service(engine: Engine, physical_index_count: int) -> None:
    """Indexes whose active physical indexes take `physical_index_count` slots, written in a
    worker thread: so many synchronous round trips would hold the test's loop past the
    guard's 100 ms (`84-testing.md` section 2.8)."""

    def fill() -> None:
        for _ in range(physical_index_count):
            ready_index(engine)

    await asyncio.to_thread(fill)


def runs_of(engine: Engine, index_id: UUID) -> list[Any]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                select(ingestion_runs).where(ingestion_runs.c.search_index_id == index_id)
            ).all()
        )


# --- Options and reads ----------------------------------------------------------------------


async def test_the_options_list_the_active_methods_and_the_upload_limits(
    api: ApiUnderTest, carol: SharedPrincipal
) -> None:
    response = await api.client.get("/v1/admin/index-options", headers=carol.authorization())

    assert response.status_code == 200
    body = response.json()
    assert {"recursive", "pagewise"} <= {method["key"] for method in body["methods"]}
    assert body["defaults"]["methodKey"] == "recursive"
    assert body["defaults"]["languageAnalyzer"] == "standard.lucene"
    assert body["uploadLimits"]["allowedExtensions"] == [".pdf", ".docx", ".pptx"]


async def test_an_index_detail_names_its_mapped_projects_and_counts(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal
) -> None:
    index_id = ready_index(admin)
    project_id = mapped_to_new_project(admin, index_id)
    ingested_document(admin, index_id)

    response = await api.client.get(f"/v1/admin/indexes/{index_id}", headers=carol.authorization())

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["liveDocumentCount"] == 1
    assert [project["projectId"] for project in body["mappedProjects"]] == [str(project_id)]
    assert body["activePhysicalIndex"]["generation"] == 1
    assert body["openRun"] is None
    assert body["rowVersion"] == version_of(admin, search_indexes, index_id)


async def test_t054_index_pages_stay_complete_while_indexes_are_added(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal
) -> None:
    names = ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]
    for name in names:
        ready_index(admin, display_name=name)
    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        response = await api.client.get(
            "/v1/admin/indexes", params=params, headers=carol.authorization()
        )
        assert response.status_code == 200
        body = response.json()
        seen.extend(item["displayName"] for item in body["items"])
        pages += 1
        if pages == 1:
            # One index sorts before the cursor, one after it.
            ready_index(admin, display_name="Aardvark")
            ready_index(admin, display_name="Zulu")
        cursor = body["nextCursor"]
        if cursor is None:
            break

    # Aardvark sorts before the first page's cursor, so the pages never reach it; Zulu sorts
    # after it and arrives on the last page.
    assert seen == [*names, "Zulu"]
    assert pages == 3


# --- adminCreateIndex -----------------------------------------------------------------------


async def test_t021_creating_an_index_writes_its_run_and_audit_row(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal, queue: RecordingQueue
) -> None:
    response = await api.client.post(
        "/v1/admin/indexes",
        json=CREATE_BODY,
        headers=carol.authorization() | {"traceparent": TRACEPARENT},
    )

    assert response.status_code == 202
    body = response.json()
    index_id, run_id = UUID(body["index"]["indexId"]), UUID(body["runId"])
    assert body["index"]["status"] == "provisioning"
    assert body["index"]["buildingPhysicalIndex"]["generation"] == 1
    (run,) = runs_of(admin, index_id)
    assert (run.id, run.kind, run.requested_by) == (run_id, "index_create", carol.oid)
    (row,) = audit_of(admin, index_id)
    assert (row.action, row.actor_object_id, row.correlation_id) == (
        "index.created",
        carol.oid,
        TRACE_ID,
    )
    assert json.loads(row.metadata_json)["runId"] == str(run_id)
    assert [message.run_id for message in queue.messages()] == [run_id]


async def test_t021_a_forced_audit_failure_rolls_the_creation_back(
    api: ApiUnderTest,
    admin: Engine,
    carol: SharedPrincipal,
    queue: RecordingQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_audit(connection: Any, event: Any) -> None:
        # An action outside `CK_audit_events_action` fails the insert like any audit failure.
        connection.execute(
            insert(audit_events).values(
                actor_type="user",
                actor_object_id=event.actor_object_id,
                action="index.forced_failure",
                target_type="search_index",
            )
        )

    monkeypatch.setattr(repo, "write_audit_event", failing_audit)

    response = await api.client.post(
        "/v1/admin/indexes",
        json=CREATE_BODY,
        headers=carol.authorization(),
    )

    assert response.status_code == 500
    assert count(admin, search_indexes) == 0
    assert count(admin, ingestion_runs) == 0
    assert queue.sent == []


async def test_a_display_name_in_use_is_refused(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal, queue: RecordingQueue
) -> None:
    ready_index(admin, display_name="Handbook")

    response = await api.client.post(
        "/v1/admin/indexes",
        json=CREATE_BODY,
        headers=carol.authorization(),
    )

    assert (response.status_code, response.json()["code"]) == (409, "display_name_taken")
    assert count(admin, search_indexes) == 1
    assert queue.sent == []


async def test_t198_a_creation_that_leaves_no_free_slot_is_refused(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal, queue: RecordingQueue
) -> None:
    # Creation keeps one slot free for a rebuild: 14 taken slots of 15 leave no room.
    await fill_search_service(admin, SEARCH_MAX_INDEXES - 1)

    response = await api.client.post(
        "/v1/admin/indexes",
        json=CREATE_BODY,
        headers=carol.authorization(),
    )

    assert (response.status_code, response.json()["code"]) == (409, "search_index_capacity")
    assert count(admin, search_indexes) == SEARCH_MAX_INDEXES - 1
    assert queue.sent == []


# --- adminUpdateIndex and adminStartReingest ------------------------------------------------


async def test_t021_an_update_records_its_changed_fields(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal
) -> None:
    index_id = ready_index(admin)

    response = await api.client.patch(
        f"/v1/admin/indexes/{index_id}",
        json={"displayName": "Renamed"},
        headers=if_match(carol, version_of(admin, search_indexes, index_id)),
    )

    assert response.status_code == 200
    assert response.json()["displayName"] == "Renamed"
    assert response.json()["rowVersion"] == version_of(admin, search_indexes, index_id)
    (row,) = audit_of(admin, index_id)
    assert row.action == "index.updated"
    assert json.loads(row.metadata_json) == {"changedFields": ["displayName"], "status": "ready"}


async def test_t056_an_update_that_changes_nothing_returns_the_unchanged_version(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal
) -> None:
    index_id = ready_index(admin, display_name="Handbook")
    before = version_of(admin, search_indexes, index_id)

    response = await api.client.patch(
        f"/v1/admin/indexes/{index_id}",
        json={"displayName": "Handbook"},
        headers=if_match(carol, before),
    )

    assert response.status_code == 200
    assert response.json()["rowVersion"] == before
    assert version_of(admin, search_indexes, index_id) == before
    assert audit_of(admin, index_id) == []


async def test_a_same_index_reingest_queues_every_current_version(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal, queue: RecordingQueue
) -> None:
    index_id = ready_index(admin)
    ingested_document(admin, index_id)

    response = await api.client.post(
        f"/v1/admin/indexes/{index_id}/reingest",
        json={},
        headers=if_match(carol, version_of(admin, search_indexes, index_id)),
    )

    assert response.status_code == 202
    body = response.json()
    assert (body["mode"], body["itemCount"]) == ("same_index", 1)
    assert body["index"]["status"] == "reingesting"
    (run,) = runs_of(admin, index_id)
    assert (str(run.id), run.kind) == (body["runId"], "index_reingest")
    assert [row.action for row in audit_of(admin, index_id)] == ["index.reingest_started"]
    assert [str(message.run_id) for message in queue.messages()] == [body["runId"]]


async def test_t198_a_rebuild_without_a_free_slot_is_refused(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal, queue: RecordingQueue
) -> None:
    index_id = ready_index(admin)
    # The index's own active physical index and 14 others take every slot of 15.
    await fill_search_service(admin, SEARCH_MAX_INDEXES - 1)
    path = f"/v1/admin/indexes/{index_id}"

    updated = await api.client.patch(
        path,
        json={"languageAnalyzer": "en.microsoft"},
        headers=if_match(carol, version_of(admin, search_indexes, index_id)),
    )
    refused = await api.client.post(
        f"{path}/reingest", json={}, headers=if_match(carol, updated.json()["rowVersion"])
    )

    assert updated.status_code == 200
    assert (updated.json()["status"], updated.json()["reingestRequired"]) == (
        "reingest_required",
        True,
    )
    assert (refused.status_code, refused.json()["code"]) == (409, "search_index_capacity")
    assert status_of(admin, index_id) == "reingest_required"
    assert runs_of(admin, index_id) == []
    assert queue.sent == []


# --- adminDeleteIndex -----------------------------------------------------------------------


async def test_t021_deleting_an_unmapped_index_starts_its_delete_run(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal, queue: RecordingQueue
) -> None:
    index_id = ready_index(admin)

    response = await api.client.delete(
        f"/v1/admin/indexes/{index_id}",
        headers=if_match(carol, version_of(admin, search_indexes, index_id)),
    )

    assert response.status_code == 202
    body = response.json()
    assert body["index"]["status"] == "deleting"
    (run,) = runs_of(admin, index_id)
    assert (str(run.id), run.kind) == (body["runId"], "index_delete")
    (row,) = audit_of(admin, index_id)
    assert row.action == "index.deleted"
    assert json.loads(row.metadata_json)["physicalIndexCount"] == 1
    assert [str(message.run_id) for message in queue.messages()] == [body["runId"]]


async def test_t049_t063_a_mapped_index_answers_404_412_then_409(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal
) -> None:
    index_id = ready_index(admin)
    mapped_to_new_project(admin, index_id)
    current = version_of(admin, search_indexes, index_id)

    unknown = await api.client.delete(f"/v1/admin/indexes/{uuid4()}", headers=carol.authorization())
    stale = await api.client.delete(
        f"/v1/admin/indexes/{index_id}", headers=carol.authorization() | {"If-Match": STALE}
    )
    mapped = await api.client.delete(
        f"/v1/admin/indexes/{index_id}", headers=if_match(carol, current)
    )

    assert (unknown.status_code, unknown.json()["code"]) == (404, "index_not_found")
    assert (stale.status_code, stale.json()["code"]) == (412, "precondition_failed")
    assert (mapped.status_code, mapped.json()["code"]) == (409, "index_mapped")
    assert status_of(admin, index_id) == "ready"
    assert version_of(admin, search_indexes, index_id) == current


# --- T-063: If-Match ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("PATCH", "", {"description": "Reviewed"}),
        ("POST", "/reingest", {}),
        ("DELETE", "", None),
    ],
    ids=["adminUpdateIndex", "adminStartReingest", "adminDeleteIndex"],
)
async def test_t063_t056_an_index_operation_needs_a_current_if_match(
    *,
    api: ApiUnderTest,
    admin: Engine,
    carol: SharedPrincipal,
    queue: RecordingQueue,
    method: str,
    suffix: str,
    body: dict[str, Any] | None,
) -> None:
    index_id = ready_index(admin)
    ingested_document(admin, index_id)
    before = version_of(admin, search_indexes, index_id)
    path = f"/v1/admin/indexes/{index_id}{suffix}"

    missing = await api.client.request(method, path, json=body, headers=carol.authorization())
    stale = await api.client.request(
        method, path, json=body, headers=carol.authorization() | {"If-Match": STALE}
    )

    assert (missing.status_code, missing.json()["code"]) == (428, "precondition_required")
    assert (stale.status_code, stale.json()["code"]) == (412, "precondition_failed")
    assert version_of(admin, search_indexes, index_id) == before
    assert runs_of(admin, index_id) == []
    assert audit_of(admin, index_id) == []
    assert queue.sent == []
