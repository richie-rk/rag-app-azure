"""`ingestion status`, `requeue-item`, `fail-item`, `cancel-run`, `abandon-rebuild`,
`set-deployments` and `drain-poison` (`86-operations.md` section 2.8.6; `14-ingestion.md` sections
6.11, 8.10 and 19; `80-data-model.md` sections 2.16 to 2.19 and 8.6).

A config operator recovers the ingestion ledger under the SQL role `docchat_config_operator`, which
holds no `DELETE` on any ingestion table and no queue-send right, so a run a command creates starts
at the dispatcher's next wake. Each changing command reads what it changes, validates the change,
confirms it, then applies it conditioned on the state it read (an item's state, claim count and
lease, a run's state, an index's row version, a physical index's deployment names) and inserts its
audit row in the same transaction, never retried; zero rows affected rolls the transaction back and
exits 6. `status` only reads. `drain-poison` deletes queue messages and records their count in its
own transaction. Reports hold IDs, states, counts and times only.

The helpers the `reindex` and `document` groups share with this one live here: the UUID argument,
the ledger values of `.azure/<env>/.env`, the operator session, the conditioned unit of work and
the mapping of a storage failure to its exit code.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, Final, Protocol
from uuid import UUID

from azure.core.exceptions import (
    AzureError,
    HttpResponseError,
    ServiceRequestError,
    ServiceResponseError,
)
from azure.storage.queue import ExponentialRetry
from azure.storage.queue.aio import QueueClient
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import and_, func, select, update

from docchat_core.audit import write_audit_event
from docchat_core.audit.models import (
    IngestionDeploymentsChangedMetadata,
    IngestionItemFailedMetadata,
    IngestionItemRequeuedMetadata,
    IngestionPoisonDrainedMetadata,
    IngestionRebuildAbandonedMetadata,
    IngestionRunCancelledMetadata,
)
from docchat_core.errors import ITEM_CODES
from docchat_core.ingestion.models import (
    IndexStatus,
    IngestionQueueMessage,
    ItemOperation,
    ItemState,
    PhysicalIndexState,
    RunPriority,
    RunState,
)
from docchat_core.ingestion.names import POISON_QUEUE
from docchat_core.ingestion.runs import (
    LOCK_ROW,
    FailureSideEffects,
    NewItem,
    apply_failure_side_effects,
    compute_index_status,
    create_maintenance_run,
)
from docchat_core.ingestion.settings import (
    DEPLOYMENT_NAME_PATTERN,
    EmbeddingModel,
    EmbeddingModelCatalog,
    UploadSettings,
)
from docchat_core.models import format_utc, parse_uuid
from docchat_core.settings import EndpointUrl, is_insecure_endpoint
from docchat_core.sql.engine import SqlDeadlock, SqlUniqueViolation
from docchat_core.sql.tables.ingestion import (
    ingestion_dispatcher_state,
    ingestion_run_items,
    ingestion_runs,
    physical_indexes,
    scan_canaries,
    search_indexes,
)
from docchat_core.sql.tables.schema import SYSUTCDATETIME, nvarchar_literal
from docchat_ops.framework.audit import connect, operator_event, write_event
from docchat_ops.framework.auth import STORAGE_SCOPE, acquire_token
from docchat_ops.framework.exitcodes import (
    AccessDenied,
    Conflict,
    Incomplete,
    NotFound,
    OpsError,
    Unreachable,
    UsageError,
    classify,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Sequence

    from azure.core.credentials_async import AsyncTokenCredential
    from pydantic import JsonValue
    from sqlalchemy import Connection

    from docchat_core.audit import AuditAction, AuditEvent, AuditMetadata, AuditTargetType
    from docchat_ops.cli import Run
    from docchat_ops.framework.audit import OpsDatabase
    from docchat_ops.framework.endpoints import AzdEnvironment

STATUS_UNIT: Final = "ingestion_status_read"
ITEM_READ_UNIT: Final = "ingestion_item_read"
REQUEUE_UNIT: Final = "ingestion_item_requeue"
FAIL_UNIT: Final = "ingestion_item_fail"
RUN_READ_UNIT: Final = "ingestion_run_read"
CANCEL_UNIT: Final = "ingestion_run_cancel"
REBUILD_READ_UNIT: Final = "ingestion_rebuild_read"
ABANDON_UNIT: Final = "ingestion_rebuild_abandon"
DEPLOYMENTS_READ_UNIT: Final = "ingestion_deployments_read"
DEPLOYMENTS_UNIT: Final = "ingestion_deployments_change"

INTERRUPTED: Final = "processing_interrupted"
POISON_BATCH: Final = 32
"""Messages per peek and per receive (`peek_messages(max_messages=32)`, `receive_messages(...)`)."""
CANARY_COUNT: Final = 5
DISPATCHER_ROW: Final = 1

CHANGED: Final = "the row changed since it was read; nothing was written"
RERUN: Final = "changed by someone else; read it and rerun the command"
NEXT_WAKE: Final = (
    "the run starts at the dispatcher's next wake: within 60 seconds while work is outstanding, "
    "within ingestion.idle_wake_minutes when idle"
)
UPLOAD_RUN_NOTE: Final = (
    "a pending version of a cancelled upload item returns through the sweep's orphaned-version "
    "duty unless its document is deleted"
)
ABANDON_CHANGE: Final = (
    "change: the physical index becomes retiring and is deleted by a delete_physical_index item "
    "in a maintenance run; the index's status is recomputed"
)

OPEN_RUN_COLUMNS: Final = (
    "runId",
    "kind",
    "indexId",
    "priority",
    "state",
    "createdAt",
    "startedAt",
)

MAX_INDEXES: Final = "DOCCHAT_SEARCH_MAX_INDEXES"
EMBEDDING_MODELS: Final = "DOCCHAT_OPENAI_EMBEDDING_MODELS"
QUEUE_ENDPOINT: Final = "DOCCHAT_STORAGE_QUEUE_ENDPOINT"

STORAGE_CONNECTION_TIMEOUT_SECONDS: Final = 10
STORAGE_READ_TIMEOUT_SECONDS: Final = 60
STORAGE_FIREWALL_CODE: Final = "AuthorizationFailure"
"""The error code Azure Storage answers with `403` when its network rules refuse the caller's
address; a missing data-plane role answers `AuthorizationPermissionMismatch` instead."""

_OPEN_RUN_STATES: Final = (RunState.QUEUED, RunState.RUNNING)
_DEPLOYMENT_RE: Final = re.compile(DEPLOYMENT_NAME_PATTERN)
_MAX_INDEXES_ADAPTER: Final[TypeAdapter[int]] = TypeAdapter(
    Annotated[int, UploadSettings.model_fields["search_max_indexes"]]
)
_CATALOG_ADAPTER: Final[TypeAdapter[tuple[EmbeddingModel, ...]]] = TypeAdapter(
    Annotated[
        tuple[EmbeddingModel, ...], EmbeddingModelCatalog.model_fields["openai_embedding_models"]
    ]
)
_ENDPOINT_ADAPTER: Final[TypeAdapter[str]] = TypeAdapter(EndpointUrl)


def _state(value: str) -> Any:
    # Closed-set values stay literals in the statement text, so SQL Server matches the filtered
    # index that covers them (`docchat_core.ingestion.runs` does the same).
    return nvarchar_literal(value)


# --- Shared helpers -------------------------------------------------------------------------------


def identifier(value: str) -> UUID:
    """A UUID argument (`--index-id`, `--item-id`, `--run-id`, `--version`, ...)."""
    try:
        return parse_uuid(value)
    except ValueError:
        raise argparse.ArgumentTypeError("an ID is a hyphenated UUID") from None


def deployment(value: str) -> str:
    """An Azure OpenAI deployment name: 2 to 64 letters, digits, `-`, `_` and `.`."""
    if not _DEPLOYMENT_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "a deployment name is 2 to 64 letters, digits, '-', '_' or '.'"
        )
    return value


def utc_text(value: object) -> str | None:
    """A `DATETIME2` value as ISO 8601 UTC text; the column holds UTC without a zone."""
    if not isinstance(value, datetime):
        return None
    return format_utc(value if value.tzinfo else value.replace(tzinfo=UTC))


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def age_seconds(then: object, now: object) -> int | None:
    """Whole seconds from `then` to the database's `now`; `None` when either is absent."""
    if not isinstance(then, datetime) or not isinstance(now, datetime):
        return None
    return max(0, int((_aware(now) - _aware(then)).total_seconds()))


def _refresh(environment: AzdEnvironment) -> str:
    return f"run azd env refresh -e {environment.environment}"


def max_indexes(environment: AzdEnvironment) -> int:
    """`DOCCHAT_SEARCH_MAX_INDEXES`, the index-capacity check's limit (exit 2 when malformed)."""
    try:
        return _MAX_INDEXES_ADAPTER.validate_python(environment.require(MAX_INDEXES))
    except ValidationError:
        raise UsageError(
            f"{MAX_INDEXES} in .azure/{environment.environment}/.env isn't 2 to 200",
            _refresh(environment),
        ) from None


def embedding_catalog(environment: AzdEnvironment) -> EmbeddingModelCatalog:
    """`DOCCHAT_OPENAI_EMBEDDING_MODELS`, validated by the apps' own field rules (exit 2
    otherwise). The settings class itself would also read the operator's shell, so the entries
    are validated with the field's rules and the catalogue is built from them."""
    text = environment.require(EMBEDDING_MODELS)
    try:
        entries = _CATALOG_ADAPTER.validate_python(json.loads(text))
        return EmbeddingModelCatalog.model_construct(openai_embedding_models=entries)
    except ValueError:
        raise UsageError(
            f"{EMBEDDING_MODELS} in .azure/{environment.environment}/.env isn't an embedding "
            "catalogue",
            _refresh(environment),
        ) from None


def storage_endpoint(environment: AzdEnvironment, name: str) -> str:
    """A storage endpoint of `.azure/<env>/.env`: an `https` URL of a remote host (exit 2)."""
    value = environment.require(name)
    try:
        _ENDPOINT_ADAPTER.validate_python(value)
    except ValidationError:
        valid = False
    else:
        valid = not is_insecure_endpoint(value)
    if not valid:
        raise UsageError(
            f"{name} in .azure/{environment.environment}/.env isn't an https endpoint",
            _refresh(environment),
        )
    return value


def storage_failure(error: AzureError, store: str, level: str) -> OpsError:
    """The exit an Azure Storage failure maps to; the SDK's message isn't repeated."""
    if isinstance(error, (ServiceRequestError, ServiceResponseError)):
        return Unreachable(
            f"{store} didn't answer", "check the network path and the allowlist, then run again"
        )
    if isinstance(error, HttpResponseError):
        status = error.status_code or 0
        if status == 403 and getattr(error, "error_code", None) == STORAGE_FIREWALL_CODE:  # noqa: PLR2004
            return Unreachable(
                f"the {store} firewall refused this address",
                "run from an address in the environment's operator allowlist",
            )
        if status in {401, 403}:
            return AccessDenied(
                f"{store} answered {status}",
                f"check the just-in-time level {level}, then az logout and az login",
            )
    return classify(error)


class RowChangedError(Exception):
    """A conditioned write matched no row: the transaction rolls back and the command exits 6."""


async def apply_change[T](database: OpsDatabase, work: Callable[[Connection], T], name: str) -> T:
    """One transaction, never retried, because a run or audit row must not be written twice; a
    write that matched no row, a unique violation or a deadlock leaves nothing written (exit 6)."""
    try:
        return await database.run(work, name=name, retry="none")
    except (RowChangedError, SqlUniqueViolation, SqlDeadlock):
        raise Conflict(CHANGED, RERUN) from None


@dataclass(frozen=True)
class Session:
    """An operator's sign-in for one changing command: the credential, the SQL layer and the
    actor of the audit rows (the `oid` of the SQL access token)."""

    credential: AsyncTokenCredential
    database: OpsDatabase
    actor: str


@asynccontextmanager
async def operator_session(run: Run, environment: AzdEnvironment) -> AsyncIterator[Session]:
    """Refuse a service principal where section 2.2 says so (exit 3), sign in to SQL and yield the
    session; the SQL layer and the credential are closed on the way out."""
    target = environment.sql_target()
    await run.refuse_service_principal()
    async with run.toolkit.credential() as credential:
        database = run.toolkit.database(target, credential)
        try:
            actor = await connect(database)
            yield Session(credential=credential, database=database, actor=actor)
        finally:
            await database.dispose()


def _event(
    session: Session,
    *,
    action: AuditAction,
    target_type: AuditTargetType,
    target_id: str,
    metadata: AuditMetadata,
) -> AuditEvent:
    return operator_event(
        action=action,
        actor_object_id=session.actor,
        target_type=target_type,
        target_id=target_id,
        metadata=metadata,
    )


def _ticket(run: Run) -> str:
    return run.ticket or ""


# --- status ---------------------------------------------------------------------------------------


def status_arguments(parser: argparse.ArgumentParser) -> None:
    """`ingestion status` takes no argument beyond the global options."""
    del parser


def _priority_counts(rows: Sequence[Any]) -> dict[str, JsonValue]:
    counts: dict[str, JsonValue] = {priority.label: 0 for priority in RunPriority}
    for row in rows:
        counts[RunPriority(int(row.priority)).label] = int(row.items)
    return counts


def _status(connection: Connection) -> dict[str, JsonValue]:
    now = connection.execute(select(SYSUTCDATETIME.label("now"))).scalar_one()
    dispatcher = connection.execute(
        select(
            ingestion_dispatcher_state.c.generation,
            ingestion_dispatcher_state.c.last_sweep_at,
            ingestion_dispatcher_state.c.last_claim_at,
            ingestion_dispatcher_state.c.running_items,
            ingestion_dispatcher_state.c.updated_at,
        ).where(ingestion_dispatcher_state.c.id == DISPATCHER_ROW)
    ).first()
    items = ingestion_run_items.c
    queued = connection.execute(
        select(
            items.priority, func.count().label("items"), func.min(items.created_at).label("oldest")
        )
        .where(items.state == _state(ItemState.QUEUED))
        .group_by(items.priority)
    ).all()
    running = connection.execute(
        select(items.priority, func.count().label("items"))
        .where(items.state == _state(ItemState.RUNNING))
        .group_by(items.priority)
    ).all()
    runs = ingestion_runs.c
    open_runs = connection.execute(
        select(
            runs.id,
            runs.kind,
            runs.search_index_id,
            runs.priority,
            runs.state,
            runs.created_at,
            runs.started_at,
        )
        .where(runs.state.in_([_state(state) for state in _OPEN_RUN_STATES]))
        .order_by(runs.created_at, runs.id)
    ).all()
    canaries = connection.execute(
        select(
            scan_canaries.c.id,
            scan_canaries.c.written_at,
            scan_canaries.c.result,
            scan_canaries.c.scanned_at,
            scan_canaries.c.minutes_to_result,
        )
        .order_by(scan_canaries.c.written_at.desc())
        .limit(CANARY_COUNT)
    ).all()
    oldest = min((row.oldest for row in queued if row.oldest is not None), default=None)
    return {
        "dispatcher": None
        if dispatcher is None
        else {
            "generation": int(dispatcher.generation),
            "lastSweepAt": utc_text(dispatcher.last_sweep_at),
            "lastClaimAt": utc_text(dispatcher.last_claim_at),
            "runningItems": int(dispatcher.running_items),
            "updatedAt": utc_text(dispatcher.updated_at),
            "heartbeatAgeSeconds": age_seconds(dispatcher.last_sweep_at, now),
        },
        "queued": _priority_counts(queued),
        "running": _priority_counts(running),
        "oldestQueuedAgeSeconds": age_seconds(oldest, now),
        "openRuns": [
            {
                "runId": str(row.id).lower(),
                "kind": str(row.kind),
                "indexId": str(row.search_index_id).lower(),
                "priority": RunPriority(int(row.priority)).label,
                "state": str(row.state),
                "createdAt": utc_text(row.created_at),
                "startedAt": utc_text(row.started_at),
            }
            for row in open_runs
        ],
        "canaries": [
            {
                "canaryId": str(row.id).lower(),
                "writtenAt": utc_text(row.written_at),
                "result": row.result,
                "scannedAt": utc_text(row.scanned_at),
                "minutesToResult": row.minutes_to_result,
            }
            for row in canaries
        ],
    }


async def status(run: Run, args: argparse.Namespace) -> None:
    """Read-only: no confirmation, no audit row, and a service principal is accepted."""
    del args
    target = run.azd().sql_target()
    async with run.toolkit.credential() as credential:
        database = run.toolkit.database(target, credential)
        try:
            snapshot = await database.run(_status, name=STATUS_UNIT, retry="transient")
        finally:
            await database.dispose()
    run.report("listed", snapshot, rows="openRuns", columns=OPEN_RUN_COLUMNS)


# --- requeue-item and fail-item ---------------------------------------------------------------


def item_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--item-id", type=identifier, required=True, help="the run item's ID")


@dataclass(frozen=True)
class _Item:
    item_id: UUID
    run_id: UUID
    state: ItemState
    claim_count: int
    lease_expires_at: datetime | None
    now: datetime

    @property
    def lease_expired(self) -> bool:
        return self.lease_expires_at is not None and _aware(self.lease_expires_at) < _aware(
            self.now
        )

    def lease_line(self) -> str:
        return f"lease expires at: {utc_text(self.lease_expires_at) or '-'}"


async def _read_item(database: OpsDatabase, item_id: UUID) -> _Item:
    items = ingestion_run_items.c
    statement = select(
        items.id,
        items.run_id,
        items.state,
        items.claim_count,
        items.lease_expires_at,
        SYSUTCDATETIME.label("now"),
    ).where(items.id == item_id)

    def read(connection: Connection) -> Any:
        return connection.execute(statement).first()

    row = await database.run(read, name=ITEM_READ_UNIT, retry="transient")
    if row is None:
        raise NotFound(f"no run item {item_id}", "check the item ID in the ingestion workbook")
    return _Item(
        item_id=item_id,
        run_id=UUID(str(row.run_id)),
        state=ItemState(row.state),
        claim_count=int(row.claim_count),
        lease_expires_at=row.lease_expires_at,
        now=row.now,
    )


def _valid_lease(item: _Item) -> UsageError:
    return UsageError(
        f"the item's lease is valid until {utc_text(item.lease_expires_at)}; nothing was written",
        "wait for the lease to expire; the sweep requeues an expired lease by itself",
    )


async def requeue_item(run: Run, args: argparse.Namespace) -> None:
    item_id: UUID = args.item_id
    environment = run.azd()
    async with operator_session(run, environment) as session:
        item = await _read_item(session.database, item_id)
        if item.state is not ItemState.RUNNING:
            raise UsageError(
                f"the item is {item.state}, not running; nothing was written",
                "requeue only a running item whose lease has expired",
            )
        if not item.lease_expired:
            raise _valid_lease(item)
        await run.confirm(
            [
                f"item: {item_id} (run {item.run_id})",
                f"state: running, claim count {item.claim_count}",
                item.lease_line(),
                "change: running to queued with no lease; the claim count stays",
            ]
        )
        event = _event(
            session,
            action="ingestion.item_requeued",
            target_type="ingestion_run_item",
            target_id=str(item_id),
            metadata=IngestionItemRequeuedMetadata(
                run_id=item.run_id, claim_count=item.claim_count, ticket=_ticket(run)
            ),
        )
        items = ingestion_run_items.c
        # `claim_count` stays, so the writes of the expired claim stay fenced.
        statement = (
            update(ingestion_run_items)
            .where(
                items.id == item_id,
                items.state == _state(ItemState.RUNNING),
                items.claim_count == item.claim_count,
                items.lease_expires_at < SYSUTCDATETIME,
            )
            .values(state=_state(ItemState.QUEUED), lease_expires_at=None)
        )

        def work(connection: Connection) -> None:
            if connection.execute(statement).rowcount != 1:
                raise RowChangedError
            write_audit_event(connection, event)

        await apply_change(session.database, work, REQUEUE_UNIT)
    run.report(
        "requeued",
        {
            "itemId": str(item_id),
            "runId": str(item.run_id),
            "state": ItemState.QUEUED.value,
            "claimCount": item.claim_count,
        },
    )


async def fail_item(run: Run, args: argparse.Namespace) -> None:
    item_id: UUID = args.item_id
    environment = run.azd()
    async with operator_session(run, environment) as session:
        item = await _read_item(session.database, item_id)
        if item.state is ItemState.RUNNING and not item.lease_expired:
            raise _valid_lease(item)
        if item.state not in {ItemState.QUEUED, ItemState.RUNNING}:
            raise UsageError(
                f"the item is already {item.state}; nothing was written",
                "fail only a queued item or a running item whose lease has expired",
            )
        await run.confirm(
            [
                f"item: {item_id} (run {item.run_id})",
                f"state: {item.state}, claim count {item.claim_count}",
                item.lease_line(),
                f"change: failed with {INTERRUPTED}, with every failure side effect",
            ]
        )
        event = _event(
            session,
            action="ingestion.item_failed",
            target_type="ingestion_run_item",
            target_id=str(item_id),
            metadata=IngestionItemFailedMetadata(
                run_id=item.run_id, outcome_code=INTERRUPTED, ticket=_ticket(run)
            ),
        )
        items = ingestion_run_items.c
        condition = (
            items.state == _state(ItemState.QUEUED)
            if item.state is ItemState.QUEUED
            else and_(
                items.state == _state(ItemState.RUNNING), items.lease_expires_at < SYSUTCDATETIME
            )
        )
        statement = (
            update(ingestion_run_items)
            .where(items.id == item_id, items.claim_count == item.claim_count, condition)
            .values(
                state=_state(ItemState.FAILED),
                outcome_code=INTERRUPTED,
                outcome_message=ITEM_CODES[INTERRUPTED].message,
                lease_expires_at=None,
                completed_at=SYSUTCDATETIME,
            )
        )

        def work(connection: Connection) -> FailureSideEffects:
            if connection.execute(statement).rowcount != 1:
                raise RowChangedError
            effects = apply_failure_side_effects(connection, item_id, INTERRUPTED)
            write_audit_event(connection, event)
            return effects

        effects = await apply_change(session.database, work, FAIL_UNIT)
    run.report(
        "failed",
        {
            "itemId": str(item_id),
            "runId": str(item.run_id),
            "outcomeCode": INTERRUPTED,
            "chunkSetRetired": effects.chunk_set_retired,
            "versionFailed": effects.version_failed,
        },
    )


# --- cancel-run -----------------------------------------------------------------------------------


def cancel_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-id", type=identifier, required=True, help="the ingestion run's ID")


@dataclass(frozen=True)
class _Run:
    kind: str
    index_id: UUID
    state: RunState
    queued_items: int


async def _read_run(database: OpsDatabase, run_id: UUID) -> _Run:
    runs = ingestion_runs.c
    items = ingestion_run_items.c

    def read(connection: Connection) -> _Run | None:
        row = connection.execute(
            select(runs.kind, runs.search_index_id, runs.state).where(runs.id == run_id)
        ).first()
        if row is None:
            return None
        queued = connection.execute(
            select(func.count())
            .select_from(ingestion_run_items)
            .where(items.run_id == run_id, items.state == _state(ItemState.QUEUED))
        ).scalar_one()
        return _Run(
            kind=str(row.kind),
            index_id=UUID(str(row.search_index_id)),
            state=RunState(row.state),
            queued_items=int(queued),
        )

    found = await database.run(read, name=RUN_READ_UNIT, retry="transient")
    if found is None:
        raise NotFound(f"no ingestion run {run_id}", "check the run ID in the ingestion workbook")
    return found


async def cancel_run(run: Run, args: argparse.Namespace) -> None:
    run_id: UUID = args.run_id
    environment = run.azd()
    async with operator_session(run, environment) as session:
        found = await _read_run(session.database, run_id)
        if found.state not in _OPEN_RUN_STATES:
            raise UsageError(
                f"the run is already {found.state}; nothing was written",
                "cancel only a queued or running run",
            )
        await run.confirm(
            [
                f"run: {run_id} (kind {found.kind}, index {found.index_id}, state {found.state})",
                f"queued items to cancel: {found.queued_items}; running items finish",
            ]
        )
        runs = ingestion_runs.c
        items = ingestion_run_items.c
        locked = (
            select(runs.state).with_hint(ingestion_runs, LOCK_ROW, "mssql").where(runs.id == run_id)
        )
        cancel = (
            update(ingestion_run_items)
            .where(items.run_id == run_id, items.state == _state(ItemState.QUEUED))
            .values(state=_state(ItemState.CANCELLED), completed_at=SYSUTCDATETIME)
        )

        def work(connection: Connection) -> int:
            if connection.execute(locked).scalar_one_or_none() != found.state:
                raise RowChangedError
            # A cancelled item keeps a null outcome code (`80-data-model.md` section 8.6).
            cancelled = int(connection.execute(cancel).rowcount)
            write_audit_event(
                connection,
                _event(
                    session,
                    action="ingestion.run_cancelled",
                    target_type="ingestion_run",
                    target_id=str(run_id),
                    metadata=IngestionRunCancelledMetadata(
                        index_id=found.index_id,
                        cancelled_item_count=cancelled,
                        ticket=_ticket(run),
                    ),
                ),
            )
            return cancelled

        cancelled = await apply_change(session.database, work, CANCEL_UNIT)
    if found.kind == "upload":
        run.progress(UPLOAD_RUN_NOTE)
    run.report(
        "cancelled",
        {"runId": str(run_id), "indexId": str(found.index_id), "cancelledItemCount": cancelled},
    )


# --- abandon-rebuild ------------------------------------------------------------------------------


def index_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--index-id", type=identifier, required=True, help="the logical index's ID")


@dataclass(frozen=True)
class _Rebuild:
    status: IndexStatus
    row_version: bytes
    physical_index_id: UUID
    generation: int
    queued_items: int


async def _read_rebuild(database: OpsDatabase, index_id: UUID) -> _Rebuild:
    def read(connection: Connection) -> tuple[Any, Any, int]:
        index = connection.execute(
            select(search_indexes.c.status, search_indexes.c.row_version).where(
                search_indexes.c.id == index_id
            )
        ).first()
        if index is None or index.status == IndexStatus.DELETED:
            return None, None, 0
        building = connection.execute(
            select(physical_indexes.c.id, physical_indexes.c.generation).where(
                physical_indexes.c.search_index_id == index_id,
                physical_indexes.c.state == _state(PhysicalIndexState.BUILDING),
            )
        ).first()
        if building is None:
            return index, None, 0
        queued = connection.execute(
            select(func.count())
            .select_from(ingestion_run_items)
            .where(
                ingestion_run_items.c.physical_index_id == building.id,
                ingestion_run_items.c.state == _state(ItemState.QUEUED),
            )
        ).scalar_one()
        return index, building, int(queued)

    index, building, queued = await database.run(read, name=REBUILD_READ_UNIT, retry="transient")
    if index is None:
        raise NotFound(
            f"no live logical index {index_id}",
            "check the index ID",
            code="index_not_found",
        )
    if index.status == IndexStatus.DELETING:
        raise UsageError(
            "the index is being deleted; its index_delete run removes every physical index",
            "let the deletion finish",
            code="index_busy",
        )
    if building is None:
        raise UsageError(
            "the index has no building physical index; nothing was written",
            "check the index's rebuild in the admin Indexes screen",
        )
    return _Rebuild(
        status=IndexStatus(index.status),
        row_version=bytes(index.row_version),
        physical_index_id=UUID(str(building.id)),
        generation=int(building.generation),
        queued_items=queued,
    )


@dataclass(frozen=True)
class _Abandoned:
    cancelled: int
    run_id: UUID
    status: IndexStatus


async def abandon_rebuild(run: Run, args: argparse.Namespace) -> None:
    index_id: UUID = args.index_id
    environment = run.azd()
    async with operator_session(run, environment) as session:
        rebuild = await _read_rebuild(session.database, index_id)
        physical_index_id = rebuild.physical_index_id
        await run.confirm(
            [
                f"index: {index_id} (status {rebuild.status})",
                f"building physical index: {physical_index_id} (generation {rebuild.generation})",
                f"queued items to cancel: {rebuild.queued_items}",
                ABANDON_CHANGE,
            ]
        )
        actor = UUID(session.actor)
        indexes = search_indexes.c
        items = ingestion_run_items.c
        locked = (
            select(indexes.row_version)
            .with_hint(search_indexes, LOCK_ROW, "mssql")
            .where(indexes.id == index_id)
        )
        retire = (
            update(physical_indexes)
            .where(
                physical_indexes.c.id == physical_index_id,
                physical_indexes.c.state == _state(PhysicalIndexState.BUILDING),
            )
            .values(
                state=_state(PhysicalIndexState.RETIRING),
                retiring_at=SYSUTCDATETIME,
                delete_after=SYSUTCDATETIME,
                build_run_id=None,
            )
        )
        cancel = (
            update(ingestion_run_items)
            .where(
                items.physical_index_id == physical_index_id,
                items.state == _state(ItemState.QUEUED),
            )
            .values(state=_state(ItemState.CANCELLED), completed_at=SYSUTCDATETIME)
        )

        def work(connection: Connection) -> _Abandoned:
            current = connection.execute(locked).scalar_one_or_none()
            if current is None or bytes(current) != rebuild.row_version:
                raise RowChangedError
            if connection.execute(retire).rowcount != 1:
                raise RowChangedError
            cancelled = int(connection.execute(cancel).rowcount)
            created = create_maintenance_run(
                connection,
                index_id=index_id,
                items=[
                    NewItem(
                        ItemOperation.DELETE_PHYSICAL_INDEX, physical_index_id=physical_index_id
                    )
                ],
            )
            new_status = compute_index_status(connection, index_id)
            changed = connection.execute(
                update(search_indexes)
                .where(indexes.id == index_id, indexes.row_version == rebuild.row_version)
                .values(
                    status=new_status.value,
                    status_changed_at=SYSUTCDATETIME,
                    updated_at=SYSUTCDATETIME,
                    updated_by=actor,
                )
            ).rowcount
            if changed != 1:
                raise RowChangedError
            write_audit_event(
                connection,
                _event(
                    session,
                    action="ingestion.rebuild_abandoned",
                    target_type="physical_index",
                    target_id=str(physical_index_id),
                    metadata=IngestionRebuildAbandonedMetadata(
                        index_id=index_id,
                        generation=rebuild.generation,
                        cancelled_item_count=cancelled,
                        run_id=created.run_id,
                        ticket=_ticket(run),
                    ),
                ),
            )
            return _Abandoned(cancelled=cancelled, run_id=created.run_id, status=new_status)

        abandoned = await apply_change(session.database, work, ABANDON_UNIT)
    run.progress(NEXT_WAKE)
    run.report(
        "abandoned",
        {
            "indexId": str(index_id),
            "physicalIndexId": str(physical_index_id),
            "generation": rebuild.generation,
            "cancelledItemCount": abandoned.cancelled,
            "runId": str(abandoned.run_id),
            "status": abandoned.status.value,
        },
    )


# --- set-deployments ------------------------------------------------------------------------------


def set_deployments_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--physical-index-id", type=identifier, required=True, help="the physical index's ID"
    )
    parser.add_argument(
        "--query", type=deployment, required=True, help="the query embedding deployment's name"
    )
    parser.add_argument(
        "--ingestion",
        type=deployment,
        required=True,
        help="the ingestion embedding deployment's name",
    )


async def _read_physical_index(database: OpsDatabase, physical_index_id: UUID) -> Any:
    pi = physical_indexes.c
    statement = select(
        pi.search_index_id,
        pi.generation,
        pi.embedding_model,
        pi.embedding_model_version,
        pi.query_embedding_deployment,
        pi.ingestion_embedding_deployment,
        pi.state,
    ).where(pi.id == physical_index_id)

    def read(connection: Connection) -> Any:
        return connection.execute(statement).first()

    row = await database.run(read, name=DEPLOYMENTS_READ_UNIT, retry="transient")
    if row is None or row.state == PhysicalIndexState.DELETED:
        raise NotFound(
            f"no physical index {physical_index_id} that isn't deleted",
            "check the physical index ID",
        )
    return row


async def set_deployments(run: Run, args: argparse.Namespace) -> None:
    physical_index_id: UUID = args.physical_index_id
    query: str = args.query
    ingestion: str = args.ingestion
    environment = run.azd()
    catalog = embedding_catalog(environment)
    async with operator_session(run, environment) as session:
        row = await _read_physical_index(session.database, physical_index_id)
        entry = catalog.for_deployments(query, ingestion)
        if entry is None:
            raise UsageError(
                f"{EMBEDDING_MODELS} has no entry with these query and ingestion deployments",
                "name a deployment pair of the catalogue, then run azd env refresh",
            )
        if (entry.model, entry.model_version) != (
            row.embedding_model,
            row.embedding_model_version,
        ):
            raise UsageError(
                "the deployments serve another model or version than the physical index's; "
                "nothing was written",
                "a different model or version needs a rebuild (reindex start)",
            )
        previous_query = str(row.query_embedding_deployment)
        previous_ingestion = str(row.ingestion_embedding_deployment)
        index_id = UUID(str(row.search_index_id))
        generation = int(row.generation)
        fields: dict[str, JsonValue] = {
            "physicalIndexId": str(physical_index_id),
            "indexId": str(index_id),
            "generation": generation,
            "previousQueryDeployment": previous_query,
            "queryDeployment": query,
            "previousIngestionDeployment": previous_ingestion,
            "ingestionDeployment": ingestion,
        }
        if (previous_query, previous_ingestion) == (query, ingestion):
            run.report("unchanged", fields)
            return
        event = _event(
            session,
            action="ingestion.deployments_changed",
            target_type="physical_index",
            target_id=str(physical_index_id),
            metadata=IngestionDeploymentsChangedMetadata(
                index_id=index_id,
                generation=generation,
                previous_query_deployment=previous_query,
                query_deployment=query,
                previous_ingestion_deployment=previous_ingestion,
                ingestion_deployment=ingestion,
                ticket=_ticket(run),
            ),
        )
        await run.confirm(
            [
                f"physical index: {physical_index_id} (index {index_id}, generation {generation})",
                f"model: {row.embedding_model} version {row.embedding_model_version}",
                f"query deployment: {previous_query} -> {query}",
                f"ingestion deployment: {previous_ingestion} -> {ingestion}",
            ]
        )
        pi = physical_indexes.c
        statement = (
            update(physical_indexes)
            .where(
                pi.id == physical_index_id,
                pi.state != _state(PhysicalIndexState.DELETED),
                pi.query_embedding_deployment == previous_query,
                pi.ingestion_embedding_deployment == previous_ingestion,
            )
            .values(query_embedding_deployment=query, ingestion_embedding_deployment=ingestion)
        )

        def work(connection: Connection) -> None:
            if connection.execute(statement).rowcount != 1:
                raise RowChangedError
            write_audit_event(connection, event)

        await apply_change(session.database, work, DEPLOYMENTS_UNIT)
    run.report("changed", fields)


# --- drain-poison ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class PoisonMessage:
    """A message of `ingestion-requests-poison` as the queue returned it: base64 text."""

    message_id: str
    pop_receipt: str | None
    content: str


class PoisonQueue(Protocol):
    """The poison queue as `drain-poison` uses it."""

    async def peek(self, count: int) -> list[PoisonMessage]: ...

    async def receive(self, count: int) -> list[PoisonMessage]: ...

    async def delete(self, message: PoisonMessage) -> None: ...

    async def close(self) -> None: ...


class StoragePoisonQueue:
    """`azure.storage.queue.aio.QueueClient` for `ingestion-requests-poison`."""

    def __init__(self, client: QueueClient) -> None:
        self._client = client

    async def peek(self, count: int) -> list[PoisonMessage]:
        # A peek changes nothing: no message becomes invisible and none is dequeued.
        return [
            PoisonMessage(str(message.id), None, _content(message.content))
            for message in await self._client.peek_messages(max_messages=count)
        ]

    async def receive(self, count: int) -> list[PoisonMessage]:
        return [
            PoisonMessage(str(message.id), message.pop_receipt, _content(message.content))
            async for message in self._client.receive_messages(max_messages=count)
        ]

    async def delete(self, message: PoisonMessage) -> None:
        await self._client.delete_message(message.message_id, message.pop_receipt)

    async def close(self) -> None:
        await self._client.close()


def _content(value: object) -> str:
    return value if isinstance(value, str) else ""


def storage_poison_queue(endpoint: str, credential: AsyncTokenCredential) -> PoisonQueue:
    """The poison queue with the platform's storage client options; messages are read as their
    raw base64 text, so a message that isn't valid base64 can still be drained."""
    return StoragePoisonQueue(
        QueueClient(
            endpoint,
            POISON_QUEUE,
            credential=credential,
            connection_timeout=STORAGE_CONNECTION_TIMEOUT_SECONDS,
            read_timeout=STORAGE_READ_TIMEOUT_SECONDS,
            retry_policy=ExponentialRetry(
                initial_backoff=1, increment_base=2, retry_total=3, random_jitter_range=1
            ),
        )
    )


def run_id_of(content: str) -> str | None:
    """The run ID a poison message names, or `None` when it isn't an `IngestionQueueMessage`."""
    try:
        text = base64.b64decode(content, validate=True).decode("utf-8")
        return str(IngestionQueueMessage.validate_wire(text).run_id)
    except ValueError:
        return None


def drain_poison_arguments(parser: argparse.ArgumentParser) -> None:
    """`ingestion drain-poison` takes no argument beyond the global options."""
    del parser


def _peek_summary(peeked: Sequence[PoisonMessage]) -> list[str]:
    named = [run_id_of(message.content) for message in peeked]
    run_ids = sorted({run_id for run_id in named if run_id is not None})
    count = f"at least {POISON_BATCH}" if len(peeked) >= POISON_BATCH else str(len(peeked))
    lines = [
        f"queue: {POISON_QUEUE}",
        f"messages: {count}",
        f"runs named: {', '.join(run_ids) if run_ids else 'none'}",
    ]
    unreadable = named.count(None)
    if unreadable:
        lines.append(f"messages naming no run: {unreadable}")
    lines.append(
        "check with ingestion status that these runs have their items queued or finished; "
        "then every message is received and deleted until a receive returns none"
    )
    return lines


async def _drain(queue: PoisonQueue) -> tuple[int, AzureError | None]:
    deleted = 0
    try:
        while batch := await queue.receive(POISON_BATCH):
            for message in batch:
                await queue.delete(message)
                deleted += 1
    except AzureError as error:
        return deleted, error
    return deleted, None


async def drain_poison(run: Run, args: argparse.Namespace) -> None:
    del args
    environment = run.azd()
    endpoint = storage_endpoint(environment, QUEUE_ENDPOINT)
    async with operator_session(run, environment) as session:
        await acquire_token(session.credential, STORAGE_SCOPE, "Queue Storage")
        queue = run.toolkit.poison_queue(endpoint, session.credential)
        try:
            try:
                peeked = await queue.peek(POISON_BATCH)
            except AzureError as error:
                raise storage_failure(error, "Queue Storage", "config") from None
            await run.confirm(_peek_summary(peeked))
            deleted, failure = await _drain(queue)
        finally:
            await queue.close()
        if failure is not None and deleted == 0:
            raise storage_failure(failure, "Queue Storage", "config") from None
        if failure is not None:
            run.progress(f"drained {deleted} messages before a receive or delete failed")
        await write_event(
            session.database,
            _event(
                session,
                action="ingestion.poison_drained",
                target_type="ingestion_queue",
                target_id=POISON_QUEUE,
                metadata=IngestionPoisonDrainedMetadata(deleted_count=deleted, ticket=_ticket(run)),
            ),
        )
    if failure is not None:
        raise Incomplete(
            f"a receive or delete failed after {deleted} messages were drained",
            "run the same command once more to drain the rest",
        )
    run.report("drained", {"queue": POISON_QUEUE, "deletedCount": deleted})
