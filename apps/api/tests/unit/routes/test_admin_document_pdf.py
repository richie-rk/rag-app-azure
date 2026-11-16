"""Unit tests of `adminGetDocumentPdf` in process (T-068's in-process half; `11-api.md` sections
1.3, 1.10, 1.11 and 3.11.10; `12-chat-pipeline.md` section 16.5).

The app runs over the unit suite's fake data-access layer, taught here the document lookup, over
`FakeContainer`s for the Cosmos DB containers (the administrator's download window and leases
live in their own `usage` partition) and over a real `ContainerClient` of `renditions` whose
`BlobTransport` records every blob read.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any, Final, Protocol, cast
from uuid import UUID, uuid4

import httpx
import pytest
from azure.storage.blob.aio import ContainerClient

from docchat_api.app import Ports
from docchat_api.chat.citations import download_clock, renditions_container
from docchat_api.http.headers import content_disposition
from docchat_api.routes import admin_document_pdf
from docchat_api.routes.admin_document_pdf import DocumentNotFound, DocumentPdfMissing
from docchat_core.cosmos.containers import CONVERSATIONS, MEMORIES, USAGE
from docchat_core.cosmos.usage import download_lease_id, download_rate_window_id, epoch_minute
from docchat_core.sql.engine import SqlEngine
from docchat_testing.clock import FakeClock
from docchat_testing.fakes.blobs import BlobTransport
from docchat_testing.fakes.cosmos import FakeContainer
from docchat_testing.fixtures.api import ApiUnderTest, api_settings, ports_under_test, running_api
from docchat_testing.fixtures.identities import SharedPrincipal
from docchat_testing.tokens import SigningKeys

ACCOUNT: Final = "https://stdocchatdatatest.blob.core.windows.net"
MIB: Final = 1_048_576
INDEX: Final = UUID("1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d")
DOCUMENT: Final = UUID("2b3c4d5e-6f7a-4b8c-9d0e-1f2a3b4c5d6e")
VERSION: Final = UUID("3c4d5e6f-7a8b-4c9d-8e1f-2a3b4c5d6e7f")
VERSION_NUMBER: Final = 4
WHOLE_PDF: Final = b"%PDF-1.7 board pack " * 70_000
DOCUMENT_URL: Final = f"/renditions/{INDEX}/{DOCUMENT}/{VERSION_NUMBER}/document.pdf"
URL: Final = f"/v1/admin/indexes/{INDEX}/documents/{DOCUMENT}/pdf"


class Captured(Protocol):
    def records(self, message: str) -> list[Any]: ...


def lookup_row(**changes: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "status": "ready",
        "document_id": DOCUMENT,
        "source_name": "Board Pack Q3.pptx",
        "version_id": VERSION,
        "version_number": VERSION_NUMBER,
        "document_pdf_status": "built",
    }
    return SimpleNamespace(**(values | changes))


@dataclass
class LookupSql:
    row: SimpleNamespace | None = field(default_factory=lookup_row)
    calls: list[Any] = field(default_factory=list)


@pytest.fixture
def lookup_sql(fake_sql: Any, monkeypatch: pytest.MonkeyPatch) -> LookupSql:
    script = LookupSql()
    fetch_one = fake_sql.fetch_one

    async def answer(
        statement: object, parameters: Any = None, *, name: str, retry: str = "transient"
    ) -> Any:
        if name == "document_pdf":
            script.calls.append(parameters)
            return script.row
        return await fetch_one(statement, parameters, name=name, retry=retry)

    monkeypatch.setattr(fake_sql, "fetch_one", answer)
    return script


@pytest.fixture
def containers(clock: FakeClock) -> dict[str, FakeContainer]:
    return {
        definition.name: FakeContainer.of(definition, clock=clock)
        for definition in (CONVERSATIONS, MEMORIES, USAGE)
    }


@pytest.fixture
def ports(fake_sql: Any, clock: FakeClock, containers: dict[str, FakeContainer]) -> Ports:
    return replace(
        ports_under_test(sql_engine=cast("SqlEngine", fake_sql), clock=clock),
        container_wrapper=lambda container: cast("Any", containers[container.id]),
    )


@pytest.fixture
def transport() -> BlobTransport:
    return BlobTransport(blobs={DOCUMENT_URL: WHOLE_PDF})


@pytest.fixture
def renditions(transport: BlobTransport) -> ContainerClient:
    return ContainerClient(
        ACCOUNT,
        "renditions",
        transport=transport,
        retry_total=0,
        max_single_get_size=MIB,
        max_chunk_get_size=MIB,
    )


@pytest.fixture
async def api(
    *,
    ports: Ports,
    signing_keys: SigningKeys,
    renditions: ContainerClient,
    clock: FakeClock,
    captured: Captured,
    lookup_sql: LookupSql,
) -> AsyncIterator[ApiUnderTest]:
    async with (
        renditions,
        running_api(settings=api_settings(), ports=ports, signing_keys=signing_keys) as started,
    ):
        started.app.dependency_overrides[renditions_container] = lambda: renditions
        started.app.dependency_overrides[download_clock] = lambda: clock
        yield started


def window_count(containers: dict[str, FakeContainer], user: UUID, clock: FakeClock) -> int:
    item = containers["usage"].get(str(user), download_rate_window_id(epoch_minute(clock.now())))
    return 0 if item is None else int(item["count"])


def problem(response: httpx.Response) -> tuple[int, str]:
    return response.status_code, response.json()["code"]


async def test_t068_the_whole_document_rendition_streams_with_the_file_headers(
    *,
    api: ApiUnderTest,
    carol: SharedPrincipal,
    containers: dict[str, FakeContainer],
    transport: BlobTransport,
    clock: FakeClock,
    lookup_sql: LookupSql,
) -> None:
    written: list[dict[str, Any]] = []

    def keep() -> None:
        lease = containers["usage"].get(str(carol.oid), download_lease_id(1))
        if lease is not None:
            written.append(lease)

    containers["usage"].before("delete_item", keep)

    response = await api.client.get(URL, headers=carol.authorization())

    assert response.status_code == 200
    assert response.content == WHOLE_PDF
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-length"] == str(len(WHOLE_PDF))
    assert response.headers["content-disposition"] == content_disposition("Board Pack Q3.pdf")
    assert "filename*=UTF-8''Board%20Pack%20Q3.pdf" in response.headers["content-disposition"]
    assert response.headers["content-security-policy"] == "sandbox"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert set(transport.paths()) == {DOCUMENT_URL}
    assert lookup_sql.calls == [{"index_id": INDEX, "document_id": DOCUMENT}]
    assert window_count(containers, carol.oid, clock) == 1
    (lease,) = written
    assert (lease["sessionId"], lease["turnId"]) == (None, None)
    assert containers["usage"].get(str(carol.oid), download_lease_id(1)) is None


async def test_t068_a_user_gets_403_admin_required_before_the_window(
    api: ApiUnderTest, alice: SharedPrincipal, containers: dict[str, FakeContainer]
) -> None:
    response = await api.client.get(URL, headers=alice.authorization())

    assert problem(response) == (403, "admin_required")
    assert containers["usage"].calls == []


@pytest.mark.parametrize(
    ("row", "answer"),
    [
        (None, (404, "index_not_found")),
        (lookup_row(status="deleted"), (404, "index_not_found")),
        (
            lookup_row(document_id=None, source_name=None, version_id=None),
            (404, "document_not_found"),
        ),
        (
            lookup_row(version_id=None, version_number=None, document_pdf_status=None),
            (409, "document_pdf_unavailable"),
        ),
        (lookup_row(document_pdf_status="too_large"), (409, "document_pdf_unavailable")),
        (lookup_row(document_pdf_status=None), (409, "document_pdf_unavailable")),
    ],
    ids=["no_index", "deleted_index", "no_document", "no_version", "too_large", "unrecorded"],
)
async def test_t068_each_refusal_reads_no_blob_and_leaves_no_lease(
    *,
    api: ApiUnderTest,
    carol: SharedPrincipal,
    containers: dict[str, FakeContainer],
    transport: BlobTransport,
    clock: FakeClock,
    lookup_sql: LookupSql,
    row: SimpleNamespace | None,
    answer: tuple[int, str],
) -> None:
    lookup_sql.row = row

    response = await api.client.get(URL, headers=carol.authorization())

    assert problem(response) == answer
    assert transport.requests == []
    assert containers["usage"].get(str(carol.oid), download_lease_id(1)) is None
    assert window_count(containers, carol.oid, clock) == 1


async def test_t068_a_missing_blob_is_500_document_pdf_missing(
    *,
    api: ApiUnderTest,
    carol: SharedPrincipal,
    containers: dict[str, FakeContainer],
    transport: BlobTransport,
    captured: Captured,
) -> None:
    del transport.blobs[DOCUMENT_URL]

    response = await api.client.get(URL, headers=carol.authorization())

    assert problem(response) == (500, "document_pdf_missing")
    (record,) = captured.records("document_pdf_blob_missing")
    assert record.__dict__["docchat.index_id"] == str(INDEX)
    assert record.__dict__["docchat.document_id"] == str(DOCUMENT)
    assert record.__dict__["docchat.version_id"] == str(VERSION)
    assert containers["usage"].get(str(carol.oid), download_lease_id(1)) is None


async def test_t068_the_request_counts_in_the_window_the_citation_files_share(
    *,
    api: ApiUnderTest,
    carol: SharedPrincipal,
    containers: dict[str, FakeContainer],
    clock: FakeClock,
    lookup_sql: LookupSql,
) -> None:
    containers["usage"].put(
        {
            "id": download_rate_window_id(epoch_minute(clock.now())),
            "userId": str(carol.oid),
            "type": "download_rate_window",
            "count": 30,
            "ttl": 180,
        }
    )

    response = await api.client.get(URL, headers=carol.authorization())

    assert problem(response) == (429, "rate_limited")
    assert response.headers["retry-after"] == str(response.json()["retryAfterSeconds"])
    assert lookup_sql.calls == []


async def test_path_validation_answers_before_the_window(
    api: ApiUnderTest, carol: SharedPrincipal, containers: dict[str, FakeContainer]
) -> None:
    response = await api.client.get(
        f"/v1/admin/indexes/{INDEX}/documents/{uuid4().hex}/pdf", headers=carol.authorization()
    )

    assert problem(response) == (422, "validation_failed")
    assert response.json()["errors"] == [{"path": "path.documentId", "reason": "invalid_uuid"}]
    assert containers["usage"].calls == []


def test_the_route_is_an_administrator_route_with_its_codes() -> None:
    (route,) = admin_document_pdf.router.routes
    assert getattr(route, "operation_id", None) == "adminGetDocumentPdf"
    assert getattr(route, "path", None) == "/admin/indexes/{indexId}/documents/{documentId}/pdf"
    assert DocumentNotFound().status == 404
    assert DocumentPdfMissing().status == 500
