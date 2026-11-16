"""The upload operations against SQL Server and Azurite (T-063's upload half, T-135, T-138,
T-139, T-160 and the isolation entry `ing.uploads.other_index_refused`; `14-ingestion.md`
sections 2 and 5.5; `11-api.md` sections 3.11.5 to 3.11.9; `84-testing.md` section 4.2).

The in-process API runs as the `docchat_api` role of the session's database; its `documents`
container and ingestion queue are Azurite's (the session's suffixed queue), so every blob and
message the tests read is one the API wrote. Each test builds its own logical index with an
active physical index and a project mapped to it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobBlock, ContentSettings, StandardBlobTier
from sqlalchemy import Engine, func, insert, select, update

from docchat_api.routes.admin_uploads import (
    UploadStorage,
    block_id,
    commit_blob,
    upload_storage,
)
from docchat_api.settings import ProcessSettings
from docchat_core.ingestion.models import ContentType
from docchat_core.ingestion.names import source_path
from docchat_core.sql.engine import SqlEngine
from docchat_core.sql.tables.access import audit_events, project_indexes, projects
from docchat_core.sql.tables.ingestion import (
    document_versions,
    documents,
    ingestion_run_items,
    ingestion_runs,
    physical_indexes,
    scan_canaries,
    search_indexes,
)
from docchat_testing.builders import project_index_row, project_row
from docchat_testing.builders.ingestion import (
    document_row,
    document_version_row,
    physical_index_row,
    scan_canary_row,
    search_index_row,
)
from docchat_testing.builders.rows import sql_now
from docchat_testing.fixtures.api import ApiUnderTest, api_settings, ports_under_test, running_api
from docchat_testing.fixtures.identities import SharedPrincipal
from docchat_testing.fixtures.sql import SqlTestDatabase
from docchat_testing.fixtures.storage import StorageClients
from docchat_testing.tokens import SigningKeys

OCTET = {"Content-Type": "application/octet-stream"}
BLOCK = 4_194_304


def put(engine: Engine, *rows: tuple[Any, dict[str, Any]]) -> None:
    with engine.begin() as connection:
        for table, row in rows:
            connection.execute(insert(table), row)


def pdf(size: int, seed: bytes = b"0") -> bytes:
    return (b"%PDF-1.7\n" + seed * size)[:size]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Index:
    """A logical index with an active physical index and a project mapped to it."""

    id: UUID
    project_id: UUID

    @property
    def base(self) -> str:
        return f"/v1/admin/indexes/{self.id}/uploads"


def index_with_project(admin: Engine) -> Index:
    index = search_index_row(status="ready")
    physical = physical_index_row(index["id"], state="active", provisioned_at=sql_now())
    project = project_row()
    put(
        admin,
        (search_indexes, index),
        (physical_indexes, physical),
        (projects, project),
        (project_indexes, project_index_row(project["id"], index["id"], is_default=True)),
    )
    with admin.begin() as connection:
        connection.execute(
            update(search_indexes)
            .where(search_indexes.c.id == index["id"])
            .values(active_physical_index_id=physical["id"])
        )
    return Index(index["id"], project["id"])


def canary(admin: Engine, *, hours_ago: int) -> None:
    put(
        admin,
        (
            scan_canaries,
            scan_canary_row(result="clean", written_at=sql_now() - timedelta(hours=hours_ago)),
        ),
    )


def counts(admin: Engine) -> tuple[int, ...]:
    with admin.connect() as connection:
        return tuple(
            int(connection.execute(select(func.count()).select_from(table)).scalar_one())
            for table in (documents, document_versions, ingestion_runs, audit_events)
        )


def version_of(admin: Engine, version_id: str) -> Any:
    with admin.connect() as connection:
        return connection.execute(
            select(document_versions).where(document_versions.c.id == UUID(version_id))
        ).one()


@pytest.fixture
def admin(sql_database: SqlTestDatabase) -> Iterator[Engine]:
    engine = sql_database.engine()
    yield engine
    engine.dispose()


type Uploads = Callable[..., AbstractAsyncContextManager[ApiUnderTest]]


@pytest.fixture
async def uploads(
    api_sql_engine: SqlEngine, signing_keys: SigningKeys, storage: StorageClients
) -> AsyncIterator[Uploads]:
    """Start an in-process API over the session's database and Azurite, with the upload
    settings a test chooses (`storage_malware_scanning_enabled`, …)."""
    documents_container = storage.container("documents")
    queue = storage.queue()

    @asynccontextmanager
    async def started(**upload: Any) -> AsyncIterator[ApiUnderTest]:
        base = api_settings()
        settings: ProcessSettings = replace(base, upload=base.upload.model_copy(update=upload))
        async with running_api(
            settings=settings,
            ports=ports_under_test(sql_engine=api_sql_engine),
            signing_keys=signing_keys,
        ) as api:
            api.app.dependency_overrides[upload_storage] = lambda: UploadStorage(
                documents=cast("Any", documents_container), queue=cast("Any", queue)
            )
            yield api

    try:
        yield started
    finally:
        await api_sql_engine.dispose()


async def reserve(
    api: ApiUnderTest, carol: SharedPrincipal, index: Index, data: bytes, name: str = "Policy.pdf"
) -> Any:
    return await api.client.post(
        index.base,
        json={
            "name": name,
            "sizeBytes": len(data),
            "sha256": sha(data),
            "projectId": str(index.project_id),
        },
        headers=carol.authorization(),
    )


async def upload(
    api: ApiUnderTest,
    carol: SharedPrincipal,
    index: Index,
    version_id: str,
    data: bytes | AsyncIterator[bytes],
    *,
    length: int | None = None,
) -> Any:
    headers = OCTET | carol.authorization()
    if length is not None:
        headers["Content-Length"] = str(length)
    return await api.client.put(f"{index.base}/{version_id}/content", content=data, headers=headers)


async def blob_exists(storage: StorageClients, path: str) -> bool:
    try:
        await storage.container("documents").get_blob_client(path).get_blob_properties()
    except ResourceNotFoundError:
        return False
    return True


# --- The scanner-canary gate (T-160, T-063) ---------------------------------------------------


async def test_t160_t063_a_49_hour_old_canary_closes_all_four_operations(
    uploads: Uploads, admin: Engine, carol: SharedPrincipal
) -> None:
    index = index_with_project(admin)
    canary(admin, hours_ago=49)
    data = pdf(1000)
    async with uploads(storage_malware_scanning_enabled=True) as api:
        # The first request provisions the caller, which writes a row of its own.
        await api.client.get("/v1/me", headers=carol.authorization())
        before = counts(admin)
        answers = [
            await api.client.post(
                f"{index.base}/preflight",
                json={"files": [{"name": "a.pdf", "sizeBytes": 1000, "sha256": sha(data)}]},
                headers=carol.authorization(),
            ),
            await reserve(api, carol, index, data),
            await upload(api, carol, index, str(UUID(int=1)), data),
            await api.client.post(
                f"{index.base}/submit",
                json={"versionIds": [str(UUID(int=1))]},
                headers=carol.authorization(),
            ),
        ]

    for answer in answers:
        assert answer.status_code == 503
        assert answer.json()["code"] == "malware_scanning_not_ready"
        assert answer.json()["retryAfterSeconds"] == 3600
    assert counts(admin) == before


@pytest.mark.parametrize("scanning", [True, False])
async def test_t160_t063_a_47_hour_old_canary_or_scanning_off_opens_them(
    uploads: Uploads,
    admin: Engine,
    carol: SharedPrincipal,
    storage: StorageClients,
    scanning: bool,
) -> None:
    index = index_with_project(admin)
    if scanning:
        canary(admin, hours_ago=47)
    data = pdf(1000)
    async with uploads(storage_malware_scanning_enabled=scanning) as api:
        preflight = await api.client.post(
            f"{index.base}/preflight",
            json={"files": [{"name": "a.pdf", "sizeBytes": 1000, "sha256": sha(data)}]},
            headers=carol.authorization(),
        )
        reservation = await reserve(api, carol, index, data)
        version_id = reservation.json()["versionId"]
        content = await upload(api, carol, index, version_id, data)
        submitted = await api.client.post(
            f"{index.base}/submit", json={"versionIds": [version_id]}, headers=carol.authorization()
        )

    assert [answer.status_code for answer in (preflight, reservation, content, submitted)] == [
        200,
        201,
        200,
        202,
    ]
    assert preflight.json()["results"][0]["status"] == "new"
    run_id = submitted.json()["runId"]
    message = await storage.queue().receive_message()
    assert message is not None
    assert json.loads(message.content)["runId"] == run_id
    with admin.connect() as connection:
        run = connection.execute(
            select(ingestion_runs).where(ingestion_runs.c.id == UUID(run_id))
        ).one()
        items = connection.execute(
            select(ingestion_run_items).where(ingestion_run_items.c.run_id == UUID(run_id))
        ).all()
    assert (run.kind, run.priority, run.requested_by) == ("upload", 0, carol.oid)
    assert run.traceparent == json.loads(message.content)["traceparent"]
    assert [(item.operation, str(item.document_version_id)) for item in items] == [
        ("ingest", version_id)
    ]


# --- Streaming refusals (T-063, T-135) --------------------------------------------------------


@pytest.mark.parametrize(
    ("sent", "length", "status", "code"),
    [
        (b"PK\x03\x04" + b"0" * 996, None, 422, "file_signature_mismatch"),
        (pdf(1000, b"1"), None, 422, "hash_mismatch"),
        (pdf(999), None, 422, "size_mismatch"),
        (pdf(999), 1000, 422, "size_mismatch"),
        (pdf(1001), 1000, 422, "size_mismatch"),
        (pdf(1000), 52_428_801, 413, "file_too_large"),
    ],
)
async def test_t063_t135_a_refused_stream_commits_no_blob(
    *,
    uploads: Uploads,
    admin: Engine,
    carol: SharedPrincipal,
    storage: StorageClients,
    sent: bytes,
    length: int | None,
    status: int,
    code: str,
) -> None:
    index = index_with_project(admin)
    data = pdf(1000)
    async with uploads() as api:
        reservation = (await reserve(api, carol, index, data)).json()

        async def body() -> AsyncIterator[bytes]:
            yield sent

        answer = await upload(
            api,
            carol,
            index,
            reservation["versionId"],
            body() if length is not None else sent,
            length=length,
        )

    assert answer.status_code == status
    assert answer.json()["code"] == code
    version = version_of(admin, reservation["versionId"])
    assert version.state == "uploading"
    assert not await blob_exists(storage, version.blob_path)


async def test_t063_the_fifth_concurrent_stream_is_busy(
    uploads: Uploads, admin: Engine, carol: SharedPrincipal
) -> None:
    index = index_with_project(admin)
    gate = asyncio.Event()
    async with uploads() as api:
        reservations = []
        for number in range(5):
            data = pdf(2000, str(number).encode())
            answer = await reserve(api, carol, index, data, name=f"{number}.pdf")
            reservations.append((answer.json()["versionId"], data))

        def held(data: bytes) -> AsyncIterator[bytes]:
            async def pieces() -> AsyncIterator[bytes]:
                yield data[:1000]
                await gate.wait()
                yield data[1000:]

            return pieces()

        streams = [
            asyncio.create_task(upload(api, carol, index, version_id, held(data), length=2000))
            for version_id, data in reservations[:4]
        ]
        await asyncio.sleep(0.5)
        busy = await upload(api, carol, index, reservations[4][0], reservations[4][1])
        gate.set()
        finished = await asyncio.gather(*streams)

    assert busy.status_code == 429
    assert busy.json()["code"] == "upload_busy"
    assert busy.json()["retryAfterSeconds"] == 5
    assert [answer.status_code for answer in finished] == [200] * 4


async def test_t135_a_successful_upload_writes_its_audit_row(
    uploads: Uploads, admin: Engine, carol: SharedPrincipal, storage: StorageClients
) -> None:
    index = index_with_project(admin)
    data = pdf(BLOCK + 1000)
    async with uploads() as api:
        reservation = (await reserve(api, carol, index, data)).json()
        answer = await upload(api, carol, index, reservation["versionId"], data)

    assert answer.status_code == 200
    assert answer.json()["version"]["state"] == "pending"
    version = version_of(admin, reservation["versionId"])
    blob = storage.container("documents").get_blob_client(version.blob_path)
    properties = await blob.get_blob_properties()
    assert properties.content_settings.content_type == "application/pdf"
    downloaded = await (await blob.download_blob()).readall()
    assert sha(downloaded) == sha(data)
    with admin.connect() as connection:
        (row,) = connection.execute(
            select(audit_events).where(audit_events.c.action == "document.upload_committed")
        ).all()
    assert (row.actor_type, row.actor_object_id) == ("user", carol.oid)
    assert (row.target_type, row.target_id) == ("document_version", reservation["versionId"])
    assert row.project_id == index.project_id
    assert json.loads(row.metadata_json) == {
        "indexId": str(index.id),
        "documentId": reservation["documentId"],
        "versionNumber": 1,
        "sizeBytes": BLOCK + 1000,
        "contentType": "application/pdf",
    }


# --- The upload commit (T-097) ----------------------------------------------------------------


async def _staged_commit(blob: Any, data: bytes, nonce: str) -> Any:
    """Stage `data` as one block and commit it as the API does: `If-None-Match: *`, Cool."""
    identifier = block_id(nonce, 0)
    await blob.stage_block(identifier, data)
    return await blob.commit_block_list(
        [BlobBlock(block_id=identifier)],
        content_settings=ContentSettings(content_type="application/pdf"),
        standard_blob_tier=StandardBlobTier.COOL,
        match_condition=MatchConditions.IfMissing,
    )


async def test_t097_a_second_commit_to_a_written_source_path_is_refused_and_changes_nothing(
    storage: StorageClients,
) -> None:
    path = source_path(uuid4(), uuid4(), 1)
    blob = storage.container("documents").get_blob_client(path)
    first, second = pdf(1000), pdf(2000)
    await _staged_commit(blob, first, uuid4().hex)

    with pytest.raises(ResourceExistsError) as refused:
        await _staged_commit(blob, second, uuid4().hex)

    assert refused.value.status_code == 409
    assert getattr(refused.value, "error_code", None) == "BlobAlreadyExists"
    stored = await (await blob.download_blob()).readall()
    assert stored == first
    # The API's own commit takes the refusal as the path already written and reads its time.
    retry = block_id(uuid4().hex, 0)
    await blob.stage_block(retry, first)
    committed_at = await commit_blob(cast("Any", blob), [retry], ContentType.PDF)
    assert committed_at == (await blob.get_blob_properties()).last_modified
    assert await (await blob.download_blob()).readall() == first


# --- Commit idempotency (T-138, T-135) --------------------------------------------------------


async def test_t138_a_commit_retried_after_a_committed_attempt_meets_condition_not_met(
    uploads: Uploads, admin: Engine, carol: SharedPrincipal
) -> None:
    index = index_with_project(admin)
    data = pdf(1000)
    async with uploads() as api:
        version_id = (await reserve(api, carol, index, data)).json()["versionId"]
        assert (await upload(api, carol, index, version_id, data)).status_code == 200
        # The first attempt's SQL commit is undone, as if its answer had been lost before it
        # committed, so the retry stages, meets the committed blob and commits SQL itself.
        with admin.begin() as connection:
            connection.execute(
                update(document_versions)
                .where(document_versions.c.id == UUID(version_id))
                .values(state="uploading", committed_at=None, blob_last_modified=None)
            )
        retried = await upload(api, carol, index, version_id, data)
        repeated = await upload(api, carol, index, version_id, b"ignored")

    assert retried.status_code == 200
    assert repeated.status_code == 200
    version = version_of(admin, version_id)
    assert version.state == "pending"
    assert version.blob_last_modified is not None


async def test_t138_t135_two_concurrent_streams_for_one_version_commit_one_blob(
    uploads: Uploads, admin: Engine, carol: SharedPrincipal, storage: StorageClients
) -> None:
    index = index_with_project(admin)
    data = pdf(2 * BLOCK + 10)
    async with uploads() as api:
        version_id = (await reserve(api, carol, index, data)).json()["versionId"]
        answers = await asyncio.gather(
            upload(api, carol, index, version_id, data), upload(api, carol, index, version_id, data)
        )

    assert [answer.status_code for answer in answers] == [200, 200]
    version = version_of(admin, version_id)
    blob = storage.container("documents").get_blob_client(version.blob_path)
    downloaded = await (await blob.download_blob()).readall()
    assert sha(downloaded) == sha(data) == version.sha256.hex()
    with admin.connect() as connection:
        committed = connection.execute(
            select(func.count())
            .select_from(audit_events)
            .where(audit_events.c.action == "document.upload_committed")
        ).scalar_one()
    assert committed == 1


async def test_t135_the_same_bytes_under_another_name_fail_at_commit(
    uploads: Uploads, admin: Engine, carol: SharedPrincipal, storage: StorageClients
) -> None:
    index = index_with_project(admin)
    data = pdf(1000)
    async with uploads() as api:
        first = (await reserve(api, carol, index, data, name="a.pdf")).json()
        second = (await reserve(api, carol, index, data, name="b.pdf")).json()
        assert (await upload(api, carol, index, first["versionId"], data)).status_code == 200
        refused = await upload(api, carol, index, second["versionId"], data)

    assert refused.status_code == 409
    assert refused.json()["code"] == "duplicate_content"
    version = version_of(admin, second["versionId"])
    assert (version.state, version.failure_code) == ("failed", "duplicate_content")
    assert not await blob_exists(storage, version.blob_path)


# --- Reserve (T-139) --------------------------------------------------------------------------


async def test_t139_two_concurrent_first_reservations_create_one_document(
    uploads: Uploads, admin: Engine, carol: SharedPrincipal
) -> None:
    index = index_with_project(admin)
    data = pdf(1000)
    async with uploads() as api:
        answers = await asyncio.gather(
            reserve(api, carol, index, data, name="Report.pdf"),
            reserve(api, carol, index, data, name="report.PDF"),
        )

    assert [answer.status_code for answer in answers] == [201, 201]
    assert answers[0].json()["documentId"] == answers[1].json()["documentId"]
    assert answers[0].json()["versionId"] == answers[1].json()["versionId"]
    assert sorted(answer.json()["reused"] for answer in answers) == [False, True]
    with admin.connect() as connection:
        assert connection.execute(select(func.count()).select_from(documents)).scalar_one() == 1


@pytest.mark.parametrize(
    ("scanning", "required", "status"), [(True, True, "awaiting"), (False, False, "not_required")]
)
async def test_t139_the_version_records_the_scanning_setting_and_ids_only(
    *,
    uploads: Uploads,
    admin: Engine,
    carol: SharedPrincipal,
    scanning: bool,
    required: bool,
    status: str,
) -> None:
    index = index_with_project(admin)
    if scanning:
        canary(admin, hours_ago=1)
    data = pdf(1000)
    async with uploads(storage_malware_scanning_enabled=scanning) as api:
        reservation = (await reserve(api, carol, index, data, name="Name é.pdf")).json()
        again = (await reserve(api, carol, index, data, name="Name é.pdf")).json()
        await upload(api, carol, index, reservation["versionId"], data)

    assert again == reservation | {"reused": True}
    version = version_of(admin, reservation["versionId"])
    assert version.blob_path == f"{index.id}/{reservation['documentId']}/1/source"
    assert (version.state, version.scan_required, version.scan_status) == (
        "pending",
        required,
        status,
    )


async def test_t139_an_uploading_row_of_the_other_setting_isnt_reused(
    uploads: Uploads, admin: Engine, carol: SharedPrincipal
) -> None:
    index = index_with_project(admin)
    canary(admin, hours_ago=1)
    data = pdf(1000)
    async with uploads(storage_malware_scanning_enabled=False) as api:
        unscanned = (await reserve(api, carol, index, data)).json()
    async with uploads(storage_malware_scanning_enabled=True) as api:
        scanned = (await reserve(api, carol, index, data)).json()

    assert scanned["documentId"] == unscanned["documentId"]
    assert (unscanned["versionNumber"], scanned["versionNumber"]) == (1, 2)
    assert scanned["reused"] is False


async def test_t139_a_deleted_name_returns_under_its_document_id(
    uploads: Uploads, admin: Engine, carol: SharedPrincipal
) -> None:
    index = index_with_project(admin)
    deleted = document_row(
        index.id,
        source_name="Policy.pdf",
        normalized_name="policy.pdf",
        is_deleted=True,
        deleted_at=sql_now(),
        next_version_number=4,
    )
    put(admin, (documents, deleted))
    async with uploads() as api:
        reservation = (await reserve(api, carol, index, pdf(1000), name=" POLICY.pdf")).json()

    assert reservation["documentId"] == str(deleted["id"])
    assert reservation["versionNumber"] == 4
    with admin.connect() as connection:
        row = connection.execute(select(documents).where(documents.c.id == deleted["id"])).one()
    assert (row.is_deleted, row.source_name, row.updated_by) == (False, "POLICY.pdf", carol.oid)


# --- The isolation entry `ing.uploads.other_index_refused` (T-002) ----------------------------


async def test_t002_ing_uploads_other_index_refused(
    uploads: Uploads, admin: Engine, carol: SharedPrincipal
) -> None:
    index_x = index_with_project(admin)
    index_y = index_with_project(admin)
    document_y = document_row(index_y.id, source_name="Shared.pdf", normalized_name="shared.pdf")
    version_y = document_version_row(
        document_y["id"], index_y.id, index_y.project_id, state="pending", committed_at=sql_now()
    )
    put(admin, (documents, document_y), (document_versions, version_y))
    async with uploads() as api:
        submitted = await api.client.post(
            f"{index_x.base}/submit",
            json={"versionIds": [str(version_y["id"])]},
            headers=carol.authorization(),
        )
        content = await upload(api, carol, index_x, str(version_y["id"]), pdf(1024))
        reservation = await reserve(api, carol, index_x, pdf(1000), name="Shared.pdf")
        through_y = await api.client.post(
            index_x.base,
            json={
                "name": "Other.pdf",
                "sizeBytes": 1000,
                "sha256": sha(pdf(1000, b"9")),
                "projectId": str(index_y.project_id),
            },
            headers=carol.authorization(),
        )

    assert submitted.status_code == 200
    assert submitted.json() == {
        "runId": None,
        "results": [{"versionId": str(version_y["id"]), "outcome": "not_found"}],
    }
    assert content.status_code == 404
    assert content.json()["code"] == "version_not_found"
    assert reservation.status_code == 201
    assert reservation.json()["documentId"] != str(document_y["id"])
    assert through_y.status_code == 422
    assert through_y.json()["code"] == "project_not_mapped"
    with admin.connect() as connection:
        assert (
            connection.execute(select(func.count()).select_from(ingestion_runs)).scalar_one() == 0
        )
