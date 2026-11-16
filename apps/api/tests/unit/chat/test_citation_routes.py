"""Unit tests of `getCitation`, `getCitationContent` and `getCitationDocument` in process (T-212's
citation half, T-276's and T-283's in-process halves, T-277's routes, the download limits of
T-278 and T-279; `12-chat-pipeline.md` section 16; `11-api.md` sections 1.3 and 3.5).

The app runs over the unit suite's fake data-access layer, taught here the access query Q1 and
the citation's document statement, over `FakeContainer`s for the Cosmos DB containers (the turn
lives in the caller's `conversations` partition, the download window and leases in `usage`), and
over a real `ContainerClient` of `renditions` whose `BlobTransport` records every blob read.
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
from opentelemetry.sdk.trace import ReadableSpan

from docchat_api.app import Ports
from docchat_api.chat import citation_routes
from docchat_api.chat.citation_routes import (
    CITATION_PATH,
    CitationContentMissing,
    CitationNotFound,
    DocumentChanged,
    outcome_of,
)
from docchat_api.chat.citations import download_clock, renditions_container
from docchat_api.http.headers import content_disposition
from docchat_api.http.ratelimit import BurstLimiter
from docchat_api.http.routing import DocChatRoute
from docchat_api.http.state import current_process
from docchat_api.usage import RateLimited
from docchat_core.cosmos.containers import CONVERSATIONS, MEMORIES, USAGE
from docchat_core.cosmos.conversations import (
    CitationContentType,
    StoredCitation,
    TurnStatus,
    turn_id,
)
from docchat_core.cosmos.usage import download_lease_id, download_rate_window_id, epoch_minute
from docchat_core.ingestion.models import ContentType
from docchat_core.sql.engine import SqlEngine, SqlUnavailable
from docchat_testing.builders.cosmos import turn_document
from docchat_testing.clock import FakeClock
from docchat_testing.fakes.blobs import BlobTransport
from docchat_testing.fakes.cosmos import FakeContainer
from docchat_testing.fixtures.api import ApiUnderTest, api_settings, ports_under_test, running_api
from docchat_testing.fixtures.identities import SharedPrincipal
from docchat_testing.tokens import SigningKeys

ACCOUNT: Final = "https://stdocchatdatatest.blob.core.windows.net"
MIB: Final = 1_048_576
INDEX: Final = UUID("4d8f0a71-2b3c-4d5e-8f60-718293a4b5c6")
PHYSICAL: Final = UUID("5e9a1b82-3c4d-4e6f-9a71-8293a4b5c6d7")
PROJECT: Final = UUID("6fab2c93-4d5e-4f70-8b82-93a4b5c6d7e8")
DOCUMENT: Final = UUID("7abc3da4-5e6f-4081-9c93-a4b5c6d7e8f9")
VERSION: Final = UUID("8bcd4eb5-6f70-4192-8da4-b5c6d7e8f90a")
CHUNK_SET: Final = UUID("9cde5fc6-7081-42a3-9eb5-c6d7e8f90a1b")
SESSION: Final = UUID("adef60d7-8192-43b4-afc6-d7e8f90a1b2c")
CLIENT_TURN: Final = UUID("bef071e8-92a3-44c5-80d7-e8f90a1b2c3d")
TURN: Final = turn_id(SESSION, CLIENT_TURN)
SHA: Final = bytes(range(32))
CONVERTED: Final = bytes(range(32, 64))
VERSION_NUMBER: Final = 3
PAGE: Final = 2
PAGE_PDF: Final = b"%PDF-1.7 page " * 9_000
WHOLE_PDF: Final = b"%PDF-1.7 whole " * 160_000
PAGE_URL: Final = f"/renditions/{INDEX}/{DOCUMENT}/{VERSION_NUMBER}/pages/{PAGE}.pdf"
DOCUMENT_URL: Final = f"/renditions/{INDEX}/{DOCUMENT}/{VERSION_NUMBER}/document.pdf"
BASE: Final = f"/v1/sessions/{SESSION}/turns/{TURN}/citations"
OPERATIONS: Final = ("getCitation", "getCitationContent", "getCitationDocument")
SUFFIXES: Final = {
    "getCitation": "",
    "getCitationContent": "/content",
    "getCitationDocument": "/document",
}


class Captured(Protocol):
    def finished_spans(self, name: str | None = None) -> list[ReadableSpan]: ...

    def records(self, message: str) -> list[Any]: ...

    def points(self, metric_name: str) -> dict[tuple[tuple[str, object], ...], float]: ...


def url(operation: str, number: int = 1, *, session: UUID = SESSION, turn: UUID = TURN) -> str:
    return f"/v1/sessions/{session}/turns/{turn}/citations/{number}{SUFFIXES[operation]}"


# --- The stored turn ---------------------------------------------------------------------------


def stored_citation(number: int = 1, **changes: Any) -> StoredCitation:
    values: dict[str, Any] = {
        "number": number,
        "title": "Travel Policy",
        "page_number": PAGE,
        "heading_path": ["Expenses", "", "Hotels"],
        "content_type": CitationContentType.PDF,
        "open_as": "pdf_page",
        "source_id": f"S{number}",
        "chunk_key": f"{DOCUMENT}_{CHUNK_SET}_{number}",
        "search_index_id": INDEX,
        "physical_index_id": PHYSICAL,
        "document_id": DOCUMENT,
        "document_version_id": VERSION,
        "chunk_set_id": CHUNK_SET,
    }
    return StoredCitation.model_validate(values | changes)


def turn_item(
    user: UUID,
    *,
    status: TurnStatus = TurnStatus.COMPLETED,
    citations: list[StoredCitation] | None = None,
    session: UUID = SESSION,
    client_turn: UUID = CLIENT_TURN,
) -> dict[str, Any]:
    return turn_document(
        user_id=user,
        session_id=session,
        client_turn_id=client_turn,
        project_id=PROJECT,
        status=status,
        citations=[stored_citation()] if citations is None else citations,
    ).to_item()


# --- The fake statements -----------------------------------------------------------------------


def project_row(**changes: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "id": PROJECT,
        "name": "Travel",
        "description": "",
        "instructions": "",
        "example_questions_json": "[]",
        "status": "active",
        "is_member": 1,
    }
    return SimpleNamespace(**(values | changes))


def cited_row(**changes: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "search_index_id": INDEX,
        "is_deleted": False,
        "current_version_id": VERSION,
        "source_name": "Travel Policy.pdf",
        "content_type": ContentType.PDF.value,
        "version_number": VERSION_NUMBER,
        "sha256": SHA,
        "converted_sha256": None,
        "document_pdf_status": "built",
        "pdf_sha256": SHA,
        "index_mapped": 1,
    }
    return SimpleNamespace(**(values | changes))


@dataclass
class CitationSql:
    """The answers of Q1 and the cited-document statement; every call recorded by name."""

    project: SimpleNamespace | None = field(default_factory=project_row)
    document: SimpleNamespace | BaseException | None = field(default_factory=cited_row)
    calls: list[tuple[str, Any]] = field(default_factory=list)

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


@pytest.fixture
def citation_sql(fake_sql: Any, monkeypatch: pytest.MonkeyPatch) -> CitationSql:
    script = CitationSql()
    fetch_one = fake_sql.fetch_one

    async def answer(
        statement: object, parameters: Any = None, *, name: str, retry: str = "transient"
    ) -> Any:
        if name == "authorize_project":
            script.calls.append((name, parameters))
            return script.project
        if name == "cited_document":
            script.calls.append((name, parameters))
            if isinstance(script.document, BaseException):
                raise script.document
            return script.document
        return await fetch_one(statement, parameters, name=name, retry=retry)

    monkeypatch.setattr(fake_sql, "fetch_one", answer)
    return script


# --- The app -----------------------------------------------------------------------------------


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
    return BlobTransport(blobs={PAGE_URL: PAGE_PDF, DOCUMENT_URL: WHOLE_PDF})


@pytest.fixture
def renditions(transport: BlobTransport) -> ContainerClient:
    """A real `renditions` client over the recording transport, built outside the loop."""
    return ContainerClient(
        ACCOUNT,
        "renditions",
        transport=transport,
        retry_total=0,
        max_single_get_size=MIB,
        max_chunk_get_size=MIB,
    )


async def serve(
    ports: Ports, signing_keys: SigningKeys, renditions: ContainerClient, clock: FakeClock
) -> AsyncIterator[ApiUnderTest]:
    async with (
        renditions,
        running_api(settings=api_settings(), ports=ports, signing_keys=signing_keys) as api,
    ):
        api.app.dependency_overrides[renditions_container] = lambda: renditions
        api.app.dependency_overrides[download_clock] = lambda: clock
        yield api


@pytest.fixture
async def api(
    *,
    ports: Ports,
    signing_keys: SigningKeys,
    renditions: ContainerClient,
    clock: FakeClock,
    captured: Captured,
    citation_sql: CitationSql,
) -> AsyncIterator[ApiUnderTest]:
    async for started in serve(ports, signing_keys, renditions, clock):
        yield started


@pytest.fixture
def alices_turn(containers: dict[str, FakeContainer], alice: SharedPrincipal) -> dict[str, Any]:
    return containers["conversations"].put(turn_item(alice.oid))


def window_count(containers: dict[str, FakeContainer], user: UUID, clock: FakeClock) -> int:
    item = containers["usage"].get(str(user), download_rate_window_id(epoch_minute(clock.now())))
    return 0 if item is None else int(item["count"])


def leases(containers: dict[str, FakeContainer], user: UUID) -> list[dict[str, Any]]:
    return [
        item
        for slot in (1, 2)
        if (item := containers["usage"].get(str(user), download_lease_id(slot))) is not None
    ]


def problem(response: httpx.Response) -> tuple[int, str]:
    return response.status_code, response.json()["code"]


# --- Routing (T-212) ---------------------------------------------------------------------------


def test_t212_the_three_routes_are_on_the_route_class_with_the_default_options() -> None:
    routes = {
        route.operation_id: route
        for route in citation_routes.router.routes
        if isinstance(route, DocChatRoute)
    }
    assert set(routes) == set(OPERATIONS)
    for route in routes.values():
        assert route.route_options.burst_limited is True
        assert route.route_options.write_limited is False
        assert route.access_spec.level == "user"
        assert route.access_spec.scoped_params == ("sessionId", "turnId", "citationNumber")
        dependencies = {dependency.call for dependency in route.dependant.dependencies}
        assert current_process in dependencies
    assert routes["getCitation"].path == CITATION_PATH
    assert routes["getCitationContent"].path == f"{CITATION_PATH}/content"
    assert routes["getCitationDocument"].path == f"{CITATION_PATH}/document"


async def test_t212_an_empty_burst_bucket_answers_before_path_validation_and_the_window(
    *,
    ports: Ports,
    signing_keys: SigningKeys,
    renditions: ContainerClient,
    clock: FakeClock,
    captured: Captured,
    citation_sql: CitationSql,
    containers: dict[str, FakeContainer],
    alice: SharedPrincipal,
) -> None:
    limiter = BurstLimiter(clock)
    for _ in range(40):
        limiter.check(alice.oid)
    limited = replace(ports, burst_limiter=limiter)

    async for api in serve(limited, signing_keys, renditions, clock):
        for operation in OPERATIONS:
            response = await api.client.get(
                f"/v1/sessions/not-a-uuid/turns/{TURN}/citations/99{SUFFIXES[operation]}",
                headers=alice.authorization(),
            )
            assert problem(response) == (429, "too_many_requests")
            assert response.json()["retryAfterSeconds"] == 1

    assert containers["usage"].calls == []
    assert containers["conversations"].calls == []


# --- getCitation -------------------------------------------------------------------------------


async def test_t276_a_pdf_citation_returns_its_detail(
    api: ApiUnderTest, alice: SharedPrincipal, alices_turn: dict[str, Any], transport: BlobTransport
) -> None:
    response = await api.client.get(url("getCitation"), headers=alice.authorization())

    assert response.status_code == 200
    assert response.json() == {
        "number": 1,
        "title": "Travel Policy",
        "pageNumber": PAGE,
        "headingPath": ["Expenses", "Hotels"],
        "contentType": "application/pdf",
        "openAs": "pdf_page",
        "fileName": "Travel Policy-page-2.pdf",
        "documentPdfAvailable": True,
        "documentFileName": "Travel Policy.pdf",
    }
    assert transport.requests == []


@pytest.mark.parametrize(
    ("content_type", "source_name"),
    [(ContentType.DOCX, "Handbook.docx"), (ContentType.PPTX, "Board Pack.pptx")],
)
async def test_t276_an_office_citation_opens_as_a_page_of_its_converted_pdf(
    *,
    api: ApiUnderTest,
    alice: SharedPrincipal,
    alices_turn: dict[str, Any],
    citation_sql: CitationSql,
    content_type: ContentType,
    source_name: str,
) -> None:
    citation_sql.document = cited_row(
        content_type=content_type.value,
        source_name=source_name,
        sha256=bytes(32),
        converted_sha256=CONVERTED,
        pdf_sha256=CONVERTED,
    )

    response = await api.client.get(url("getCitation"), headers=alice.authorization())

    body = response.json()
    stem = source_name.rsplit(".", 1)[0]
    assert response.status_code == 200
    assert (body["contentType"], body["openAs"]) == (content_type.value, "pdf_page")
    assert body["fileName"] == f"{stem}-page-2.pdf"
    assert body["documentFileName"] == f"{stem}.pdf"


@pytest.mark.parametrize("status", ["too_large", None])
async def test_t276_a_version_without_a_whole_document_rendition_still_opens(
    *,
    api: ApiUnderTest,
    alice: SharedPrincipal,
    alices_turn: dict[str, Any],
    citation_sql: CitationSql,
    containers: dict[str, FakeContainer],
    transport: BlobTransport,
    clock: FakeClock,
    status: str | None,
) -> None:
    citation_sql.document = cited_row(document_pdf_status=status)

    detail = await api.client.get(url("getCitation"), headers=alice.authorization())
    page = await api.client.get(url("getCitationContent"), headers=alice.authorization())
    whole = await api.client.get(url("getCitationDocument"), headers=alice.authorization())

    assert detail.status_code == 200
    assert detail.json()["documentPdfAvailable"] is False
    assert detail.json()["documentFileName"] == "Travel Policy.pdf"
    assert page.status_code == 200
    assert problem(whole) == (409, "document_pdf_unavailable")
    assert DOCUMENT_URL not in transport.paths()
    assert leases(containers, alice.oid) == []
    assert window_count(containers, alice.oid, clock) == 2


@pytest.mark.parametrize(
    ("path", "where", "reason"),
    [
        (f"/v1/sessions/{SESSION}x/turns/{TURN}/citations/1", "path.sessionId", "invalid_uuid"),
        (f"/v1/sessions/{SESSION}/turns/{TURN.hex}/citations/1", "path.turnId", "invalid_uuid"),
        (f"{BASE}/one", "path.citationNumber", "invalid_type"),
        (f"{BASE}/+1", "path.citationNumber", "invalid_type"),
        (f"{BASE}/0", "path.citationNumber", "out_of_range"),
        (f"{BASE}/17", "path.citationNumber", "out_of_range"),
        (f"{BASE}/{'9' * 5000}", "path.citationNumber", "out_of_range"),
    ],
)
async def test_path_validation_answers_before_the_window_and_the_turn(
    *,
    api: ApiUnderTest,
    alice: SharedPrincipal,
    containers: dict[str, FakeContainer],
    path: str,
    where: str,
    reason: str,
) -> None:
    for suffix in ("", "/content", "/document"):
        response = await api.client.get(path + suffix, headers=alice.authorization())

        assert problem(response) == (422, "validation_failed")
        assert response.json()["errors"] == [{"path": where, "reason": reason}]
    assert containers["usage"].calls == []
    assert containers["conversations"].calls == []


# --- The checks in order -----------------------------------------------------------------------


@dataclass(frozen=True)
class Refusal:
    """One cause of a refusal, its answer and the statements run before it answered."""

    name: str
    code: str
    statements: tuple[str, ...]
    status: TurnStatus | None = TurnStatus.COMPLETED
    project: SimpleNamespace | None = field(default_factory=project_row)
    document: SimpleNamespace | None = field(default_factory=cited_row)
    number: int = 1


REFUSALS: Final = (
    Refusal("no_turn", "citation_not_found", (), status=None),
    Refusal("streaming_turn", "citation_not_found", (), status=TurnStatus.STREAMING),
    Refusal("project_absent", "citation_not_found", ("authorize_project",), project=None),
    Refusal(
        "project_archived",
        "citation_not_found",
        ("authorize_project",),
        project=project_row(status="archived"),
    ),
    Refusal(
        "membership_removed",
        "citation_not_found",
        ("authorize_project",),
        project=project_row(is_member=0),
    ),
    Refusal("unknown_number", "citation_not_found", ("authorize_project",), number=16),
    Refusal(
        "no_document_row",
        "document_changed",
        ("authorize_project", "cited_document"),
        document=None,
    ),
    Refusal(
        "index_unmapped",
        "citation_not_found",
        ("authorize_project", "cited_document"),
        document=cited_row(index_mapped=0, is_deleted=True),
    ),
    Refusal(
        "document_deleted",
        "document_changed",
        ("authorize_project", "cited_document"),
        document=cited_row(is_deleted=True, current_version_id=None),
    ),
    Refusal(
        "version_superseded",
        "document_changed",
        ("authorize_project", "cited_document"),
        document=cited_row(current_version_id=uuid4()),
    ),
    Refusal(
        "renditions_of_another_pdf",
        "document_changed",
        ("authorize_project", "cited_document"),
        document=cited_row(pdf_sha256=bytes(32)),
    ),
    Refusal(
        "chunk_set_without_pdf_hash",
        "document_changed",
        ("authorize_project", "cited_document"),
        document=cited_row(pdf_sha256=None),
    ),
    Refusal(
        "converted_pdf_not_recorded",
        "document_changed",
        ("authorize_project", "cited_document"),
        document=cited_row(
            content_type=ContentType.DOCX.value, converted_sha256=None, pdf_sha256=CONVERTED
        ),
    ),
)


@pytest.mark.parametrize("refusal", REFUSALS, ids=[refusal.name for refusal in REFUSALS])
async def test_t276_each_refusal_in_order_on_all_three_operations(
    *,
    api: ApiUnderTest,
    alice: SharedPrincipal,
    containers: dict[str, FakeContainer],
    citation_sql: CitationSql,
    transport: BlobTransport,
    clock: FakeClock,
    refusal: Refusal,
) -> None:
    if refusal.status is not None:
        containers["conversations"].put(turn_item(alice.oid, status=refusal.status))
    citation_sql.project = refusal.project
    citation_sql.document = refusal.document

    for operation in OPERATIONS:
        citation_sql.calls.clear()
        response = await api.client.get(
            url(operation, refusal.number), headers=alice.authorization()
        )

        status = 404 if refusal.code == "citation_not_found" else 410
        assert problem(response) == (status, refusal.code), operation
        assert tuple(citation_sql.names()) == refusal.statements
    assert transport.requests == []
    assert leases(containers, alice.oid) == []
    assert window_count(containers, alice.oid, clock) == 2


async def test_the_not_found_bodies_are_identical_for_every_cause(
    api: ApiUnderTest,
    alice: SharedPrincipal,
    bob: SharedPrincipal,
    containers: dict[str, FakeContainer],
) -> None:
    containers["conversations"].put(turn_item(bob.oid))

    foreign = await api.client.get(url("getCitation"), headers=alice.authorization())
    random = await api.client.get(
        url("getCitation", session=uuid4(), turn=uuid4()), headers=alice.authorization()
    )

    assert problem(foreign) == (404, "citation_not_found")

    def without_correlation(response: httpx.Response) -> dict[str, Any]:
        body: dict[str, Any] = response.json()
        del body["correlationId"]
        return body

    assert without_correlation(foreign) == without_correlation(random)
    (read,) = containers["conversations"].calls_of("read_item")[:1]
    assert read.partition_key == (str(alice.oid), str(SESSION))


@pytest.mark.parametrize("status", [TurnStatus.FAILED, TurnStatus.CANCELLED])
async def test_a_citation_of_a_failed_or_cancelled_turn_opens(
    api: ApiUnderTest,
    alice: SharedPrincipal,
    containers: dict[str, FakeContainer],
    status: TurnStatus,
) -> None:
    containers["conversations"].put(turn_item(alice.oid, status=status))

    response = await api.client.get(url("getCitation"), headers=alice.authorization())

    assert response.status_code == 200


async def test_an_administrator_opens_a_citation_of_a_project_they_reach_without_membership(
    api: ApiUnderTest,
    carol: SharedPrincipal,
    containers: dict[str, FakeContainer],
    citation_sql: CitationSql,
) -> None:
    containers["conversations"].put(turn_item(carol.oid))
    citation_sql.project = project_row(is_member=0)

    response = await api.client.get(url("getCitation"), headers=carol.authorization())

    assert response.status_code == 200


async def test_an_unreachable_conversation_store_is_a_503(
    api: ApiUnderTest,
    alice: SharedPrincipal,
    containers: dict[str, FakeContainer],
    alices_turn: dict[str, Any],
) -> None:
    containers["conversations"].fail("read_item", status=503, times=3)

    for operation in OPERATIONS:
        response = await api.client.get(url(operation), headers=alice.authorization())

        assert problem(response) == (503, "dependency_unavailable")
        assert response.json()["retryAfterSeconds"] == 5
    assert leases(containers, alice.oid) == []


async def test_an_unreachable_database_is_a_503(
    *,
    api: ApiUnderTest,
    alice: SharedPrincipal,
    citation_sql: CitationSql,
    alices_turn: dict[str, Any],
    containers: dict[str, FakeContainer],
    captured: Captured,
) -> None:
    citation_sql.document = SqlUnavailable("exhausted", 40613, "")

    for operation in OPERATIONS:
        response = await api.client.get(url(operation), headers=alice.authorization())

        assert problem(response) == (503, "dependency_unavailable")
    assert leases(containers, alice.oid) == []
    outcomes = captured.points("docchat.citation.requests")
    assert {dict(key)["outcome"] for key in outcomes} == {"unavailable"}


# --- The files ---------------------------------------------------------------------------------


async def test_t276_the_page_rendition_streams_with_the_file_headers(
    *,
    api: ApiUnderTest,
    alice: SharedPrincipal,
    alices_turn: dict[str, Any],
    containers: dict[str, FakeContainer],
    transport: BlobTransport,
    clock: FakeClock,
) -> None:
    response = await api.client.get(
        url("getCitationContent"),
        headers=alice.authorization() | {"Accept-Encoding": "gzip, br", "Range": "bytes=0-9"},
    )

    assert response.status_code == 200
    assert response.content == PAGE_PDF
    headers = response.headers
    assert headers["content-type"] == "application/pdf"
    assert headers["content-length"] == str(len(PAGE_PDF))
    assert headers["content-disposition"] == content_disposition("Travel Policy-page-2.pdf")
    assert headers["content-disposition"].startswith("attachment; ")
    assert "filename*=UTF-8''" in headers["content-disposition"]
    assert headers["content-security-policy"] == "sandbox"
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["strict-transport-security"] == "max-age=31536000"
    assert "content-encoding" not in headers
    assert "content-range" not in headers
    assert set(transport.paths()) == {PAGE_URL}
    assert all(path.startswith("/renditions/") for path in transport.paths())
    assert window_count(containers, alice.oid, clock) == 1
    (created,) = [
        call
        for call in containers["usage"].calls_of("create_item")
        if call.item_id == download_lease_id(1)
    ]
    assert created.partition_key == (str(alice.oid),)
    assert leases(containers, alice.oid) == []


async def test_t276_the_whole_document_rendition_streams_under_its_name(
    api: ApiUnderTest,
    alice: SharedPrincipal,
    alices_turn: dict[str, Any],
    containers: dict[str, FakeContainer],
    transport: BlobTransport,
) -> None:
    response = await api.client.get(url("getCitationDocument"), headers=alice.authorization())

    assert response.status_code == 200
    assert response.content == WHOLE_PDF
    assert response.headers["content-length"] == str(len(WHOLE_PDF))
    assert response.headers["content-disposition"] == content_disposition("Travel Policy.pdf")
    assert response.headers["content-security-policy"] == "sandbox"
    assert set(transport.paths()) == {DOCUMENT_URL}
    assert len(transport.requests) == 3
    assert leases(containers, alice.oid) == []


async def test_the_lease_names_the_cited_turn(
    api: ApiUnderTest,
    alice: SharedPrincipal,
    alices_turn: dict[str, Any],
    containers: dict[str, FakeContainer],
) -> None:
    written: list[dict[str, Any]] = []

    def keep() -> None:
        lease = containers["usage"].get(str(alice.oid), download_lease_id(1))
        if lease is not None:
            written.append(lease)

    containers["usage"].before("delete_item", keep)

    response = await api.client.get(url("getCitationContent"), headers=alice.authorization())

    assert response.status_code == 200
    (lease,) = written
    assert (lease["sessionId"], lease["turnId"]) == (str(SESSION), str(TURN))


async def test_t276_a_missing_blob_is_500_citation_content_missing(
    *,
    api: ApiUnderTest,
    alice: SharedPrincipal,
    alices_turn: dict[str, Any],
    containers: dict[str, FakeContainer],
    transport: BlobTransport,
    captured: Captured,
) -> None:
    del transport.blobs[PAGE_URL]

    response = await api.client.get(url("getCitationContent"), headers=alice.authorization())

    assert problem(response) == (500, "citation_content_missing")
    (record,) = captured.records("citation_blob_missing")
    assert record.__dict__["docchat.session_id"] == str(SESSION)
    assert record.__dict__["docchat.turn_id"] == str(TURN)
    assert record.__dict__["docchat.citation.number"] == 1
    assert record.__dict__["docchat.document_id"] == str(DOCUMENT)
    assert record.__dict__["docchat.version_id"] == str(VERSION)
    assert leases(containers, alice.oid) == []
    assert captured.points("docchat.citation.requests") == {
        (("operation", "getCitationContent"), ("outcome", "missing")): 1
    }


async def test_t279_a_full_window_answers_429_rate_limited_before_the_turn(
    api: ApiUnderTest,
    alice: SharedPrincipal,
    alices_turn: dict[str, Any],
    containers: dict[str, FakeContainer],
    clock: FakeClock,
) -> None:
    containers["usage"].put(
        {
            "id": download_rate_window_id(epoch_minute(clock.now())),
            "userId": str(alice.oid),
            "type": "download_rate_window",
            "count": 30,
            "ttl": 180,
        }
    )

    for operation in ("getCitationContent", "getCitationDocument"):
        response = await api.client.get(url(operation), headers=alice.authorization())

        assert problem(response) == (429, "rate_limited")
        retry = response.json()["retryAfterSeconds"]
        assert 1 <= retry <= 120
        assert response.headers["retry-after"] == str(retry)
    assert window_count(containers, alice.oid, clock) == 30
    assert containers["conversations"].calls == []
    assert leases(containers, alice.oid) == []


async def test_t278_every_live_lease_answers_429_too_many_downloads(
    *,
    api: ApiUnderTest,
    alice: SharedPrincipal,
    alices_turn: dict[str, Any],
    containers: dict[str, FakeContainer],
    clock: FakeClock,
    captured: Captured,
) -> None:
    for slot in (1, 2):
        containers["usage"].put(
            {
                "id": download_lease_id(slot),
                "userId": str(alice.oid),
                "type": "download_lease",
                "sessionId": None,
                "turnId": None,
                "acquiredAt": "2026-01-01T00:00:00.000Z",
                "expiresAt": "2026-01-01T00:05:00.000Z",
                "ttl": 300,
            }
        )

    response = await api.client.get(url("getCitationDocument"), headers=alice.authorization())

    assert problem(response) == (429, "too_many_downloads")
    assert response.json()["retryAfterSeconds"] == 10
    assert window_count(containers, alice.oid, clock) == 1
    assert containers["conversations"].calls == []
    assert captured.points("docchat.ratelimit.rejections") == {
        (("reason", "download_concurrency"),): 1
    }


async def test_an_unreachable_usage_store_fails_closed(
    api: ApiUnderTest,
    alice: SharedPrincipal,
    alices_turn: dict[str, Any],
    containers: dict[str, FakeContainer],
) -> None:
    containers["usage"].fail("read_item", status=503, times=4)

    response = await api.client.get(url("getCitationContent"), headers=alice.authorization())

    assert problem(response) == (503, "dependency_unavailable")
    assert containers["conversations"].calls == []


# --- Telemetry ---------------------------------------------------------------------------------


async def test_each_call_is_one_span_and_one_count_with_its_outcome(
    api: ApiUnderTest,
    alice: SharedPrincipal,
    alices_turn: dict[str, Any],
    captured: Captured,
) -> None:
    await api.client.get(url("getCitation"), headers=alice.authorization())
    await api.client.get(url("getCitationContent"), headers=alice.authorization())
    await api.client.get(url("getCitationDocument", 9), headers=alice.authorization())

    spans = captured.finished_spans("docchat.citation.open")
    assert [
        (
            span.attributes["docchat.operation"],
            span.attributes["docchat.open_as"],
            span.attributes["docchat.outcome"],
        )
        for span in spans
        if span.attributes is not None
    ] == [
        ("getCitation", "pdf_page", "ok"),
        ("getCitationContent", "pdf_page", "ok"),
        ("getCitationDocument", "document_pdf", "not_found"),
    ]
    assert captured.points("docchat.citation.requests") == {
        (("operation", "getCitation"), ("outcome", "ok")): 1,
        (("operation", "getCitationContent"), ("outcome", "ok")): 1,
        (("operation", "getCitationDocument"), ("outcome", "not_found")): 1,
    }


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (CitationNotFound(), "not_found"),
        (DocumentChanged(), "gone"),
        (CitationContentMissing(), "missing"),
        (RateLimited(retry_after_seconds=3), "limited"),
        (SqlUnavailable("exhausted", 40613, ""), "unavailable"),
        (RuntimeError("defect"), None),
    ],
)
def test_each_refusal_records_its_outcome(error: BaseException, outcome: str | None) -> None:
    assert outcome_of(error) == outcome
