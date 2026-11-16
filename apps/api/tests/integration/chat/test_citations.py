"""The citation operations against SQL Server, the Cosmos DB emulator and Azurite (T-276, T-278,
T-279, T-283 and the citation entries of the Cosmos DB data-access audit T-301;
`12-chat-pipeline.md` section 16; `11-api.md` section 3.5; `84-testing.md` section 2.11).

The in-process API runs as the `docchat_api` role of the session's database; its Cosmos DB
containers are the session's emulator database (`EmulatorContainers`, each wrapped in
`CosmosAudit`), and its `renditions` container client is Azurite's, built with a recording policy
(the blob recorder), so a test proves which blobs the API read and that it read none under
`documents/`. Each test lays out its own logical index, project, document, version and chunk set,
writes `alice`'s turn into her partition and the renditions into Azurite, and clears her download
windows and leases first. The slow-reader and duration tests drive the app through its ASGI
interface with a `send` that stops accepting pieces, as a reader that stopped reading does.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, replace
from typing import Any, Final, override
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
import pytest
from azure.core.pipeline import PipelineRequest
from azure.core.pipeline.policies import SansIOHTTPPolicy
from azure.cosmos.aio import DatabaseProxy
from azure.cosmos.exceptions import CosmosResourceNotFoundError
from azure.storage.blob.aio import BlobServiceClient, ContainerClient
from sqlalchemy import Engine, delete, insert, update
from starlette.types import Message

from docchat_api.chat.citations import download_clock, renditions_container
from docchat_api.http.headers import content_disposition
from docchat_core.clients.emulators import AZURITE_CONNECTION_STRING
from docchat_core.cosmos.containers import CONVERSATIONS, USAGE
from docchat_core.cosmos.conversations import CitationContentType, TurnStatus, turn_id
from docchat_core.cosmos.usage import download_lease_id, download_rate_window_id, epoch_minute
from docchat_core.ingestion.models import ContentType
from docchat_core.ingestion.names import rendition_document_path, rendition_page_path
from docchat_core.sql.engine import SqlEngine
from docchat_core.sql.tables.access import project_indexes, project_members, projects, users
from docchat_core.sql.tables.ingestion import (
    chunk_sets,
    document_versions,
    documents,
    physical_indexes,
    search_indexes,
)
from docchat_testing.builders import project_index_row, project_member_row, project_row, user_row
from docchat_testing.builders.cosmos import cosmos_now, stored_citation, turn_document
from docchat_testing.builders.ingestion import (
    chunk_set_row,
    document_row,
    document_version_row,
    physical_index_row,
    search_index_row,
)
from docchat_testing.builders.rows import sql_now
from docchat_testing.clock import FakeClock
from docchat_testing.data_audit import CosmosAudit
from docchat_testing.fixtures.api import (
    ApiUnderTest,
    BuiltApp,
    build_app,
    operation_map,
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
PAGE: Final = 2
PAGE_PDF: Final = b"%PDF-1.7\n% page rendition\n" + b"0" * 300_000 + b"\n%%EOF\n"
WHOLE_PDF: Final = b"%PDF-1.7\n% whole document\n" + b"1" * (2 * MIB) + b"\n%%EOF\n"
CITATION_OPERATIONS: Final = frozenset({"getCitation", "getCitationContent", "getCitationDocument"})
SUFFIXES: Final = ("", "/content", "/document")
OFFICE_SOURCE: Final = bytes(range(32))


def put(engine: Engine, *rows: tuple[Any, dict[str, Any]]) -> None:
    with engine.begin() as connection:
        for table, row in rows:
            connection.execute(insert(table), row)


# --- The world ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Index:
    """A logical index with an active physical index, the default of project A."""

    id: UUID
    physical: UUID
    project: UUID


@dataclass(frozen=True)
class Cited:
    """A document's current version and the chunk set a citation names."""

    index: Index
    document: UUID
    version: UUID
    chunk_set: UUID
    source_name: str
    content_type: ContentType


def project_with_index(admin: Engine, alice: SharedPrincipal) -> Index:
    project = project_row()
    index = search_index_row(status="ready")
    physical = physical_index_row(index["id"], state="active", provisioned_at=sql_now())
    put(
        admin,
        (projects, project),
        (project_members, project_member_row(alice.oid, project["id"])),
        (search_indexes, index),
        (physical_indexes, physical),
        (project_indexes, project_index_row(project["id"], index["id"], is_default=True)),
    )
    with admin.begin() as connection:
        connection.execute(
            update(search_indexes)
            .where(search_indexes.c.id == index["id"])
            .values(active_physical_index_id=physical["id"])
        )
    return Index(index["id"], physical["id"], project["id"])


def cited_version(
    admin: Engine,
    index: Index,
    *,
    content_type: ContentType = ContentType.PDF,
    document_pdf_status: str | None = "built",
    chunk_set_hash: str = "same",
) -> Cited:
    """A live document whose ingested version 1 is current, and a served chunk set of it whose
    `pdf_sha256` is the PDF the renditions come from (`same`), another one (`other`) or `NULL`
    (`null`); a DOCX or PPTX version has a converted PDF, `NULL` when `chunk_set_hash` is
    `null_converted`."""
    extension = {ContentType.PDF: "pdf", ContentType.DOCX: "docx", ContentType.PPTX: "pptx"}
    name = f"Field Guide {uuid4().hex[:6]}.{extension[content_type]}"
    document = document_row(index.id, source_name=name, normalized_name=name.casefold())
    converted = None if content_type is ContentType.PDF else OFFICE_SOURCE
    if chunk_set_hash == "null_converted":
        converted = None
    version = document_version_row(
        document["id"],
        index.id,
        index.project,
        state="ingested",
        committed_at=sql_now(),
        ingested_at=sql_now(),
        content_type=content_type.value,
        source_name=name,
        page_count=PAGE,
        converted_sha256=converted,
        document_pdf_status=document_pdf_status,
    )
    rendered_from = version["sha256"] if content_type is ContentType.PDF else OFFICE_SOURCE
    pdf_sha256 = {"same": rendered_from, "other": bytes(32), "null": None}.get(
        chunk_set_hash, rendered_from
    )
    chunk_set = chunk_set_row(
        document["id"],
        version["id"],
        index.physical,
        state="served",
        pdf_sha256=pdf_sha256,
        chunk_count=1,
    )
    put(admin, (documents, document), (document_versions, version), (chunk_sets, chunk_set))
    with admin.begin() as connection:
        connection.execute(
            update(documents)
            .where(documents.c.id == document["id"])
            .values(current_version_id=version["id"])
        )
    return Cited(index, document["id"], version["id"], chunk_set["id"], name, content_type)


@dataclass(frozen=True)
class Turn:
    session: UUID
    turn: UUID

    def url(self, number: int = 1, suffix: str = "") -> str:
        return f"/v1/sessions/{self.session}/turns/{self.turn}/citations/{number}{suffix}"


async def write_turn(
    cosmos: DatabaseProxy,
    owner: SharedPrincipal,
    project: UUID,
    *cited: Cited,
    status: TurnStatus = TurnStatus.COMPLETED,
) -> Turn:
    """`owner`'s turn in `project`, citing page 2 of each `cited` as citations 1, 2, …"""
    session, client_turn = uuid4(), uuid4()
    citations = [
        stored_citation(
            number=number,
            document_id=item.document,
            document_version_id=item.version,
            chunk_set_id=item.chunk_set,
            search_index_id=item.index.id,
            page_number=PAGE,
            content_type=CitationContentType(item.content_type.value),
        )
        for number, item in enumerate(cited, start=1)
    ]
    document = turn_document(
        user_id=owner.oid,
        session_id=session,
        client_turn_id=client_turn,
        project_id=project,
        status=status,
        citations=citations,
    )
    await cosmos.get_container_client(CONVERSATIONS.name).upsert_item(document.to_item())
    return Turn(session, turn_id(session, client_turn))


async def write_renditions(storage: StorageClients, cited: Cited) -> None:
    container = storage.container("renditions")
    await container.upload_blob(
        rendition_page_path(cited.index.id, cited.document, 1, PAGE), PAGE_PDF, overwrite=True
    )
    await write_blob(
        container, rendition_document_path(cited.index.id, cited.document, 1), WHOLE_PDF
    )


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


async def live_leases(cosmos: DatabaseProxy, user: SharedPrincipal) -> list[str]:
    usage = cosmos.get_container_client(USAGE.name)
    found: list[str] = []
    for slot in (1, 2):
        with contextlib.suppress(CosmosResourceNotFoundError):
            await usage.read_item(download_lease_id(slot), str(user.oid))
            found.append(download_lease_id(slot))
    return found


# --- The app -----------------------------------------------------------------------------------


class BlobRecorder(SansIOHTTPPolicy[Any, Any]):
    """Records the URL path of every request the app's Blob client sends."""

    def __init__(self) -> None:
        self.paths: list[str] = []

    @override
    def on_request(self, request: PipelineRequest[Any]) -> None:
        self.paths.append(urlsplit(request.http_request.url).path)

    def containers(self) -> set[str]:
        # Azurite's URLs are path-style: /devstoreaccount1/<container>/<blob>.
        return {path.split("/")[2] for path in self.paths}

    def blobs(self) -> set[str]:
        return {path.split("/", 3)[3] for path in self.paths}


@pytest.fixture
def admin(sql_database: SqlTestDatabase, alice: SharedPrincipal) -> Iterator[Engine]:
    engine = sql_database.engine()
    with engine.begin() as connection:
        connection.execute(insert(users), user_row(object_id=alice.oid))
    yield engine
    engine.dispose()


@pytest.fixture
def audit() -> CosmosAudit:
    return CosmosAudit(operations=operation_map())


@pytest.fixture
def emulator(audit: CosmosAudit) -> EmulatorContainers:
    return EmulatorContainers(audit=audit)


@pytest.fixture
def recorder() -> BlobRecorder:
    return BlobRecorder()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(cosmos_now())


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
    alice: SharedPrincipal,
) -> AsyncIterator[ApiUnderTest]:
    emulator.database = cosmos_database
    await clear_downloads(cosmos_database, alice)
    try:
        async with serving(built) as started:
            started.operations = operation_map()
            started.app.dependency_overrides[renditions_container] = lambda: renditions
            yield started
    finally:
        await api_sql_engine.dispose()


async def open_all(api: ApiUnderTest, alice: SharedPrincipal, url: str) -> list[httpx.Response]:
    return [
        await api.client.get(url + suffix, headers=alice.authorization()) for suffix in SUFFIXES
    ]


def codes(responses: list[httpx.Response]) -> list[tuple[int, str]]:
    return [(response.status_code, response.json()["code"]) for response in responses]


# --- T-276 -------------------------------------------------------------------------------------


@pytest.mark.parametrize("content_type", list(ContentType))
async def test_t276_a_citation_of_each_format_opens_its_page_and_its_document(
    *,
    api: ApiUnderTest,
    admin: Engine,
    alice: SharedPrincipal,
    cosmos_database: DatabaseProxy,
    storage: StorageClients,
    recorder: BlobRecorder,
    content_type: ContentType,
) -> None:
    index = project_with_index(admin, alice)
    cited = cited_version(admin, index, content_type=content_type)
    await write_renditions(storage, cited)
    turn = await write_turn(cosmos_database, alice, index.project, cited)

    detail, page, whole = await open_all(api, alice, turn.url())

    stem = cited.source_name.rsplit(".", 1)[0]
    assert detail.status_code == 200
    body = detail.json()
    assert (body["openAs"], body["contentType"]) == ("pdf_page", content_type.value)
    assert body["fileName"] == f"{stem}-page-{PAGE}.pdf"
    assert body["documentFileName"] == f"{stem}.pdf"
    assert body["documentPdfAvailable"] is True
    for response, data, name in (
        (page, PAGE_PDF, f"{stem}-page-{PAGE}.pdf"),
        (whole, WHOLE_PDF, f"{stem}.pdf"),
    ):
        assert response.status_code == 200
        assert response.content == data
        assert response.headers["content-type"] == "application/pdf"
        assert response.headers["content-length"] == str(len(data))
        assert response.headers["content-disposition"] == content_disposition(name)
        assert "filename*=UTF-8''" in response.headers["content-disposition"]
        assert response.headers["content-security-policy"] == "sandbox"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
    assert recorder.containers() == {"renditions"}
    assert recorder.blobs() == {
        rendition_page_path(index.id, cited.document, 1, PAGE),
        rendition_document_path(index.id, cited.document, 1),
    }
    assert await live_leases(cosmos_database, alice) == []


async def test_t276_a_superseded_version_and_a_deleted_document_are_gone(
    *,
    api: ApiUnderTest,
    admin: Engine,
    alice: SharedPrincipal,
    cosmos_database: DatabaseProxy,
    recorder: BlobRecorder,
) -> None:
    index = project_with_index(admin, alice)
    superseded, deleted = cited_version(admin, index), cited_version(admin, index)
    newer = document_version_row(
        superseded.document,
        index.id,
        index.project,
        version_number=2,
        state="ingested",
        committed_at=sql_now(),
        ingested_at=sql_now(),
    )
    # A newer version's Flip supersedes the ingested one: at most one version of a document is
    # `ingested` (`80-data-model.md` section 8.1, `UX_document_versions_one_ingested`).
    with admin.begin() as connection:
        connection.execute(
            update(document_versions)
            .where(document_versions.c.id == superseded.version)
            .values(state="superseded", superseded_at=sql_now())
        )
    put(admin, (document_versions, newer))
    with admin.begin() as connection:
        connection.execute(
            update(documents)
            .where(documents.c.id == superseded.document)
            .values(current_version_id=newer["id"])
        )
        connection.execute(
            update(documents)
            .where(documents.c.id == deleted.document)
            .values(
                is_deleted=True,
                current_version_id=None,
                deleted_at=sql_now(),
                deleted_by=uuid4(),
            )
        )
    turn = await write_turn(cosmos_database, alice, index.project, superseded, deleted)

    for number in (1, 2):
        responses = await open_all(api, alice, turn.url(number))
        assert codes(responses) == [(410, "document_changed")] * 3
    assert recorder.paths == []
    assert await live_leases(cosmos_database, alice) == []


async def test_t276_each_hidden_citation_is_404_on_all_three(
    *,
    api: ApiUnderTest,
    admin: Engine,
    alice: SharedPrincipal,
    bob: SharedPrincipal,
    cosmos_database: DatabaseProxy,
    recorder: BlobRecorder,
) -> None:
    index = project_with_index(admin, alice)
    cited = cited_version(admin, index)
    mine = await write_turn(cosmos_database, alice, index.project, cited)
    streaming = await write_turn(
        cosmos_database, alice, index.project, cited, status=TurnStatus.STREAMING
    )
    bobs = await write_turn(cosmos_database, bob, index.project, cited)
    other_index = project_with_index(admin, alice)
    unmapped = cited_version(admin, other_index)
    with admin.begin() as connection:
        connection.execute(
            delete(project_indexes).where(project_indexes.c.search_index_id == other_index.id)
        )
    of_unmapped = await write_turn(cosmos_database, alice, index.project, unmapped)

    hidden = [
        await open_all(api, alice, bobs.url()),
        await open_all(api, alice, streaming.url()),
        await open_all(api, alice, mine.url(16)),
        await open_all(api, alice, of_unmapped.url()),
    ]
    with admin.begin() as connection:
        connection.execute(
            delete(project_members).where(
                project_members.c.user_object_id == alice.oid,
                project_members.c.project_id == index.project,
            )
        )
    hidden.append(await open_all(api, alice, mine.url()))

    for responses in hidden:
        assert codes(responses) == [(404, "citation_not_found")] * 3
    bodies = {
        str({key: value for key, value in response.json().items() if key != "correlationId"})
        for responses in hidden
        for response in responses
    }
    assert len(bodies) == 1
    assert recorder.paths == []
    assert await live_leases(cosmos_database, alice) == []


async def test_t276_a_version_too_large_for_one_file_still_opens_its_pages(
    *,
    api: ApiUnderTest,
    admin: Engine,
    alice: SharedPrincipal,
    cosmos_database: DatabaseProxy,
    storage: StorageClients,
    recorder: BlobRecorder,
) -> None:
    index = project_with_index(admin, alice)
    cited = cited_version(admin, index, document_pdf_status="too_large")
    await write_renditions(storage, cited)
    turn = await write_turn(cosmos_database, alice, index.project, cited)

    detail, page, whole = await open_all(api, alice, turn.url())

    assert detail.status_code == 200
    assert detail.json()["documentPdfAvailable"] is False
    assert page.status_code == 200
    assert (whole.status_code, whole.json()["code"]) == (409, "document_pdf_unavailable")
    assert recorder.blobs() == {rendition_page_path(index.id, cited.document, 1, PAGE)}
    assert await live_leases(cosmos_database, alice) == []


async def test_t276_a_missing_page_blob_is_500_citation_content_missing(
    *,
    api: ApiUnderTest,
    admin: Engine,
    alice: SharedPrincipal,
    cosmos_database: DatabaseProxy,
) -> None:
    index = project_with_index(admin, alice)
    cited = cited_version(admin, index)
    turn = await write_turn(cosmos_database, alice, index.project, cited)

    response = await api.client.get(turn.url(1, "/content"), headers=alice.authorization())

    assert (response.status_code, response.json()["code"]) == (500, "citation_content_missing")
    assert await live_leases(cosmos_database, alice) == []


# --- T-283 -------------------------------------------------------------------------------------


def outcomes(capture: TelemetryCapture) -> list[str]:
    return [
        str((span.attributes or {}).get("docchat.outcome"))
        for span in capture.spans()
        if span.name == "docchat.citation.open"
    ]


async def test_t283_a_chunk_set_of_another_pdf_answers_gone_until_the_flip(
    *,
    api: ApiUnderTest,
    admin: Engine,
    alice: SharedPrincipal,
    cosmos_database: DatabaseProxy,
    storage: StorageClients,
    recorder: BlobRecorder,
    telemetry_capture: TelemetryCapture,
) -> None:
    def arrange() -> tuple[Any, list[Any], Any]:
        # The synchronous SQL arrange runs off the loop, which the 100 ms guard watches.
        index = project_with_index(admin, alice)
        stale = [
            cited_version(admin, index, content_type=ContentType.DOCX, chunk_set_hash=hashed)
            for hashed in ("other", "null", "null_converted")
        ]
        return index, stale, cited_version(admin, index, document_pdf_status="too_large")

    index, stale, too_large = await asyncio.to_thread(arrange)
    turn = await write_turn(cosmos_database, alice, index.project, *stale, too_large)
    before = outcomes(telemetry_capture)

    for number in (1, 2, 3):
        responses = await open_all(api, alice, turn.url(number))
        assert codes(responses) == [(410, "document_changed")] * 3
    whole = await api.client.get(turn.url(4, "/document"), headers=alice.authorization())
    assert whole.status_code == 409
    assert recorder.paths == []
    added = outcomes(telemetry_capture)[len(before) :]
    assert added.count("gone") == 9
    assert added.count("unavailable") == 1

    # The Flip serves a chunk set that parsed the converted PDF now in use.
    flipped = chunk_set_row(
        stale[0].document,
        stale[0].version,
        index.physical,
        state="served",
        pdf_sha256=OFFICE_SOURCE,
        chunk_count=1,
    )
    with admin.begin() as connection:
        connection.execute(
            update(chunk_sets)
            .where(chunk_sets.c.id == stale[0].chunk_set)
            .values(state="retired", retired_at=sql_now())
        )
    put(admin, (chunk_sets, flipped))
    await write_renditions(storage, stale[0])
    renewed = await write_turn(
        cosmos_database, alice, index.project, replace(stale[0], chunk_set=flipped["id"])
    )

    response = await api.client.get(renewed.url(1, "/content"), headers=alice.authorization())

    assert response.status_code == 200
    assert response.content == PAGE_PDF


# --- T-278 and T-279: the download limits ------------------------------------------------------


@dataclass
class HeldReader:
    """A reader that accepts the response start and the first piece, then nothing until
    `release` is set."""

    release: asyncio.Event
    started: asyncio.Event
    status: int | None = None
    pieces: int = 0

    async def send(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.status = int(message["status"])
            self.started.set()
            return
        if self.pieces >= 1:
            await self.release.wait()
        self.pieces += 1

    @staticmethod
    async def receive() -> Message:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def scope(principal: SharedPrincipal, path: str) -> dict[str, Any]:
    return {
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
            (b"authorization", f"Bearer {principal.token()}".encode()),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("docchat-api.test", 80),
    }


async def test_t278_two_held_downloads_leave_a_third_429_and_a_completed_one_frees_a_slot(
    *,
    api: ApiUnderTest,
    admin: Engine,
    alice: SharedPrincipal,
    cosmos_database: DatabaseProxy,
    storage: StorageClients,
) -> None:
    index = project_with_index(admin, alice)
    cited = cited_version(admin, index)
    await write_renditions(storage, cited)
    turn = await write_turn(cosmos_database, alice, index.project, cited)
    readers = [HeldReader(asyncio.Event(), asyncio.Event()) for _ in range(2)]
    held = [
        asyncio.create_task(api.app(scope(alice, turn.url(1, suffix)), reader.receive, reader.send))
        for reader, suffix in zip(readers, ("/content", "/document"), strict=True)
    ]
    for reader in readers:
        await reader.started.wait()

    third = await api.client.get(turn.url(1, "/content"), headers=alice.authorization())
    assert (third.status_code, third.json()["code"]) == (429, "too_many_downloads")
    assert third.json()["retryAfterSeconds"] == 10
    assert [reader.status for reader in readers] == [200, 200]

    readers[0].release.set()
    await held[0]
    fourth = await api.client.get(turn.url(1, "/content"), headers=alice.authorization())
    assert fourth.status_code == 200

    readers[1].release.set()
    await held[1]
    refused = await api.client.get(turn.url(9, "/content"), headers=alice.authorization())
    assert refused.status_code == 404
    assert await live_leases(cosmos_database, alice) == []


async def test_t279_the_31st_download_of_a_minute_is_429_rate_limited(
    *,
    api: ApiUnderTest,
    admin: Engine,
    alice: SharedPrincipal,
    cosmos_database: DatabaseProxy,
) -> None:
    index = project_with_index(admin, alice)
    cited = cited_version(admin, index)
    turn = await write_turn(cosmos_database, alice, index.project, cited)
    usage = cosmos_database.get_container_client(USAGE.name)
    minute = epoch_minute(cosmos_now())
    await usage.upsert_item(
        {
            "id": download_rate_window_id(minute),
            "userId": str(alice.oid),
            "type": "download_rate_window",
            "count": 30,
            "ttl": 180,
        }
    )

    response = await api.client.get(turn.url(1, "/document"), headers=alice.authorization())

    assert (response.status_code, response.json()["code"]) == (429, "rate_limited")
    assert response.headers["retry-after"] == str(response.json()["retryAfterSeconds"])


async def test_t279_a_reader_that_stops_is_aborted_after_30_seconds(
    *,
    api: ApiUnderTest,
    admin: Engine,
    alice: SharedPrincipal,
    cosmos_database: DatabaseProxy,
    storage: StorageClients,
    telemetry_capture: TelemetryCapture,
) -> None:
    index = project_with_index(admin, alice)
    cited = cited_version(admin, index)
    await write_renditions(storage, cited)
    turn = await write_turn(cosmos_database, alice, index.project, cited)
    reader = HeldReader(asyncio.Event(), asyncio.Event())
    loop = asyncio.get_running_loop()

    started = loop.time()
    await api.app(scope(alice, turn.url(1, "/document")), reader.receive, reader.send)
    elapsed = loop.time() - started

    assert 28 <= elapsed <= 32
    aborted = [
        record
        for record in telemetry_capture.log_records()
        if record.log_record.body == "citation_download_aborted"
        and (record.log_record.attributes or {}).get("docchat.turn_id") == str(turn.turn)
    ]
    (record,) = aborted
    assert (record.log_record.attributes or {})["docchat.citation.abort_reason"] == "slow_client"
    assert await live_leases(cosmos_database, alice) == []


async def test_t279_a_download_still_running_at_300_seconds_is_aborted(
    *,
    api: ApiUnderTest,
    admin: Engine,
    alice: SharedPrincipal,
    cosmos_database: DatabaseProxy,
    storage: StorageClients,
    clock: FakeClock,
    telemetry_capture: TelemetryCapture,
) -> None:
    index = project_with_index(admin, alice)
    cited = cited_version(admin, index)
    await write_renditions(storage, cited)
    turn = await write_turn(cosmos_database, alice, index.project, cited)
    api.app.dependency_overrides[download_clock] = lambda: clock
    sent: list[int] = []

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body":
            sent.append(len(message.get("body", b"")))
            if len(sent) == 2:
                clock.advance(300)

    await api.app(scope(alice, turn.url(1, "/document")), HeldReader.receive, send)

    assert len(sent) == 2
    reasons = [
        (record.log_record.attributes or {}).get("docchat.citation.abort_reason")
        for record in telemetry_capture.log_records()
        if record.log_record.body == "citation_download_aborted"
        and (record.log_record.attributes or {}).get("docchat.turn_id") == str(turn.turn)
    ]
    assert reasons == ["duration"]
    assert await live_leases(cosmos_database, alice) == []


# --- T-301: the Cosmos DB data-access audit ----------------------------------------------------


async def test_t301_every_citation_call_names_the_callers_full_partition_key(
    *,
    api: ApiUnderTest,
    admin: Engine,
    alice: SharedPrincipal,
    cosmos_database: DatabaseProxy,
    storage: StorageClients,
    audit: CosmosAudit,
    telemetry_capture: TelemetryCapture,
) -> None:
    index = project_with_index(admin, alice)
    cited = cited_version(admin, index)
    await write_renditions(storage, cited)
    turn = await write_turn(cosmos_database, alice, index.project, cited)

    responses = await open_all(api, alice, turn.url())

    assert [response.status_code for response in responses] == [200, 200, 200]
    audit.check()
    calls = [call for call in audit.calls if call.context.operation in CITATION_OPERATIONS]
    assert {call.context.operation for call in calls} == CITATION_OPERATIONS
    for call in calls:
        assert call.full_key
        assert call.partition_key is not None
        assert call.partition_key[0] == str(alice.oid)
        assert call.method != "query_items"
        if call.container == CONVERSATIONS.name:
            assert call.partition_key == (str(alice.oid), str(turn.session))
    assert audit.user_ids() == {str(alice.oid)}
