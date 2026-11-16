"""Unit tests of the administrator index routes (`11-api.md` section 3.10) on the in-process app
over the fake data-access layer. The repository functions are replaced by recorders that play
the transaction's part: a recorder given a `version` runs the operation's `If-Match` check on it,
as the locked read does, after its `404` and before its `409`; the statements themselves are
`packages/core/tests/unit/sql/repositories/test_indexes.py`'s, and the whole operations against
SQL Server `apps/api/tests/integration/routes/test_admin_indexes.py`'s."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

import pytest
from opentelemetry.sdk.trace import ReadableSpan

from docchat_api.http.paging import encode_cursor
from docchat_api.http.queue import ingestion_queue
from docchat_api.routes.admin_indexes import IndexesCursor
from docchat_core.audit import AuditEvent
from docchat_core.audit.models import IndexUpdatedMetadata
from docchat_core.clients import ClientRegistry
from docchat_core.ingestion.models import (
    ContentType,
    IndexStatus,
    IngestionRefusedError,
    PhysicalIndexState,
    ReingestMode,
)
from docchat_core.models import encode_row_version
from docchat_core.sql.engine import SqlUniqueViolation
from docchat_core.sql.repositories import indexes
from docchat_core.sql.repositories.indexes import (
    DocumentCounts,
    IndexCreated,
    IndexDeleted,
    IndexDetailRow,
    IndexSettingsRefusedError,
    IndexSummaryRow,
    IndexUpdated,
    MethodOptionRow,
    PhysicalIndexRow,
    ReingestStarted,
)
from docchat_testing.fakes.queue import RecordingQueue
from docchat_testing.fixtures.api import ApiUnderTest
from docchat_testing.fixtures.identities import SharedPrincipal

AT = datetime(2026, 10, 21, 9, 15, 2, 123000, tzinfo=UTC)
INDEX = UUID("8b3c4d5e-6f7a-4b9c-8d1e-2f3a4b5c6d62")
ROW_VERSION = b"\x00\x00\x00\x00\x00\x00\x07\xd1"
STALE = b"\x00\x00\x00\x00\x00\x00\x07\xd0"
IF_MATCH = {"If-Match": f'"{encode_row_version(ROW_VERSION)}"'}
BASE = "/v1/admin/indexes"


class Captured(Protocol):
    def finished_spans(self, name: str | None = None) -> list[ReadableSpan]: ...

    def events(self, name: str) -> list[dict[str, Any]]: ...


@dataclass
class Fake:
    """A repository function: records its keyword arguments, raises `before` (a lookup's
    refusal), runs `check_version(version)` when given one, then raises `error` or answers
    `result`."""

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
    if isinstance(error, IndexSettingsRefusedError):
        return IndexSettingsRefusedError(error.code, error.parameter_errors)
    if isinstance(error, SqlUniqueViolation):
        return SqlUniqueViolation(error.number, error.sqlstate, error.constraint)
    raise TypeError(f"no copy for {type(error).__name__}")


def summary_row(status: IndexStatus = IndexStatus.READY, name: str = "Policy") -> IndexSummaryRow:
    return IndexSummaryRow(
        id=INDEX if name == "Policy" else uuid4(),
        display_name=name,
        status=status,
        method_key="recursive",
        method_version=1,
        method_display_name="By headings and paragraphs",
        embedding_model="text-embedding-3-large",
        embedding_dimensions=1024,
        language_analyzer="standard.lucene",
        live_document_count=0,
        updated_at=AT,
        row_version=ROW_VERSION,
    )


def detail_row(status: IndexStatus = IndexStatus.READY) -> IndexDetailRow:
    return IndexDetailRow(
        summary=summary_row(status),
        description=None,
        method_parameters={"targetTokens": 512, "overlapPercent": 25, "tableMaxTokens": 1600},
        active=PhysicalIndexRow(
            id=uuid4(),
            generation=1,
            schema_version=1,
            embedding_model="text-embedding-3-large",
            embedding_dimensions=1024,
            language_analyzer="standard.lucene",
            state=PhysicalIndexState.ACTIVE,
            created_at=AT,
            activated_at=AT,
        ),
        building=None,
        document_counts=DocumentCounts(0, 0, 0, 0),
        rebuild_progress=None,
        mapped_projects=(),
        open_run=None,
        created_at=AT,
        created_by=uuid4(),
        updated_by=uuid4(),
    )


def event(action: Any = "index.created") -> AuditEvent:
    return AuditEvent(
        action="index.updated",
        actor_type="user",
        actor_object_id=uuid4(),
        target_type="search_index",
        target_id=str(INDEX),
        metadata=IndexUpdatedMetadata(changed_fields=["displayName"], status="ready"),
    )


def patch(monkeypatch: pytest.MonkeyPatch, name: str, fake: Fake) -> Fake:
    monkeypatch.setattr(indexes, name, fake)
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


# --- adminGetIndexOptions -------------------------------------------------------------------


async def test_the_options_combine_the_catalogue_the_settings_and_the_constants(
    started: ApiUnderTest, carol: SharedPrincipal, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch(
        monkeypatch,
        "read_index_options",
        Fake(
            [
                MethodOptionRow(
                    key="recursive",
                    version=1,
                    display_name="By headings and paragraphs",
                    description="Splits.",
                    parameter_schema={"type": "object"},
                    default_parameters={"targetTokens": 512},
                    supported_content_types=(ContentType.PDF,),
                )
            ]
        ),
    )

    response = await started.client.get("/v1/admin/index-options", headers=carol.authorization())

    assert response.status_code == 200
    body = response.json()
    assert body["methods"] == [
        {
            "key": "recursive",
            "version": 1,
            "displayName": "By headings and paragraphs",
            "description": "Splits.",
            "parameterSchema": {"type": "object"},
            "defaultParameters": {"targetTokens": 512},
            "supportedContentTypes": ["application/pdf"],
        }
    ]
    assert body["embeddingModels"] == [
        {
            "model": "text-embedding-3-large",
            "modelVersion": "1",
            "dimensions": [256, 1024, 3072],
            "defaultDimensions": 1024,
        }
    ]
    assert len(body["languageAnalyzers"]) == 20
    assert body["languageAnalyzers"][0] == {"value": "standard.lucene", "label": "Any language"}
    assert body["defaults"] == {
        "methodKey": "recursive",
        "methodVersion": 1,
        "embeddingModel": "text-embedding-3-large",
        "embeddingDimensions": 1024,
        "languageAnalyzer": "standard.lucene",
    }
    assert body["uploadLimits"] == {
        "maxFileBytes": 52_428_800,
        "maxFilesPerBatch": 100,
        "allowedExtensions": [".pdf", ".docx", ".pptx"],
    }


async def test_a_user_gets_admin_required_before_any_read(
    started: ApiUnderTest, alice: SharedPrincipal, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = patch(monkeypatch, "read_index_options", Fake([]))

    response = await started.client.get("/v1/admin/index-options", headers=alice.authorization())

    assert (response.status_code, response.json()["code"]) == (403, "admin_required")
    assert fake.calls == []


# --- adminListIndexes -----------------------------------------------------------------------


async def test_pages_follow_the_cursor_with_its_filters(
    started: ApiUnderTest, carol: SharedPrincipal, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [summary_row(name=name) for name in ("Alpha", "Beta", "Gamma")]
    fake = patch(monkeypatch, "read_indexes", Fake(rows))

    first = await started.client.get(
        BASE,
        params={"limit": 2, "status": "ready", "q": "  al\tpha "},
        headers=carol.authorization(),
    )

    body = first.json()
    assert [item["displayName"] for item in body["items"]] == ["Alpha", "Beta"]
    assert body["nextCursor"] == encode_cursor(
        IndexesCursor(n="Beta", i=rows[1].id, s="ready", q="al pha")
    )
    call = fake.calls[0]
    assert (call["filters"].status, call["filters"].name_prefix) == (IndexStatus.READY, "al pha")
    assert (call["after"], call["limit_plus_one"]) == (None, 3)

    fake.result = rows[2:]
    second = await started.client.get(
        BASE,
        params={"limit": 2, "status": "ready", "q": "al pha", "cursor": body["nextCursor"]},
        headers=carol.authorization(),
    )

    assert second.json()["nextCursor"] is None
    assert fake.calls[1]["after"] == indexes.IndexPosition(display_name="Beta", index_id=rows[1].id)


async def test_a_cursor_for_other_filters_is_invalid(
    started: ApiUnderTest, carol: SharedPrincipal, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = patch(monkeypatch, "read_indexes", Fake([]))
    cursor = encode_cursor(IndexesCursor(n="Beta", i=INDEX, s=None, q=None))

    response = await started.client.get(
        BASE, params={"cursor": cursor, "status": "failed"}, headers=carol.authorization()
    )

    assert response.status_code == 422
    assert response.json()["errors"] == [{"path": "query.cursor", "reason": "invalid_cursor"}]
    assert fake.calls == []


async def test_list_parameters_are_validated(started: ApiUnderTest, carol: SharedPrincipal) -> None:
    response = await started.client.get(
        BASE, params={"status": "gone", "limit": 0, "q": "\u202e"}, headers=carol.authorization()
    )

    assert response.json()["errors"] == [
        {"path": "query.limit", "reason": "out_of_range"},
        {"path": "query.status", "reason": "not_allowed"},
        {"path": "query.q", "reason": "invalid_characters"},
    ]


# --- adminCreateIndex -----------------------------------------------------------------------

CREATE = {
    "displayName": "Policy",
    "methodKey": "recursive",
    "embeddingModel": "text-embedding-3-large",
}


async def test_a_create_answers_202_audits_and_sends_one_message(
    started: ApiUnderTest,
    carol: SharedPrincipal,
    monkeypatch: pytest.MonkeyPatch,
    captured: Captured,
) -> None:
    run = uuid4()
    fake = patch(
        monkeypatch,
        "create_index",
        Fake(
            IndexCreated(index=detail_row(IndexStatus.PROVISIONING), run_id=run, events=(event(),))
        ),
    )

    response = await started.client.post(
        BASE, json=CREATE | {"description": "  "}, headers=carol.authorization()
    )

    assert response.status_code == 202
    body = response.json()
    assert body["runId"] == str(run)
    assert body["index"]["status"] == "provisioning"
    (call,) = fake.calls
    request = call["request"]
    assert (request.display_name, request.description, request.method_version) == (
        "Policy",
        None,
        None,
    )
    assert (request.language_analyzer, call["max_indexes"], call["created_by"]) == (
        "standard.lucene",
        15,
        carol.oid,
    )
    (message,) = queue_of(started).messages()
    assert message.run_id == run
    assert call["trace"].traceparent == message.traceparent
    (span,) = captured.finished_spans("docchat.ingestion.submit")
    assert span.attributes is not None
    assert span.attributes["docchat.ingestion.run_kind"] == "index_create"
    assert span.attributes["docchat.ingestion.item_count"] == 1
    assert len(captured.events("admin_change")) == 1


async def test_a_failed_send_still_answers_202(
    started: ApiUnderTest,
    carol: SharedPrincipal,
    monkeypatch: pytest.MonkeyPatch,
    captured: Captured,
) -> None:
    run = uuid4()
    patch(monkeypatch, "create_index", Fake(IndexCreated(detail_row(), run, ())))
    queue_of(started).failing = True

    response = await started.client.post(BASE, json=CREATE, headers=carol.authorization())

    assert response.status_code == 202
    assert captured.events("ingestion_queue_send_failed") == [{"runId": str(run)}]


@pytest.mark.parametrize(
    ("field_name", "value", "reason"),
    [
        ("methodVersion", None, "invalid_type"),
        ("embeddingDimensions", 4096, "out_of_range"),
        ("displayName", "", "too_short"),
        ("methodParameters", [], "invalid_type"),
        ("languageAnalyzer", None, "invalid_type"),
    ],
)
async def test_the_body_is_validated_before_any_read(
    *,
    started: ApiUnderTest,
    carol: SharedPrincipal,
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    value: object,
    reason: str,
) -> None:
    fake = patch(monkeypatch, "create_index", Fake())

    response = await started.client.post(
        BASE, json=CREATE | {field_name: value}, headers=carol.authorization()
    )

    assert (response.status_code, response.json()["code"]) == (422, "validation_failed")
    assert response.json()["errors"] == [{"path": f"body.{field_name}", "reason": reason}]
    assert fake.calls == []


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (IndexSettingsRefusedError("method_inactive"), 422, "method_inactive"),
        (IngestionRefusedError("search_index_capacity"), 409, "search_index_capacity"),
        (
            SqlUniqueViolation(2601, "23000", "UX_search_indexes_display_name"),
            409,
            "display_name_taken",
        ),
        (SqlUniqueViolation(2627, "23000", "PK_search_indexes"), 500, "internal_error"),
        (IngestionRefusedError("index_busy"), 500, "internal_error"),
    ],
)
async def test_create_refusals(
    *,
    started: ApiUnderTest,
    carol: SharedPrincipal,
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
    status: int,
    code: str,
) -> None:
    patch(monkeypatch, "create_index", Fake(error=error))

    response = await started.client.post(BASE, json=CREATE, headers=carol.authorization())

    assert (response.status_code, response.json()["code"]) == (status, code)
    assert queue_of(started).sent == []


async def test_invalid_parameters_name_each_parameter(
    started: ApiUnderTest, carol: SharedPrincipal, monkeypatch: pytest.MonkeyPatch
) -> None:
    errors = [{"loc": ("targetTokens",), "type": "greater_than_equal", "msg": "x"}]
    patch(
        monkeypatch,
        "create_index",
        Fake(error=IndexSettingsRefusedError("invalid_method_parameters", errors)),
    )

    response = await started.client.post(BASE, json=CREATE, headers=carol.authorization())

    assert response.json()["code"] == "invalid_method_parameters"
    assert response.json()["errors"] == [
        {"path": "body.methodParameters.targetTokens", "reason": "out_of_range"}
    ]


# --- adminGetIndex --------------------------------------------------------------------------


async def test_get_answers_the_detail_or_index_not_found(
    started: ApiUnderTest, carol: SharedPrincipal, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = patch(monkeypatch, "read_index", Fake(detail_row()))

    found = await started.client.get(f"{BASE}/{str(INDEX).upper()}", headers=carol.authorization())
    fake.result = None
    missing = await started.client.get(f"{BASE}/{uuid4()}", headers=carol.authorization())

    assert found.status_code == 200
    assert found.json()["indexId"] == str(INDEX)
    assert fake.calls[0] == {"index_id": INDEX}
    assert (missing.status_code, missing.json()["code"]) == (404, "index_not_found")


async def test_a_malformed_index_id_is_invalid_uuid(
    started: ApiUnderTest, carol: SharedPrincipal
) -> None:
    response = await started.client.get(f"{BASE}/{{{INDEX}}}", headers=carol.authorization())

    assert response.json()["errors"] == [{"path": "path.indexId", "reason": "invalid_uuid"}]


# --- adminUpdateIndex -----------------------------------------------------------------------


async def test_an_empty_update_is_empty_update(
    started: ApiUnderTest, carol: SharedPrincipal, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = patch(monkeypatch, "update_index", Fake())

    response = await started.client.patch(
        f"{BASE}/{INDEX}", json={}, headers=carol.authorization() | IF_MATCH
    )

    assert (response.status_code, response.json()["code"]) == (422, "empty_update")
    assert response.json()["errors"] == [{"path": "body", "reason": "required"}]
    assert fake.calls == []


@pytest.mark.parametrize(
    ("before", "headers", "status", "code"),
    [
        (IngestionRefusedError("index_not_found"), {}, 404, "index_not_found"),
        (None, {}, 428, "precondition_required"),
        (None, {"If-Match": f'"{encode_row_version(STALE)}"'}, 412, "precondition_failed"),
        (None, {"If-Match": '"not-a-version"'}, 412, "precondition_failed"),
    ],
)
async def test_update_answers_404_then_428_then_412(
    *,
    started: ApiUnderTest,
    carol: SharedPrincipal,
    monkeypatch: pytest.MonkeyPatch,
    before: BaseException | None,
    headers: dict[str, str],
    status: int,
    code: str,
) -> None:
    patch(
        monkeypatch,
        "update_index",
        Fake(before=before, version=ROW_VERSION, error=IngestionRefusedError("index_busy")),
    )

    response = await started.client.patch(
        f"{BASE}/{INDEX}", json={"displayName": "X"}, headers=carol.authorization() | headers
    )

    assert (response.status_code, response.json()["code"]) == (status, code)


async def test_update_passes_the_fields_the_body_holds(
    started: ApiUnderTest,
    carol: SharedPrincipal,
    monkeypatch: pytest.MonkeyPatch,
    captured: Captured,
) -> None:
    fake = patch(
        monkeypatch,
        "update_index",
        Fake(IndexUpdated(index=detail_row(), events=(event(),)), version=ROW_VERSION),
    )

    response = await started.client.patch(
        f"{BASE}/{INDEX}",
        json={"description": None, "languageAnalyzer": "en.lucene"},
        headers=carol.authorization() | IF_MATCH,
    )

    assert response.status_code == 200
    changes = fake.calls[0]["changes"]
    assert changes.fields == frozenset({"description", "languageAnalyzer"})
    assert (changes.description, changes.language_analyzer) == (None, "en.lucene")
    assert fake.calls[0]["updated_by"] == carol.oid
    assert len(captured.events("admin_change")) == 1


async def test_an_unchanged_update_reports_no_change(
    started: ApiUnderTest,
    carol: SharedPrincipal,
    monkeypatch: pytest.MonkeyPatch,
    captured: Captured,
) -> None:
    patch(monkeypatch, "update_index", Fake(IndexUpdated(index=detail_row()), version=ROW_VERSION))

    response = await started.client.patch(
        f"{BASE}/{INDEX}", json={"displayName": "Policy"}, headers=carol.authorization() | IF_MATCH
    )

    assert response.json()["rowVersion"] == encode_row_version(ROW_VERSION)
    assert captured.events("admin_change") == []


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (IngestionRefusedError("index_busy"), 409, "index_busy"),
        (IndexSettingsRefusedError("unsupported_dimensions"), 422, "unsupported_dimensions"),
        (
            SqlUniqueViolation(2601, "23000", "UX_search_indexes_display_name"),
            409,
            "display_name_taken",
        ),
    ],
)
async def test_update_refusals_after_the_version_check(
    *,
    started: ApiUnderTest,
    carol: SharedPrincipal,
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
    status: int,
    code: str,
) -> None:
    patch(monkeypatch, "update_index", Fake(version=ROW_VERSION, error=error))

    response = await started.client.patch(
        f"{BASE}/{INDEX}", json={"displayName": "X"}, headers=carol.authorization() | IF_MATCH
    )

    assert (response.status_code, response.json()["code"]) == (status, code)


# --- adminDeleteIndex -----------------------------------------------------------------------


async def test_a_delete_answers_202_with_the_deleting_index(
    started: ApiUnderTest,
    carol: SharedPrincipal,
    monkeypatch: pytest.MonkeyPatch,
    captured: Captured,
) -> None:
    run = uuid4()
    fake = patch(
        monkeypatch,
        "delete_index",
        Fake(
            IndexDeleted(
                index=detail_row(IndexStatus.DELETING), run_id=run, item_count=2, events=()
            ),
            version=ROW_VERSION,
        ),
    )

    response = await started.client.delete(
        f"{BASE}/{INDEX}", headers=carol.authorization() | IF_MATCH
    )

    assert response.status_code == 202
    assert response.json()["index"]["status"] == "deleting"
    assert fake.calls[0]["requested_by"] == carol.oid
    assert queue_of(started).messages()[0].run_id == run
    (span,) = captured.finished_spans("docchat.ingestion.submit")
    assert span.attributes is not None
    assert span.attributes["docchat.ingestion.item_count"] == 2


@pytest.mark.parametrize("code", ["index_mapped", "index_busy"])
async def test_delete_state_rules(
    started: ApiUnderTest, carol: SharedPrincipal, monkeypatch: pytest.MonkeyPatch, code: Any
) -> None:
    patch(monkeypatch, "delete_index", Fake(version=ROW_VERSION, error=IngestionRefusedError(code)))

    response = await started.client.delete(
        f"{BASE}/{INDEX}", headers=carol.authorization() | IF_MATCH
    )

    assert (response.status_code, response.json()["code"]) == (409, code)
    assert queue_of(started).sent == []


async def test_a_stale_version_wins_over_a_mapping(
    started: ApiUnderTest, carol: SharedPrincipal, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch(
        monkeypatch,
        "delete_index",
        Fake(version=STALE, error=IngestionRefusedError("index_mapped")),
    )

    response = await started.client.delete(
        f"{BASE}/{INDEX}", headers=carol.authorization() | IF_MATCH
    )

    assert (response.status_code, response.json()["code"]) == (412, "precondition_failed")


# --- adminStartReingest ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "errors"),
    [
        ({}, [{"path": "body", "reason": "required"}]),
        ({"json": {"mode": "same_index"}}, [{"path": "body.mode", "reason": "unknown_field"}]),
    ],
)
async def test_the_reingest_body_is_exactly_an_empty_object(
    started: ApiUnderTest,
    carol: SharedPrincipal,
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, Any],
    errors: list[dict[str, str]],
) -> None:
    fake = patch(monkeypatch, "start_reingest", Fake())

    response = await started.client.post(
        f"{BASE}/{INDEX}/reingest", headers=carol.authorization() | IF_MATCH, **kwargs
    )

    assert response.status_code == 422
    assert response.json()["errors"] == errors
    assert fake.calls == []


async def test_a_rebuild_answers_its_mode_and_counts_the_create_item(
    started: ApiUnderTest,
    carol: SharedPrincipal,
    monkeypatch: pytest.MonkeyPatch,
    captured: Captured,
) -> None:
    run = uuid4()
    fake = patch(
        monkeypatch,
        "start_reingest",
        Fake(
            ReingestStarted(
                index=detail_row(IndexStatus.REBUILDING),
                run_id=run,
                mode=ReingestMode.NEW_PHYSICAL_INDEX,
                item_count=4,
                events=(),
            ),
            version=ROW_VERSION,
        ),
    )

    response = await started.client.post(
        f"{BASE}/{INDEX}/reingest", json={}, headers=carol.authorization() | IF_MATCH
    )

    assert response.status_code == 202
    assert response.json() | {"index": None} == {
        "index": None,
        "runId": str(run),
        "mode": "new_physical_index",
        "itemCount": 4,
    }
    assert fake.calls[0]["max_indexes"] == 15
    (span,) = captured.finished_spans("docchat.ingestion.submit")
    assert span.attributes is not None
    assert span.attributes["docchat.ingestion.run_kind"] == "index_rebuild"
    assert span.attributes["docchat.ingestion.item_count"] == 5


async def test_reingest_capacity_is_409(
    started: ApiUnderTest, carol: SharedPrincipal, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch(
        monkeypatch,
        "start_reingest",
        Fake(version=ROW_VERSION, error=IngestionRefusedError("search_index_capacity")),
    )

    response = await started.client.post(
        f"{BASE}/{INDEX}/reingest", json={}, headers=carol.authorization() | IF_MATCH
    )

    assert (response.status_code, response.json()["code"]) == (409, "search_index_capacity")
