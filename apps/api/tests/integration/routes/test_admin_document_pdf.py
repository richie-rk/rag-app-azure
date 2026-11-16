"""`adminGetDocumentPdf` against SQL Server, the Cosmos DB emulator and Azurite (T-068;
`11-api.md` section 3.11.10 and acceptance criterion 27; `12-chat-pipeline.md` section 16.5).

The in-process API runs as the `docchat_api` role of the session's database; its `usage`
container is the session's emulator container (the administrator's download window and leases),
and its `renditions` container client is Azurite's, built with a recording policy (the blob
recorder), so a test proves the API read only `renditions/…/document.pdf` and nothing under
`documents/`. Each test lays out its own logical index and documents and clears `carol`'s
download windows and leases first.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, replace
from typing import Any, Final, override
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import pytest
from azure.core.pipeline import PipelineRequest
from azure.core.pipeline.policies import SansIOHTTPPolicy
from azure.cosmos.aio import DatabaseProxy
from azure.cosmos.exceptions import CosmosResourceNotFoundError
from azure.storage.blob.aio import BlobServiceClient, ContainerClient
from sqlalchemy import Engine, insert, update
from starlette.types import Message

from docchat_api.chat.citations import renditions_container
from docchat_api.http.headers import content_disposition
from docchat_core.clients.emulators import AZURITE_CONNECTION_STRING
from docchat_core.cosmos.containers import USAGE
from docchat_core.cosmos.usage import download_lease_id, download_rate_window_id, epoch_minute
from docchat_core.ingestion.models import ContentType
from docchat_core.ingestion.names import rendition_document_path
from docchat_core.sql.engine import SqlEngine
from docchat_core.sql.tables.access import projects
from docchat_core.sql.tables.ingestion import (
    document_versions,
    documents,
    physical_indexes,
    search_indexes,
)
from docchat_testing.builders import project_row
from docchat_testing.builders.cosmos import cosmos_now
from docchat_testing.builders.ingestion import (
    document_row,
    document_version_row,
    physical_index_row,
    search_index_row,
)
from docchat_testing.builders.rows import sql_now
from docchat_testing.fixtures.api import (
    ApiUnderTest,
    BuiltApp,
    build_app,
    ports_under_test,
    serving,
)
from docchat_testing.fixtures.cosmos import EmulatorContainers
from docchat_testing.fixtures.identities import SharedPrincipal
from docchat_testing.fixtures.sql import SqlTestDatabase
from docchat_testing.fixtures.storage import StorageClients, write_blob
from docchat_testing.telemetry_capture import TelemetryCapture
from docchat_testing.tokens import SigningKeys

MIB: Final = 1_048_576
WHOLE_PDF: Final = b"%PDF-1.7\n% whole document\n" + b"2" * (2 * MIB) + b"\n%%EOF\n"
EXTENSIONS: Final = {ContentType.PDF: "pdf", ContentType.DOCX: "docx", ContentType.PPTX: "pptx"}


def put(engine: Engine, *rows: tuple[Any, dict[str, Any]]) -> None:
    with engine.begin() as connection:
        for table, row in rows:
            connection.execute(insert(table), row)


@dataclass(frozen=True)
class Document:
    index: UUID
    id: UUID
    version: UUID | None
    source_name: str

    @property
    def url(self) -> str:
        return f"/v1/admin/indexes/{self.index}/documents/{self.id}/pdf"

    @property
    def blob(self) -> str:
        return rendition_document_path(self.index, self.id, 1)


def ready_index(admin: Engine) -> UUID:
    project = project_row()
    index = search_index_row(status="ready")
    physical = physical_index_row(index["id"], state="active", provisioned_at=sql_now())
    put(admin, (projects, project), (search_indexes, index), (physical_indexes, physical))
    with admin.begin() as connection:
        connection.execute(
            update(search_indexes)
            .where(search_indexes.c.id == index["id"])
            .values(active_physical_index_id=physical["id"])
        )
    return UUID(str(index["id"]))


def ingested_document(
    admin: Engine,
    index: UUID,
    *,
    content_type: ContentType = ContentType.PDF,
    document_pdf_status: str | None = "built",
    current: bool = True,
) -> Document:
    """A live document whose ingested version 1 is current (or not, with `current=False`)."""
    name = f"Board Pack {uuid4().hex[:6]}.{EXTENSIONS[content_type]}"
    document = document_row(index, source_name=name, normalized_name=name.casefold())
    version = document_version_row(
        document["id"],
        index,
        uuid4(),
        state="ingested",
        committed_at=sql_now(),
        ingested_at=sql_now(),
        content_type=content_type.value,
        source_name=name,
        page_count=1,
        converted_sha256=None if content_type is ContentType.PDF else bytes(range(32)),
        document_pdf_status=document_pdf_status,
    )
    project_id = version["uploaded_via_project_id"]
    put(admin, (projects, project_row(id=project_id)), (documents, document))
    put(admin, (document_versions, version))
    if current:
        with admin.begin() as connection:
            connection.execute(
                update(documents)
                .where(documents.c.id == document["id"])
                .values(current_version_id=version["id"])
            )
    return Document(index, document["id"], version["id"] if current else None, name)


class BlobRecorder(SansIOHTTPPolicy[Any, Any]):
    """Records the URL path of every request the app's Blob client sends."""

    def __init__(self) -> None:
        self.paths: list[str] = []

    @override
    def on_request(self, request: PipelineRequest[Any]) -> None:
        self.paths.append(urlsplit(request.http_request.url).path)

    def blobs(self) -> set[str]:
        # Azurite's URLs are path-style: /devstoreaccount1/<container>/<blob>.
        return {path.split("/", 2)[2] for path in self.paths}


async def clear_downloads(cosmos: DatabaseProxy, user: SharedPrincipal) -> None:
    usage = cosmos.get_container_client(USAGE.name)
    minute = epoch_minute(cosmos_now())
    for item in (
        download_rate_window_id(minute - 1),
        download_rate_window_id(minute),
        download_lease_id(1),
        download_lease_id(2),
    ):
        with contextlib.suppress(CosmosResourceNotFoundError):
            await usage.delete_item(item, str(user.oid))


async def lease(cosmos: DatabaseProxy, user: SharedPrincipal) -> dict[str, Any] | None:
    usage = cosmos.get_container_client(USAGE.name)
    try:
        return dict(await usage.read_item(download_lease_id(1), str(user.oid)))
    except CosmosResourceNotFoundError:
        return None


async def window(cosmos: DatabaseProxy, user: SharedPrincipal) -> int:
    usage = cosmos.get_container_client(USAGE.name)
    try:
        item = await usage.read_item(
            download_rate_window_id(epoch_minute(cosmos_now())), str(user.oid)
        )
    except CosmosResourceNotFoundError:
        return 0
    return int(item["count"])


@pytest.fixture
def admin(sql_database: SqlTestDatabase) -> Iterator[Engine]:
    engine = sql_database.engine()
    yield engine
    engine.dispose()


@pytest.fixture
def recorder() -> BlobRecorder:
    return BlobRecorder()


@pytest.fixture
def emulator() -> EmulatorContainers:
    return EmulatorContainers()


@pytest.fixture
def built(
    api_sql_engine: SqlEngine, signing_keys: SigningKeys, emulator: EmulatorContainers
) -> BuiltApp:
    ports = replace(ports_under_test(sql_engine=api_sql_engine), container_wrapper=emulator)
    return build_app(ports=ports, signing_keys=signing_keys)


@pytest.fixture
async def renditions(recorder: BlobRecorder) -> AsyncIterator[ContainerClient]:
    service = BlobServiceClient.from_connection_string(
        AZURITE_CONNECTION_STRING,
        # The Blob client builds its own pipeline and ignores `per_call_policies`; its request
        # hook sees every request it sends.
        raw_request_hook=recorder.on_request,
        max_single_get_size=MIB,
        max_chunk_get_size=MIB,
    )
    async with service:
        yield service.get_container_client("renditions")


@pytest.fixture
async def api(
    *,
    built: BuiltApp,
    api_sql_engine: SqlEngine,
    cosmos_database: DatabaseProxy,
    emulator: EmulatorContainers,
    renditions: ContainerClient,
    carol: SharedPrincipal,
) -> AsyncIterator[ApiUnderTest]:
    emulator.database = cosmos_database
    await clear_downloads(cosmos_database, carol)
    try:
        async with serving(built) as started:
            started.app.dependency_overrides[renditions_container] = lambda: renditions
            yield started
    finally:
        await api_sql_engine.dispose()


@pytest.mark.parametrize("content_type", list(ContentType))
async def test_t068_the_current_versions_whole_document_rendition_downloads(
    *,
    api: ApiUnderTest,
    admin: Engine,
    carol: SharedPrincipal,
    cosmos_database: DatabaseProxy,
    storage: StorageClients,
    recorder: BlobRecorder,
    content_type: ContentType,
) -> None:
    document = ingested_document(admin, ready_index(admin), content_type=content_type)
    await write_blob(storage.container("renditions"), document.blob, WHOLE_PDF)

    response = await api.client.get(document.url, headers=carol.authorization())

    stem = document.source_name.rsplit(".", 1)[0]
    assert response.status_code == 200
    assert response.content == WHOLE_PDF
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-length"] == str(len(WHOLE_PDF))
    assert response.headers["content-disposition"] == content_disposition(f"{stem}.pdf")
    assert response.headers["content-disposition"].startswith("attachment; ")
    assert "filename*=UTF-8''" in response.headers["content-disposition"]
    assert response.headers["content-security-policy"] == "sandbox"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert recorder.blobs() == {f"renditions/{document.blob}"}
    assert await window(cosmos_database, carol) == 1
    assert await lease(cosmos_database, carol) is None


async def test_t068_a_user_gets_403_admin_required(
    api: ApiUnderTest, admin: Engine, alice: SharedPrincipal, recorder: BlobRecorder
) -> None:
    document = ingested_document(admin, ready_index(admin))

    response = await api.client.get(document.url, headers=alice.authorization())

    assert (response.status_code, response.json()["code"]) == (403, "admin_required")
    assert recorder.paths == []


async def test_t068_refusals_read_no_blob_and_leave_no_lease(
    *,
    api: ApiUnderTest,
    admin: Engine,
    carol: SharedPrincipal,
    cosmos_database: DatabaseProxy,
    recorder: BlobRecorder,
) -> None:
    index, other_index = ready_index(admin), ready_index(admin)
    of_other_index = ingested_document(admin, other_index)
    deleted = ingested_document(admin, index)
    with admin.begin() as connection:
        connection.execute(
            update(documents)
            .where(documents.c.id == deleted.id)
            .values(
                is_deleted=True, current_version_id=None, deleted_at=sql_now(), deleted_by=uuid4()
            )
        )
    too_large = ingested_document(admin, index, document_pdf_status="too_large")
    no_version = ingested_document(admin, index, current=False)
    cases = [
        (f"/v1/admin/indexes/{uuid4()}/documents/{deleted.id}/pdf", 404, "index_not_found"),
        (f"/v1/admin/indexes/{index}/documents/{uuid4()}/pdf", 404, "document_not_found"),
        (f"/v1/admin/indexes/{index}/documents/{of_other_index.id}/pdf", 404, "document_not_found"),
        (deleted.url, 404, "document_not_found"),
        (too_large.url, 409, "document_pdf_unavailable"),
        (no_version.url, 409, "document_pdf_unavailable"),
    ]

    for url, status, code in cases:
        response = await api.client.get(url, headers=carol.authorization())
        assert (response.status_code, response.json()["code"]) == (status, code), url
    assert recorder.paths == []
    assert await lease(cosmos_database, carol) is None
    assert await window(cosmos_database, carol) == len(cases)


async def test_t068_a_missing_blob_is_500_document_pdf_missing(
    *,
    api: ApiUnderTest,
    admin: Engine,
    carol: SharedPrincipal,
    telemetry_capture: TelemetryCapture,
) -> None:
    document = ingested_document(admin, ready_index(admin))

    response = await api.client.get(document.url, headers=carol.authorization())

    assert (response.status_code, response.json()["code"]) == (500, "document_pdf_missing")
    records = [
        record
        for record in telemetry_capture.log_records()
        if record.log_record.body == "document_pdf_blob_missing"
        and (record.log_record.attributes or {}).get("docchat.document_id") == str(document.id)
    ]
    assert len(records) == 1
    assert (records[0].log_record.attributes or {})["docchat.version_id"] == str(document.version)


@dataclass
class HeldReader:
    """Accepts the response start and the first piece, then nothing until `release` is set."""

    release: asyncio.Event
    started: asyncio.Event
    pieces: int = 0

    async def send(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.started.set()
            return
        if self.pieces >= 1:
            await self.release.wait()
        self.pieces += 1

    @staticmethod
    async def receive() -> Message:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


async def test_t068_the_download_holds_a_lease_naming_no_turn(
    *,
    api: ApiUnderTest,
    admin: Engine,
    carol: SharedPrincipal,
    cosmos_database: DatabaseProxy,
    storage: StorageClients,
) -> None:
    document = ingested_document(admin, ready_index(admin))
    await write_blob(storage.container("renditions"), document.blob, WHOLE_PDF)
    reader = HeldReader(asyncio.Event(), asyncio.Event())
    path = document.url
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"host", b"docchat-api.test"),
            (b"authorization", f"Bearer {carol.token()}".encode()),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("docchat-api.test", 80),
    }
    download = asyncio.create_task(api.app(scope, reader.receive, reader.send))
    await reader.started.wait()

    held = await lease(cosmos_database, carol)

    reader.release.set()
    await download
    assert held is not None
    assert (held["sessionId"], held["turnId"], held["ttl"]) == (None, None, 300)
    assert await lease(cosmos_database, carol) is None
