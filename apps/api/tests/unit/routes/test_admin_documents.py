"""Unit tests of the administrator document routes (`11-api.md` sections 3.11.1 to 3.11.4) on the
in-process app over the fake data-access layer. The repository functions are replaced by
recorders that play the transaction's part: a recorder given a `version` runs the `If-Match`
check on it, after its `404` and before its `409`; the statements are
`packages/core/tests/unit/sql/repositories/test_documents.py`'s, and the whole operations against
SQL Server `apps/api/tests/integration/routes/test_admin_documents.py`'s."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

import pytest
from opentelemetry.sdk.trace import ReadableSpan

from docchat_api.http.paging import encode_cursor
from docchat_api.http.queue import ingestion_queue
from docchat_api.routes.admin_documents import DocumentsCursor, normalise_prefix
from docchat_core.audit import AuditEvent
from docchat_core.audit.models import DocumentDeletedMetadata
from docchat_core.clients import ClientRegistry
from docchat_core.ingestion.models import IngestionRefusedError
from docchat_core.models import encode_row_version
from docchat_core.sql.repositories import documents
from docchat_core.sql.repositories.documents import (
    DocumentDeleted,
    DocumentDetailRow,
    DocumentPosition,
    DocumentReingested,
    DocumentRow,
)
from docchat_testing.fakes.queue import RecordingQueue
from docchat_testing.fixtures.api import ApiUnderTest
from docchat_testing.fixtures.identities import SharedPrincipal

AT = datetime(2026, 10, 21, 9, 15, 2, 123000, tzinfo=UTC)
INDEX = UUID("8b3c4d5e-6f7a-4b9c-8d1e-2f3a4b5c6d62")
DOCUMENT = UUID("ad5e6f7a-8b9c-4dae-9f20-4b5c6d7e8f64")
ROW_VERSION = b"\x00\x00\x00\x00\x00\x00\x07\xd1"
NEW_ROW_VERSION = b"\x00\x00\x00\x00\x00\x00\x07\xd2"
STALE = b"\x00\x00\x00\x00\x00\x00\x07\xd0"
IF_MATCH = {"If-Match": f'"{encode_row_version(ROW_VERSION)}"'}
DOCUMENTS = f"/v1/admin/indexes/{INDEX}/documents"
ONE = f"{DOCUMENTS}/{DOCUMENT}"


class Captured(Protocol):
    def finished_spans(self, name: str | None = None) -> list[ReadableSpan]: ...

    def events(self, name: str) -> list[dict[str, Any]]: ...


@dataclass
class Fake:
    result: Any = None
    before: BaseException | None = None
    version: bytes | None = None
    error: BaseException | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, engine: object, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.before is not None:
            raise fresh(self.before)
        if self.version is not None:
            kwargs["check_version"](self.version)
        if self.error is not None:
            raise fresh(self.error)
        return self.result


def fresh(error: BaseException) -> BaseException:
    """A copy of a parametrized exception: the parameter lives for the session, and raising it
    would attach a traceback whose frames keep that test's app alive."""
    if isinstance(error, IngestionRefusedError):
        return IngestionRefusedError(error.code)
    raise TypeError(f"no copy for {type(error).__name__}")


def document_row(name: str = "travel policy.pdf", row_version: bytes = ROW_VERSION) -> DocumentRow:
    return DocumentRow(
        id=DOCUMENT if name == "travel policy.pdf" else uuid4(),
        source_name=name.title(),
        normalized_name=name,
        display_state="ingested",
        current_version=None,
        latest_version=None,
        updated_at=AT,
        row_version=row_version,
    )


def deleted_event(run: UUID) -> AuditEvent:
    return AuditEvent(
        action="document.deleted",
        actor_type="user",
        actor_object_id=uuid4(),
        target_type="document",
        target_id=str(DOCUMENT),
        metadata=DocumentDeletedMetadata(
            index_id=INDEX, run_id=run, superseded_version_count=1, cancelled_item_count=0
        ),
    )


def patch(monkeypatch: pytest.MonkeyPatch, name: str, fake: Fake) -> Fake:
    monkeypatch.setattr(documents, name, fake)
    return fake


@pytest.fixture(autouse=True)
def recording_queue(monkeypatch: pytest.MonkeyPatch) -> RecordingQueue:
    """The `ingestion-requests` queue the `ingestion_queue` dependency hands out."""
    recording = RecordingQueue()

    class Registry:
        def queue(self, name: str) -> RecordingQueue:
            assert name == "ingestion-requests"
            return recording

    monkeypatch.setattr(ClientRegistry, "current", classmethod(lambda cls: Registry()))
    return recording


def queue_of(started: ApiUnderTest) -> RecordingQueue:
    return cast("RecordingQueue", ingestion_queue())


# --- adminListDocuments ---------------------------------------------------------------------


def test_the_prefix_is_normalised_as_document_names_are() -> None:
    assert normalise_prefix("  Travel　 POLICY ") == "travel policy"
    assert normalise_prefix("é") == "é"
    assert normalise_prefix(3) == 3


async def test_document_pages_follow_the_cursor(
    started: ApiUnderTest, carol: SharedPrincipal, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [document_row(name) for name in ("a.pdf", "b.pdf", "c.pdf")]
    fake = patch(monkeypatch, "read_documents", Fake(rows))

    first = await started.client.get(
        DOCUMENTS,
        params={"limit": 2, "state": "failed", "q": " A "},
        headers=carol.authorization(),
    )

    body = first.json()
    assert [item["name"] for item in body["items"]] == ["A.Pdf", "B.Pdf"]
    assert body["nextCursor"] == encode_cursor(
        DocumentsCursor(n="b.pdf", i=rows[1].id, s="failed", q="a")
    )
    call = fake.calls[0]
    assert call["index_id"] == INDEX
    assert (call["filters"].display_state, call["filters"].name_prefix) == ("failed", "a")
    assert call["limit_plus_one"] == 3

    fake.result = rows[2:]
    second = await started.client.get(
        DOCUMENTS,
        params={"limit": 2, "state": "failed", "q": "a", "cursor": body["nextCursor"]},
        headers=carol.authorization(),
    )

    assert second.json()["nextCursor"] is None
    assert fake.calls[1]["after"] == DocumentPosition(
        normalized_name="b.pdf", document_id=rows[1].id
    )


async def test_an_unknown_index_is_index_not_found(
    started: ApiUnderTest, carol: SharedPrincipal, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch(monkeypatch, "read_documents", Fake(before=IngestionRefusedError("index_not_found")))

    response = await started.client.get(DOCUMENTS, headers=carol.authorization())

    assert (response.status_code, response.json()["code"]) == (404, "index_not_found")


async def test_list_parameters_are_validated(started: ApiUnderTest, carol: SharedPrincipal) -> None:
    response = await started.client.get(
        DOCUMENTS,
        params={"state": "no_content", "q": "   ", "x": "1"},
        headers=carol.authorization(),
    )

    assert response.json()["errors"] == [
        {"path": "query.state", "reason": "not_allowed"},
        {"path": "query.q", "reason": "too_short"},
        {"path": "query.x", "reason": "unknown_field"},
    ]


# --- adminGetDocument -----------------------------------------------------------------------


async def test_get_answers_the_detail(
    started: ApiUnderTest, carol: SharedPrincipal, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = patch(
        monkeypatch,
        "read_document",
        Fake(DocumentDetailRow(document=document_row(), versions=(), recent_items=())),
    )

    response = await started.client.get(ONE, headers=carol.authorization())

    assert response.status_code == 200
    assert response.json()["versions"] == []
    assert response.json()["recentItems"] == []
    assert fake.calls == [{"index_id": INDEX, "document_id": DOCUMENT}]


@pytest.mark.parametrize("code", ["index_not_found", "document_not_found"])
async def test_get_not_found(
    started: ApiUnderTest, carol: SharedPrincipal, monkeypatch: pytest.MonkeyPatch, code: Any
) -> None:
    patch(monkeypatch, "read_document", Fake(before=IngestionRefusedError(code)))

    response = await started.client.get(ONE, headers=carol.authorization())

    assert (response.status_code, response.json()["code"]) == (404, code)


# --- adminDeleteDocument --------------------------------------------------------------------


async def test_a_delete_answers_202_and_sends_one_message(
    started: ApiUnderTest,
    carol: SharedPrincipal,
    monkeypatch: pytest.MonkeyPatch,
    captured: Captured,
) -> None:
    run = uuid4()
    fake = patch(
        monkeypatch,
        "delete_document",
        Fake(DocumentDeleted(run_id=run, events=(deleted_event(run),)), version=ROW_VERSION),
    )

    response = await started.client.delete(ONE, headers=carol.authorization() | IF_MATCH)

    assert (response.status_code, response.json()) == (202, {"runId": str(run)})
    assert fake.calls[0]["requested_by"] == carol.oid
    assert queue_of(started).messages()[0].run_id == run
    (span,) = captured.finished_spans("docchat.ingestion.submit")
    assert span.attributes is not None
    assert span.attributes["docchat.ingestion.run_kind"] == "document_delete"
    assert captured.events("admin_change") == [
        {"action": "document.deleted", "targetType": "document"}
    ]


@pytest.mark.parametrize(
    ("before", "headers", "version", "status", "code"),
    [
        (IngestionRefusedError("document_not_found"), {}, ROW_VERSION, 404, "document_not_found"),
        (None, {}, ROW_VERSION, 428, "precondition_required"),
        (None, {"If-Match": "*"}, ROW_VERSION, 428, "precondition_required"),
        (None, IF_MATCH, STALE, 412, "precondition_failed"),
        (None, IF_MATCH, ROW_VERSION, 409, "index_busy"),
    ],
)
async def test_delete_answers_404_428_412_then_409(
    *,
    started: ApiUnderTest,
    carol: SharedPrincipal,
    monkeypatch: pytest.MonkeyPatch,
    before: BaseException | None,
    headers: dict[str, str],
    version: bytes,
    status: int,
    code: str,
) -> None:
    patch(
        monkeypatch,
        "delete_document",
        Fake(before=before, version=version, error=IngestionRefusedError("index_busy")),
    )

    response = await started.client.delete(ONE, headers=carol.authorization() | headers)

    assert (response.status_code, response.json()["code"]) == (status, code)
    assert queue_of(started).sent == []


# --- adminReingestDocument ------------------------------------------------------------------


async def test_a_new_reingest_run_answers_202(
    started: ApiUnderTest, carol: SharedPrincipal, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = uuid4()
    patch(
        monkeypatch,
        "reingest_document",
        Fake(
            DocumentReingested(
                run_id=run,
                created=True,
                item_count=1,
                document=document_row(row_version=NEW_ROW_VERSION),
                events=(),
            ),
            version=ROW_VERSION,
        ),
    )

    response = await started.client.post(
        f"{ONE}/reingest", json={}, headers=carol.authorization() | IF_MATCH
    )

    assert response.status_code == 202
    body = response.json()
    assert (body["runId"], body["itemCount"]) == (str(run), 1)
    assert body["document"]["rowVersion"] == encode_row_version(NEW_ROW_VERSION)
    assert queue_of(started).messages()[0].run_id == run


async def test_an_open_item_answers_200_with_its_run_and_no_message(
    started: ApiUnderTest,
    carol: SharedPrincipal,
    monkeypatch: pytest.MonkeyPatch,
    captured: Captured,
) -> None:
    run = uuid4()
    patch(
        monkeypatch,
        "reingest_document",
        Fake(
            DocumentReingested(
                run_id=run, created=False, item_count=0, document=document_row(), events=()
            ),
            version=ROW_VERSION,
        ),
    )

    response = await started.client.post(
        f"{ONE}/reingest", json={}, headers=carol.authorization() | IF_MATCH
    )

    assert response.status_code == 200
    assert (response.json()["runId"], response.json()["itemCount"]) == (str(run), 0)
    assert queue_of(started).sent == []
    (span,) = captured.finished_spans("docchat.ingestion.submit")
    assert span.attributes == {}


@pytest.mark.parametrize("code", ["no_current_version", "index_busy"])
async def test_reingest_state_rules(
    started: ApiUnderTest, carol: SharedPrincipal, monkeypatch: pytest.MonkeyPatch, code: Any
) -> None:
    patch(
        monkeypatch,
        "reingest_document",
        Fake(version=ROW_VERSION, error=IngestionRefusedError(code)),
    )

    response = await started.client.post(
        f"{ONE}/reingest", json={}, headers=carol.authorization() | IF_MATCH
    )

    assert (response.status_code, response.json()["code"]) == (409, code)


async def test_the_reingest_body_is_required(
    started: ApiUnderTest, carol: SharedPrincipal, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = patch(monkeypatch, "reingest_document", Fake())

    response = await started.client.post(
        f"{ONE}/reingest",
        content=b"[]",
        headers=carol.authorization() | IF_MATCH | {"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert response.json()["errors"] == [{"path": "body", "reason": "invalid_type"}]
    assert fake.calls == []
