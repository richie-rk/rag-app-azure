"""The path from an upload to the Flip, across the API and the ingestion ledger
(`14-ingestion.md` sections 2, 4, 5 and 21; `85-observability.md` section 3.7 item 4;
`84-testing.md` section 5.5): idempotency test 2 (T-167, a re-upload of identical bytes) and 9
(T-174, a delete followed by a re-upload of the same name), the event loop test (T-196), the
ingestion trace integration test (T-492) and the telemetry canary test (T-197).

The in-process API runs as the `docchat_api` role against the session's SQL Server database and
Azurite, as the upload integration tests run it; the ingestion steps run as the
`docchat_ingestion` role, the ledger transactions on synchronous connections in worker threads
and the activities through their entry points over a worker process built on the compose
services. The document delete is `adminDeleteDocument` itself, through the API. The steps after
Validate need the parse pool, the model and the search service, which these tests don't wire,
so the runs here end at Validate (T-196, T-197, T-492) or take the ledger's writes of a build
(T-174).
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import Connection, Engine, func, insert, select, update

from docchat_api.http.queue import ingestion_queue
from docchat_api.routes.admin_uploads import UploadStorage, upload_storage
from docchat_core.ingestion.methods import (
    PAGEWISE_V1,
    MethodRegistry,
    PagewiseParametersV1,
    compact_json,
)
from docchat_core.ingestion.models import IngestionQueueMessage, ItemEnvelope, ItemSettings
from docchat_core.models import SystemClock, encode_row_version
from docchat_core.opsconfig import INGESTION_AREAS, OpsConfigStore
from docchat_core.settings import LocalSettings
from docchat_core.sql.engine import SqlEngine
from docchat_core.sql.settings import SqlSettings
from docchat_core.sql.tables.access import project_indexes, projects
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
from docchat_ingestion import starters
from docchat_ingestion.activities import (
    chunk_document,
    claim_ingestion_items,
    extract_document,
    flip_chunk_set,
    index_chunks,
    load_dispatch_settings,
    record_item_outcome,
    sweep_ingestion_state,
    validate_item,
)
from docchat_ingestion.activities.documents import DocumentServices, PlanInput
from docchat_ingestion.activities.runtime import (
    ContainerPort,
    IngestionProcess,
    QueuePeekPort,
    SearchIndexPort,
)
from docchat_ingestion.isolation.pdf_schema import RENDITION_RULES_VERSION
from docchat_ingestion.isolation.runner import ParsePool
from docchat_ingestion.outcomes import ItemOutcome
from docchat_testing.builders import canary, project_index_row, project_row
from docchat_testing.builders.ingestion import physical_index_row, search_index_row
from docchat_testing.builders.rows import sql_now
from docchat_testing.fixtures.api import ApiUnderTest, ports_under_test, running_api
from docchat_testing.fixtures.identities import SharedPrincipal
from docchat_testing.fixtures.sql import SqlTestDatabase
from docchat_testing.fixtures.storage import POISON_QUEUE, StorageClients
from docchat_testing.telemetry_capture import TelemetryCapture
from docchat_testing.tokens import SigningKeys

OCTET = {"Content-Type": "application/octet-stream"}
BLOCK = 4_194_304
CONVERTER = "lo-4:7.4.7-1+deb12u14/0123456789ab"
PARAMETERS = PagewiseParametersV1()
SETTINGS = ItemSettings(
    activity_time_budget_seconds=1200,
    max_pages_per_document=2000,
    pagewise_pages_per_extract=40,
    max_parse_seconds_per_document=7200,
    embedding_batch_max_inputs=64,
    embedding_batch_max_tokens=60_000,
    embedding_concurrency=2,
    index_batch_max_documents=100,
    index_batch_max_bytes=8_000_000,
    table_summary_concurrency=4,
    table_summary_max_input_tokens=6000,
    table_summary_max_output_tokens=300,
)


def pdf(seed: str) -> bytes:
    return b"%PDF-1.7\n" + seed.encode() * 200 + b"\n%%EOF\n"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Engines:
    admin: Engine
    ingestion: Engine
    api: Engine


@pytest.fixture
def engines(sql_database: SqlTestDatabase) -> Iterator[Engines]:
    created = Engines(
        admin=sql_database.engine(),
        ingestion=sql_database.engine("docchat_ingestion"),
        api=sql_database.engine("docchat_api"),
    )
    yield created
    for engine in (created.admin, created.ingestion, created.api):
        engine.dispose()


async def in_thread[T](engine: Engine, work: Callable[[Connection], T]) -> T:
    """One transaction on a synchronous connection, in a worker thread."""

    def run() -> T:
        with engine.begin() as connection:
            return work(connection)

    return await asyncio.to_thread(run)


async def read[T](engine: Engine, work: Callable[[Connection], T]) -> T:
    def run() -> T:
        with engine.connect() as connection:
            return work(connection)

    return await asyncio.to_thread(run)


@dataclass(frozen=True)
class Index:
    id: UUID
    project_id: UUID
    active_id: UUID

    @property
    def base(self) -> str:
        return f"/v1/admin/indexes/{self.id}/uploads"


async def index_with_project(engines: Engines) -> Index:
    index = search_index_row(
        status="ready",
        method_parameters=compact_json(PARAMETERS.dumped()),
        method_parameter_hash=PAGEWISE_V1.parameter_hash(PARAMETERS),
    )
    physical = physical_index_row(index["id"], state="active", provisioned_at=sql_now())
    project = project_row()

    def arrange(connection: Connection) -> None:
        connection.execute(insert(search_indexes), index)
        connection.execute(insert(physical_indexes), physical)
        connection.execute(insert(projects), project)
        connection.execute(
            insert(project_indexes), project_index_row(project["id"], index["id"], is_default=True)
        )
        connection.execute(
            update(search_indexes)
            .where(search_indexes.c.id == index["id"])
            .values(active_physical_index_id=physical["id"])
        )

    await in_thread(engines.admin, arrange)
    return Index(index["id"], project["id"], physical["id"])


type Uploads = Callable[[], AbstractAsyncContextManager[ApiUnderTest]]


@pytest.fixture
async def uploads(
    api_sql_engine: SqlEngine, signing_keys: SigningKeys, storage: StorageClients
) -> AsyncIterator[Uploads]:
    documents_container = storage.container("documents")
    queue = storage.queue()

    @asynccontextmanager
    async def started() -> AsyncIterator[ApiUnderTest]:
        async with running_api(
            ports=ports_under_test(sql_engine=api_sql_engine), signing_keys=signing_keys
        ) as api:
            api.app.dependency_overrides[upload_storage] = lambda: UploadStorage(
                documents=cast("Any", documents_container), queue=cast("Any", queue)
            )
            api.app.dependency_overrides[ingestion_queue] = lambda: cast("Any", queue)
            yield api

    try:
        yield started
    finally:
        await api_sql_engine.dispose()


async def uploaded(
    api: ApiUnderTest,
    carol: SharedPrincipal,
    index: Index,
    data: bytes,
    name: str,
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Reserve, upload and submit one file, each request with `headers` beside the token; the
    reservation."""
    extra = headers or {}
    reservation = await api.client.post(
        index.base,
        json={
            "name": name,
            "sizeBytes": len(data),
            "sha256": sha(data),
            "projectId": str(index.project_id),
            "confirmUpdate": True,
        },
        headers=carol.authorization() | extra,
    )
    assert reservation.status_code == 201, reservation.text
    body: dict[str, Any] = reservation.json()
    content = await api.client.put(
        f"{index.base}/{body['versionId']}/content",
        content=data,
        headers=OCTET | carol.authorization() | extra,
    )
    assert content.status_code == 200, content.text
    submitted = await api.client.post(
        f"{index.base}/submit",
        json={"versionIds": [body["versionId"]]},
        headers=carol.authorization() | extra,
    )
    assert submitted.status_code == 202, submitted.text
    return body


def counts(connection: Connection) -> tuple[int, ...]:
    return tuple(
        int(connection.execute(select(func.count()).select_from(table)).scalar_one())
        for table in (documents, document_versions, ingestion_runs, ingestion_run_items)
    )


async def ingest_claimed(engines: Engines, version_id: UUID) -> UUID:
    """Claim the version's `ingest` item and run the ledger of a build to its Flip; the chunk
    set served."""
    claimed, _ = await in_thread(engines.ingestion, lambda c: claim_ingestion_items.claim(c, 8))
    item_ids = [item.item_id for item in claimed]
    rows = await read(
        engines.admin,
        lambda c: c.execute(
            select(ingestion_run_items).where(ingestion_run_items.c.id.in_(item_ids))
        ).all(),
    )
    (row,) = [row for row in rows if row.document_version_id == version_id]
    envelope = ItemEnvelope(
        item_id=row.id, claim_number=row.claim_count, run_id=row.run_id, settings=SETTINGS
    )
    decision = await in_thread(
        engines.ingestion,
        lambda c: validate_item.decide(c, envelope, converter_version=CONVERTER),
    )
    plan = decision.plan
    assert plan is not None
    await in_thread(
        engines.ingestion, lambda c: extract_document.record_pdf_sha(c, plan.chunk_set_id, "c" * 64)
    )
    await in_thread(
        engines.ingestion,
        lambda c: extract_document.record_page_count(c, plan.version_id, 2, converted=False),
    )
    await in_thread(
        engines.ingestion, lambda c: chunk_document.record_chunk_count(c, plan.chunk_set_id, 3)
    )
    assert await in_thread(
        engines.ingestion, lambda c: index_chunks.mark_ready(c, plan.chunk_set_id)
    )
    item = PlanInput(**envelope.model_dump(), plan=plan)
    flipped = await in_thread(engines.ingestion, lambda c: flip_chunk_set.decide(c, item))
    assert flipped.outcome == "flipped"
    await succeed(engines, row.id, row.claim_count)
    return plan.chunk_set_id


async def succeed(engines: Engines, item_id: UUID, claim_number: int) -> None:
    """Record a claimed item `succeeded`, as its sub-orchestration's last activity does: until
    then the item is running, and no other item of its document is claimed (`14-ingestion.md`
    section 5.6 item 3)."""
    outcome = ItemOutcome(item_id=item_id, claim_number=claim_number, state="succeeded")
    recorded = await in_thread(engines.ingestion, lambda c: record_item_outcome.record(c, outcome))
    assert recorded is not None


# T-167


async def test_t167_a_re_upload_of_identical_bytes_is_unchanged_and_ingests_nothing(
    uploads: Uploads, engines: Engines, carol: SharedPrincipal, storage: StorageClients
) -> None:
    index = await index_with_project(engines)
    data = pdf(uuid4().hex)
    async with uploads() as api:
        first = await uploaded(api, carol, index, data, "Policy.pdf")
        before = await read(engines.admin, counts)
        messages_before = len(await storage.queue().peek_messages(max_messages=32))

        preflight = await api.client.post(
            f"{index.base}/preflight",
            json={"files": [{"name": "Policy.pdf", "sizeBytes": len(data), "sha256": sha(data)}]},
            headers=carol.authorization(),
        )
        again = await api.client.post(
            index.base,
            json={
                "name": "Policy.pdf",
                "sizeBytes": len(data),
                "sha256": sha(data),
                "projectId": str(index.project_id),
                "confirmUpdate": True,
            },
            headers=carol.authorization(),
        )

    assert preflight.status_code == 200
    (result,) = preflight.json()["results"]
    assert (result["status"], result["documentId"]) == ("unchanged", first["documentId"])
    assert again.status_code == 409
    assert again.json()["code"] == "content_unchanged"
    assert await read(engines.admin, counts) == before
    assert len(await storage.queue().peek_messages(max_messages=32)) == messages_before


# T-174


async def test_t174_a_delete_then_a_re_upload_keeps_the_document_and_serves_the_new_version(
    uploads: Uploads, engines: Engines, carol: SharedPrincipal
) -> None:
    index = await index_with_project(engines)
    async with uploads() as api:
        first = await uploaded(api, carol, index, pdf(uuid4().hex), "Handbook.pdf")
        document_id = UUID(first["documentId"])
        old_set = await ingest_claimed(engines, UUID(first["versionId"]))
        row_version = await read(
            engines.admin,
            lambda c: c.execute(
                select(documents.c.row_version).where(documents.c.id == document_id)
            ).scalar_one(),
        )
        deleted = await api.client.delete(
            f"/v1/admin/indexes/{index.id}/documents/{document_id}",
            headers=carol.authorization()
            | {"If-Match": f'"{encode_row_version(bytes(row_version))}"'},
        )
        assert deleted.status_code == 202, deleted.text
        second = await uploaded(api, carol, index, pdf(uuid4().hex), "Handbook.pdf")
        # The delete's clean-up item went into the queue first at the same priority, so it is
        # the document's next claim, and the re-upload's ingest item waits for it to end
        # (`14-ingestion.md` section 5.6 items 1 and 3). Its deletes need the search service,
        # which this test doesn't wire, so only its ledger end is recorded.
        claimed, _ = await in_thread(engines.ingestion, lambda c: claim_ingestion_items.claim(c, 8))
        assert [item.operation for item in claimed] == ["cleanup_document"]
        await succeed(engines, claimed[0].item_id, claimed[0].claim_number)
        new_set = await ingest_claimed(engines, UUID(second["versionId"]))

    assert second["documentId"] == first["documentId"]
    document = await read(
        engines.admin,
        lambda c: c.execute(select(documents).where(documents.c.id == document_id)).one(),
    )
    assert document.is_deleted is False or document.is_deleted == 0
    assert document.current_version_id == UUID(second["versionId"])
    served = await read(
        engines.admin,
        lambda c: c.execute(
            select(served_chunk_sets).where(served_chunk_sets.c.document_id == document_id)
        ).all(),
    )
    assert [row.chunk_set_id for row in served] == [new_set]
    states = dict(
        await read(
            engines.admin,
            lambda c: c.execute(
                select(chunk_sets.c.id, chunk_sets.c.state).where(
                    chunk_sets.c.document_id == document_id
                )
            ).all(),
        )
    )
    assert states == {old_set: "retired", new_set: "served"}
    # The old chunks are deleted by the delete run's clean-up item, queued with the delete.
    cleanup = await read(
        engines.admin,
        lambda c: c.execute(
            select(ingestion_run_items).where(
                ingestion_run_items.c.document_id == document_id,
                ingestion_run_items.c.operation == "cleanup_document",
            )
        ).all(),
    )
    assert len(cleanup) == 1
    delete_run = await read(
        engines.admin,
        lambda c: c.execute(
            select(ingestion_runs.c.kind).where(ingestion_runs.c.id == cleanup[0].run_id)
        ).scalar_one(),
    )
    assert delete_run == "document_delete"
    assert deleted.json()["runId"] == str(cleanup[0].run_id)


# --- The worker process over the compose services -------------------------------------------------


class _Hub:
    """A Durable client with no dispatcher yet: the starter starts one and hints the run."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.events: list[tuple[str, Any]] = []

    async def get_status(self, instance_id: str, *options: bool) -> Any:
        return None

    async def start_new(
        self,
        orchestration_function_name: str,
        instance_id: str | None = None,
        client_input: Any = None,
    ) -> str:
        self.started.append(orchestration_function_name)
        return instance_id or ""

    async def raise_event(self, instance_id: str, event_name: str, event_data: Any = None) -> None:
        self.events.append((event_name, event_data))

    async def terminate(self, instance_id: str, reason: str) -> None:
        return None


class _NotInThisTest:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the test reached a service it doesn't arrange ({name})")


@pytest.fixture
async def ingestion_sql(sql_database: SqlTestDatabase) -> AsyncIterator[SqlEngine]:
    engine = SqlEngine(
        "ingestion",
        sql=SqlSettings(sql_threads=2),
        local=LocalSettings(
            local_emulators=True, local_sql_sa_password=SecretStr(sql_database.sa_password)
        ),
        credential=None,
        odbc_connection_string=sql_database.connection_string("docchat_ingestion"),
    )
    yield engine
    await engine.dispose()


@dataclass(frozen=True)
class Worker:
    process: Callable[[], Awaitable[IngestionProcess]]
    services: Callable[[], Awaitable[DocumentServices]]
    storage: StorageClients


@pytest.fixture
def worker(ingestion_sql: SqlEngine, storage: StorageClients) -> Worker:
    """The ingestion worker's resources over SQL Server and Azurite; the search service, the
    model and the parse pool aren't reached by the steps these tests run."""
    process = IngestionProcess(
        sql=ingestion_sql,
        ops=OpsConfigStore(ingestion_sql, INGESTION_AREAS, report_invalid=False),
        documents=cast(ContainerPort, storage.container("documents")),
        renditions=cast(ContainerPort, storage.container("renditions")),
        work=cast(ContainerPort, storage.container("ingestion-work")),
        poison_queue=cast(QueuePeekPort, storage.queue(POISON_QUEUE)),
        search_indexes=cast(SearchIndexPort, _NotInThisTest()),
        scanning_enabled=False,
        workload_principal_id=uuid4(),
        rendition_rules_version=RENDITION_RULES_VERSION,
    )
    services = DocumentServices(
        sql=ingestion_sql,
        documents=cast(Any, storage.container("documents")),
        renditions=cast(Any, storage.container("renditions")),
        work=cast(Any, storage.container("ingestion-work")),
        search=lambda name: cast(Any, _NotInThisTest()),
        openai=_NotInThisTest(),
        parse_pool=cast(ParsePool, _NotInThisTest()),
        methods=MethodRegistry(),
        clock=SystemClock(),
        rendition_rules_version=RENDITION_RULES_VERSION,
        converter_version=CONVERTER,
    )

    async def provide_process() -> IngestionProcess:
        return process

    async def provide_services() -> DocumentServices:
        return services

    return Worker(provide_process, provide_services, storage)


async def dispatch_once(worker: Worker) -> list[dict[str, Any]]:
    """One dispatcher wake through its activities: settings, sweep, claim; the claimed items."""
    loaded = await load_dispatch_settings.run({}, worker.process)
    assert loaded["ok"] is True, loaded
    swept = await sweep_ingestion_state.run(
        {"generation": 1, "settings": loaded["settings"]}, worker.process
    )
    assert swept["ok"] is True, swept
    claimed = await claim_ingestion_items.run({"maxParallel": 4, "generation": 1}, worker.process)
    assert claimed["ok"] is True, claimed
    items: list[dict[str, Any]] = claimed["items"]
    return items


async def validate_claimed(worker: Worker, item: dict[str, Any]) -> dict[str, Any]:
    envelope = {
        "itemId": item["itemId"],
        "claimNumber": item["claimNumber"],
        "runId": item["runId"],
        "traceparent": item.get("traceparent"),
        "tracestate": item.get("tracestate"),
        "settings": SETTINGS.model_dump(mode="json"),
    }
    output: dict[str, Any] = await validate_item.run(envelope, worker.services)
    return output


# T-196


async def test_t196_upload_handlers_and_ingestion_activities_never_block_the_loop(
    uploads: Uploads, engines: Engines, carol: SharedPrincipal, worker: Worker
) -> None:
    # Every test runs on the guarded loop, which fails it when a callback holds the loop for
    # more than 100 ms; this one drives a three-block upload and the dispatcher's wake.
    index = await index_with_project(engines)
    data = b"%PDF-1.7\n" + secrets.token_bytes(2 * BLOCK + 1000)
    async with uploads() as api:
        reservation = await uploaded(api, carol, index, data, "Large.pdf")

    (item,) = await dispatch_once(worker)
    validated = await validate_claimed(worker, item)

    assert validated["ok"] is True, validated
    # `ValidatePlan` is flat: the plan's fields beside the `ActivityResult` ones (section 6.2).
    assert (validated["action"], validated["versionId"]) == ("build", reservation["versionId"])


# T-492


async def test_t492_the_upload_request_trace_reaches_the_starter_and_the_run_s_activities(
    uploads: Uploads,
    engines: Engines,
    carol: SharedPrincipal,
    worker: Worker,
    telemetry_capture: TelemetryCapture,
) -> None:
    trace_id = secrets.token_hex(16)
    headers = {
        "traceparent": f"00-{trace_id}-{secrets.token_hex(8)}-01",
        "tracestate": "docchat=t492",
    }
    index = await index_with_project(engines)
    queue = worker.storage.queue()
    async for stale in queue.receive_messages(max_messages=32, visibility_timeout=1):
        await queue.delete_message(stale)
    async with uploads() as api:
        await uploaded(api, carol, index, pdf(uuid4().hex), "Traced.pdf", headers=headers)

    (message,) = [m async for m in queue.receive_messages(max_messages=32, visibility_timeout=5)]
    sent = IngestionQueueMessage.validate_wire(str(message.content))
    assert sent.traceparent.split("-")[1] == trace_id
    assert sent.tracestate == "docchat=t492"
    hub = _Hub()
    assert await starters.start_from_message(
        str(message.content).encode(), message.id, message.dequeue_count, hub
    )
    (item,) = await dispatch_once(worker)
    assert (await validate_claimed(worker, item))["ok"] is True

    def spans_named(name: str) -> list[Any]:
        return [span for span in telemetry_capture.spans() if span.name == name]

    def in_trace(span: Any) -> bool:
        return format(span.context.trace_id, "032x") == trace_id

    assert len([span for span in spans_named(starters.QUEUE_SPAN) if in_trace(span)]) == 1
    assert [span for span in spans_named("docchat.ingestion.activity") if in_trace(span)]
    for root in (sweep_ingestion_state.SPAN, claim_ingestion_items.SPAN):
        latest = spans_named(root)[-1]
        assert latest.parent is None
        assert not in_trace(latest)
    claim = spans_named(claim_ingestion_items.SPAN)[-1]
    assert trace_id in {format(link.context.trace_id, "032x") for link in claim.links}


# T-197


async def test_t197_a_canary_name_and_canary_text_reach_no_log_or_span(
    uploads: Uploads,
    engines: Engines,
    carol: SharedPrincipal,
    worker: Worker,
    telemetry_capture: TelemetryCapture,
) -> None:
    name = f"{canary('filename')}.pdf"
    text = canary("chunk")
    index = await index_with_project(engines)
    async with uploads() as api:
        await uploaded(api, carol, index, pdf(text), name)
    (item,) = await dispatch_once(worker)
    assert (await validate_claimed(worker, item))["ok"] is True

    leaking = [
        exported.label
        for exported in telemetry_capture.items()
        if any(name in value or text in value for _, value in exported.strings)
    ]
    assert leaking == []
