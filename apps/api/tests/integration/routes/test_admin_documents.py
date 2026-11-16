"""The administrator document operations against SQL Server (`11-api.md` section 3.11's
document operations and acceptance criteria 6 and 21; `14-ingestion.md` sections 7.1 and 7.2;
T-021, T-049, T-054, T-056 and T-063's document half).

The app runs as the `docchat_api` role with the permissive limiters and a recording ingestion
queue; rows are arranged and read back as `sa`, because the API role can't read
`app.audit_events`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, Table, insert, select, update

from docchat_api.http.queue import ingestion_queue
from docchat_core.models import encode_row_version
from docchat_core.sql.tables.access import audit_events, projects
from docchat_core.sql.tables.ingestion import (
    document_versions,
    documents,
    ingestion_runs,
    physical_indexes,
    search_indexes,
)
from docchat_testing.builders import project_row
from docchat_testing.builders.ingestion import (
    document_row,
    document_version_row,
    physical_index_row,
    search_index_row,
)
from docchat_testing.builders.rows import sql_now
from docchat_testing.fakes import RecordingQueue
from docchat_testing.fixtures.api import ApiUnderTest
from docchat_testing.fixtures.identities import SharedPrincipal
from docchat_testing.fixtures.sql import SqlTestDatabase

pytestmark = pytest.mark.integration

STALE = '"AAAAAAAAAAE="'


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


def runs_of(engine: Engine, index_id: UUID) -> list[Any]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                select(ingestion_runs).where(ingestion_runs.c.search_index_id == index_id)
            ).all()
        )


class Place:
    """A ready index with its active physical index and the project its versions are uploaded
    through (mapped or not, the document operations don't ask)."""

    def __init__(self, engine: Engine) -> None:
        index = search_index_row(status="ready")
        physical = physical_index_row(index["id"], state="active", provisioned_at=sql_now())
        project = project_row()
        put(engine, (projects, project), (search_indexes, index), (physical_indexes, physical))
        with engine.begin() as connection:
            connection.execute(
                update(search_indexes)
                .where(search_indexes.c.id == index["id"])
                .values(active_physical_index_id=physical["id"])
            )
        self.engine = engine
        self.index_id = UUID(str(index["id"]))
        self.project_id = UUID(str(project["id"]))

    @property
    def base(self) -> str:
        return f"/v1/admin/indexes/{self.index_id}/documents"

    def document(self, name: str | None = None, *states: str) -> UUID:
        """A live document with one version per state, numbered from 1; the newest `ingested`
        one is current."""
        overrides = (
            {} if name is None else {"source_name": name, "normalized_name": name.casefold()}
        )
        document = document_row(self.index_id, **overrides)
        versions = [
            document_version_row(
                document["id"],
                self.index_id,
                self.project_id,
                version_number=number,
                state=state,
                committed_at=None if state in {"uploading", "failed"} else sql_now(),
                ingested_at=sql_now() if state == "ingested" else None,
            )
            for number, state in enumerate(states, start=1)
        ]
        put(
            self.engine,
            (documents, document),
            *((document_versions, version) for version in versions),
        )
        current = [version["id"] for version in versions if version["state"] == "ingested"]
        if current:
            with self.engine.begin() as connection:
                connection.execute(
                    update(documents)
                    .where(documents.c.id == document["id"])
                    .values(current_version_id=current[-1], next_version_number=len(versions) + 1)
                )
        return UUID(str(document["id"]))


async def names_of(
    api: ApiUnderTest, place: Place, caller: SharedPrincipal, **params: Any
) -> list[str]:
    response = await api.client.get(
        place.base, params={"limit": 100, **params}, headers=caller.authorization()
    )
    assert response.status_code == 200, response.text
    return [item["name"] for item in response.json()["items"]]


# --- adminListDocuments and adminGetDocument ------------------------------------------------


async def test_t054_document_pages_stay_complete_while_documents_are_added(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal
) -> None:
    place = Place(admin)
    names = ["b.pdf", "c.pdf", "d.pdf", "e.pdf", "f.pdf"]
    for name in names:
        place.document(name, "ingested")
    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        response = await api.client.get(place.base, params=params, headers=carol.authorization())
        assert response.status_code == 200
        body = response.json()
        seen.extend(item["name"] for item in body["items"])
        pages += 1
        if pages == 1:
            # One document sorts before the first page's cursor, one after it.
            place.document("a.pdf", "ingested")
            place.document("z.pdf", "ingested")
        cursor = body["nextCursor"]
        if cursor is None:
            break

    assert seen == [*names, "z.pdf"]
    assert pages == 3


async def test_the_list_filters_by_display_state_and_normalised_prefix(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal
) -> None:
    place = Place(admin)
    place.document("Policy A.pdf", "ingested")
    place.document("Policy B.pdf", "pending")
    place.document("Handbook.pdf", "failed")

    assert await names_of(api, place, carol, state="ingested") == ["Policy A.pdf"]
    assert await names_of(api, place, carol, state="in_progress") == ["Policy B.pdf"]
    assert await names_of(api, place, carol, state="failed") == ["Handbook.pdf"]
    assert await names_of(api, place, carol, q="  POLICY ") == ["Policy A.pdf", "Policy B.pdf"]
    assert await names_of(api, place, carol, q="policy", state="failed") == []


async def test_a_document_reads_its_versions_newest_first(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal
) -> None:
    place = Place(admin)
    document_id = place.document("Policy.pdf", "ingested", "failed")

    response = await api.client.get(f"{place.base}/{document_id}", headers=carol.authorization())

    assert response.status_code == 200
    body = response.json()
    assert [version["versionNumber"] for version in body["versions"]] == [2, 1]
    assert body["currentVersion"]["versionNumber"] == 1
    assert body["latestVersion"]["versionNumber"] == 2
    assert body["displayState"] == "failed"
    assert body["versions"][1]["uploadedViaProject"]["projectId"] == str(place.project_id)
    assert body["rowVersion"] == version_of(admin, documents, document_id)


async def test_another_indexs_document_is_not_found_under_this_index(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal
) -> None:
    here, there = Place(admin), Place(admin)
    document_id = there.document("Policy.pdf", "ingested")

    response = await api.client.get(f"{here.base}/{document_id}", headers=carol.authorization())

    assert (response.status_code, response.json()["code"]) == (404, "document_not_found")


# --- adminDeleteDocument --------------------------------------------------------------------


async def test_t021_deleting_a_document_starts_its_run_and_audits_it(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal, queue: RecordingQueue
) -> None:
    place = Place(admin)
    document_id = place.document("Policy.pdf", "ingested")
    path = f"{place.base}/{document_id}"

    response = await api.client.delete(
        path, headers=if_match(carol, version_of(admin, documents, document_id))
    )
    after = await api.client.get(path, headers=carol.authorization())

    assert response.status_code == 202
    run_id = response.json()["runId"]
    (run,) = runs_of(admin, place.index_id)
    assert (str(run.id), run.kind, run.requested_by) == (run_id, "document_delete", carol.oid)
    (row,) = audit_of(admin, document_id)
    assert (row.action, row.actor_object_id) == ("document.deleted", carol.oid)
    metadata = json.loads(row.metadata_json)
    assert (metadata["runId"], metadata["indexId"]) == (run_id, str(place.index_id))
    assert (after.status_code, after.json()["code"]) == (404, "document_not_found")
    assert [str(message.run_id) for message in queue.messages()] == [run_id]


# --- adminReingestDocument ------------------------------------------------------------------


async def test_reingesting_a_document_twice_reuses_the_open_run(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal, queue: RecordingQueue
) -> None:
    place = Place(admin)
    document_id = place.document("Policy.pdf", "ingested")
    path = f"{place.base}/{document_id}/reingest"

    first = await api.client.post(
        path, json={}, headers=if_match(carol, version_of(admin, documents, document_id))
    )
    second = await api.client.post(
        path, json={}, headers=if_match(carol, first.json()["document"]["rowVersion"])
    )

    assert first.status_code == 202
    assert second.status_code == 200
    assert first.json()["itemCount"] == 1
    assert (second.json()["runId"], second.json()["itemCount"]) == (first.json()["runId"], 0)
    (run,) = runs_of(admin, place.index_id)
    assert (str(run.id), run.kind) == (first.json()["runId"], "document_reingest")
    rows = audit_of(admin, document_id)
    assert [row.action for row in rows] == ["document.reingest_requested"] * 2
    assert [json.loads(row.metadata_json)["runId"] for row in rows] == [
        first.json()["runId"],
        None,
    ]
    assert [str(message.run_id) for message in queue.messages()] == [first.json()["runId"]]


async def test_a_document_without_a_current_version_cannot_be_reingested(
    api: ApiUnderTest, admin: Engine, carol: SharedPrincipal, queue: RecordingQueue
) -> None:
    place = Place(admin)
    document_id = place.document("Policy.pdf", "pending")

    response = await api.client.post(
        f"{place.base}/{document_id}/reingest",
        json={},
        headers=if_match(carol, version_of(admin, documents, document_id)),
    )

    assert (response.status_code, response.json()["code"]) == (409, "no_current_version")
    assert runs_of(admin, place.index_id) == []
    assert queue.sent == []


# --- T-049, T-056 and T-063: If-Match -------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [("DELETE", "", None), ("POST", "/reingest", {})],
    ids=["adminDeleteDocument", "adminReingestDocument"],
)
async def test_t063_t056_a_document_operation_needs_a_current_if_match(
    *,
    api: ApiUnderTest,
    admin: Engine,
    carol: SharedPrincipal,
    queue: RecordingQueue,
    method: str,
    suffix: str,
    body: dict[str, Any] | None,
) -> None:
    place = Place(admin)
    document_id = place.document("Policy.pdf", "ingested")
    before = version_of(admin, documents, document_id)
    path = f"{place.base}/{document_id}{suffix}"

    unknown = await api.client.request(
        method, f"{place.base}/{uuid4()}{suffix}", json=body, headers=carol.authorization()
    )
    missing = await api.client.request(method, path, json=body, headers=carol.authorization())
    stale = await api.client.request(
        method, path, json=body, headers=carol.authorization() | {"If-Match": STALE}
    )

    assert (unknown.status_code, unknown.json()["code"]) == (404, "document_not_found")
    assert (missing.status_code, missing.json()["code"]) == (428, "precondition_required")
    assert (stale.status_code, stale.json()["code"]) == (412, "precondition_failed")
    assert version_of(admin, documents, document_id) == before
    assert runs_of(admin, place.index_id) == []
    assert audit_of(admin, document_id) == []
    assert queue.sent == []
