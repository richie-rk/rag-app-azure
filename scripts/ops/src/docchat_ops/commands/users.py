"""`user read-conversation`, `export`, `delete`, `verify`, `redact-document`, `deletion-register`
and `audit-events` (`86-operations.md` section 2.8.4 and runbooks R06, R07, R19 and R32;
`13-conversations-and-memory.md` sections 10.5 to 10.11; `80-data-model.md` sections 2.21, 2.23
and 3.11; `82-security.md` SEC-119 and SEC-120; ADR 0033, ADR 0149).

One user's conversation data lives in three Cosmos DB containers (`conversations` under the prefix
`[oid]`, `memories` and `usage` under the partition `oid`) and in three SQL tables (`users`,
`project_members`, `user_index_overrides`); `audit_events` and the actor columns hold only the
object ID and stay. An operator reaches them only under a just-in-time level: `read` for
`read-conversation` (Data Reader on `conversations`, `docchat_conversation_reader`), `user_data`
for the rest (Data Contributor on `docchat`, `docchat_user_data_operator`). Without the level a
store refuses the call (exit 3) before anything is read or changed.

The commands use the `docchat_core` models and tables, so nothing they write is a document or a
row the apps would reject: every query is constant text with bound parameters, every SQL statement
a Core construct over `docchat_core.sql.tables` or a constant of `docchat_core.sql.statements`, and
every Cosmos DB call carries the operator commands' 10-second per-operation timeout. Content
leaves the stores only in files under `--out`, each written with mode 0600 and closed before its
audit row commits; standard output carries identifiers, counts and times only.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib import metadata as package_metadata
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol, cast
from uuid import UUID

from azure.core import MatchConditions
from azure.core.exceptions import (
    AzureError,
    ClientAuthenticationError,
    ServiceRequestError,
    ServiceResponseError,
)
from azure.cosmos.aio import CosmosClient
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosClientTimeoutError,
    CosmosHttpResponseError,
    CosmosResourceNotFoundError,
)
from sqlalchemy import Table, bindparam, delete, func, select

from docchat_core.audit import write_audit_event
from docchat_core.audit.models import (
    ConversationReadMetadata,
    DeletionCounts,
    DocumentTracesRedactedMetadata,
    ExportCounts,
    UserDataDeletedMetadata,
    UserDataExportedMetadata,
)
from docchat_core.chat.citations import strip_citation_tokens
from docchat_core.clients.registry import (
    COSMOS_CONNECTION_TIMEOUT_SECONDS,
    COSMOS_CONSISTENCY_LEVEL,
)
from docchat_core.cosmos.containers import CONVERSATIONS, MEMORIES, USAGE
from docchat_core.cosmos.conversations import CitationView, ModelInput, TurnTraceDocument
from docchat_core.models import format_utc
from docchat_core.sql.statements import SET_READ_COMMITTED_ISOLATION, SET_SNAPSHOT_ISOLATION
from docchat_core.sql.tables import ACTOR_COLUMNS, metadata
from docchat_core.sql.tables.access import (
    audit_events,
    project_members,
    projects,
    user_index_overrides,
    users,
)
from docchat_core.sql.tables.schema import nvarchar_literal
from docchat_ops.commands.ingestion import identifier, storage_endpoint, utc_text
from docchat_ops.framework.audit import connect, operator_event, write_event
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
    from collections.abc import (
        AsyncIterator,
        Awaitable,
        Callable,
        Iterable,
        Iterator,
        Mapping,
        Sequence,
    )

    from azure.core.credentials_async import AsyncTokenCredential
    from pydantic import JsonValue
    from sqlalchemy import Connection

    from docchat_ops.cli import Run
    from docchat_ops.framework.audit import OpsDatabase
    from docchat_ops.framework.endpoints import AzdEnvironment

# --- Constants ------------------------------------------------------------------------------------

COSMOS_ENDPOINT: Final = "DOCCHAT_COSMOS_ENDPOINT"
COSMOS_DATABASE: Final = "DOCCHAT_COSMOS_DATABASE"
COSMOS_DATABASE_NAME: Final = "docchat"
COSMOS_OPERATION_TIMEOUT_SECONDS: Final = 10
"""Every Cosmos DB call of the `user` commands, retries included (`13-conversations-and-memory.md`
section 1.11 item 2)."""

TURNS_MAX: Final = 50
"""`--turn` is repeatable up to 50 times; without it the newest 50 turns are read."""
DELETE_BATCH: Final = 8
"""Deletes in flight at a time (`13-conversations-and-memory.md` section 10.9 step 2)."""
REDACTION_PAGE_SIZE: Final = 100
REDACTION_ATTEMPTS: Final = 3
REDACTED_BODY: Final = "[removed]"
AUDIT_LOOKBACK_DAYS: Final = 400
PROJECT_LOOKUP_BATCH: Final = 500
"""Project IDs per statement, well below SQL Server's 2,100 parameters."""

DIRECTORY_MODE: Final = 0o700
FILE_MODE: Final = 0o600
FILE_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
"""A new file only: nothing under `--out` is replaced and no symbolic link is followed."""

SYSTEM_FIELDS: Final = frozenset({"_rid", "_self", "_etag", "_attachments", "_ts"})
"""The Cosmos DB system fields, which no output file carries."""

SESSION_TYPE: Final = "session"
TURN_TYPE: Final = "turn"
TRACE_TYPE: Final = "turn_trace"
MEMORY_TYPE: Final = "memory"
DAY_COUNTER_TYPE: Final = "usage_day"
MONTH_COUNTER_TYPE: Final = "usage_month"

READ_LEVEL: Final = "read"
USER_DATA_LEVEL: Final = "user_data"

# Every query is constant text; `type` literals travel as parameters too (section 1.12 rule 2).
TURNS_QUERY: Final = (
    "SELECT * FROM c WHERE c.userId = @userId AND c.sessionId = @sessionId AND c.type = @type"
)
TURNS_BY_ID_QUERY: Final = TURNS_QUERY + " AND ARRAY_CONTAINS(@turnIds, c.id)"
TRACES_BY_TURN_QUERY: Final = TURNS_QUERY + " AND ARRAY_CONTAINS(@turnIds, c.turnId)"
USER_ITEMS_QUERY: Final = "SELECT * FROM c WHERE c.userId = @userId"
"""Every item of the user's partitions: the prefix `[oid]` of `conversations`, the partition `oid`
of `memories` and `usage` (`80-data-model.md` section 3.11)."""
CONVERSATION_KEYS_QUERY: Final = "SELECT c.id, c.sessionId, c.type FROM c WHERE c.userId = @userId"
"""The keys of the user's `conversations` items: counting and deleting need nothing else, and a
trace can be 1.3 MB."""
ITEM_IDS_QUERY: Final = "SELECT c.id FROM c WHERE c.userId = @userId"
REDACTION_QUERY: Final = (
    "SELECT c.id, c.userId, c.sessionId FROM c WHERE c.type = @type AND "
    "EXISTS(SELECT VALUE k FROM k IN c.retrieval.kept WHERE k.documentId = @documentId)"
)
"""The one cross-partition query (`13-conversations-and-memory.md` section 10.10 step 3), served
by the index path `/retrieval/kept/[]/documentId/?`."""

REGISTER_ACTIONS: Final = ("user_data.deleted", "document.traces_redacted")
_events = audit_events.c
DELETION_REGISTER: Final = (
    select(_events.action, _events.subject_object_id, _events.target_id, _events.occurred_at)
    .where(
        _events.action.in_([nvarchar_literal(action) for action in REGISTER_ACTIONS]),
        _events.occurred_at >= bindparam("restore_point"),
    )
    .order_by(_events.occurred_at, _events.id)
)
"""The constant statement of `13-conversations-and-memory.md` section 10.11, `:restore_point`
bound as a parameter, oldest first."""
AUDIT_EVENTS_OF_ACTOR: Final = (
    select(
        _events.occurred_at,
        _events.action,
        _events.target_type,
        _events.target_id,
        _events.project_id,
        _events.metadata_json,
    )
    .where(_events.actor_object_id == bindparam("actor"), _events.occurred_at >= bindparam("since"))
    .order_by(_events.occurred_at.desc(), _events.id.desc())
)
"""A person's audit rows since a time, newest first; a seek on `IX_audit_events_actor`."""

READ_CONVERSATION_UNIT: Final = "user_read_conversation"
EXPORT_READ_UNIT: Final = "user_export_read"
PROJECT_NAMES_UNIT: Final = "user_export_project_names"
COUNT_UNIT: Final = "user_rows_count"
DELETE_UNIT: Final = "user_rows_delete"
REGISTER_UNIT: Final = "user_deletion_register"
AUDIT_EVENTS_UNIT: Final = "user_audit_events"

SESSION_COLUMNS: Final = ("userId", "sessionId")
REGISTER_COLUMNS: Final = ("action", "subject_object_id", "target_id", "occurred_at")
AUDIT_COLUMNS: Final = (
    "occurred_at",
    "action",
    "target_type",
    "target_id",
    "project_id",
    "metadata_json",
)

RERUN: Final = "run the same command once more"
LEVEL_STEP: Final = "check the just-in-time level {level}, then az logout and az login"
FIREWALL_MARKER: Final = "Request originated from"
"""Cosmos DB names the network path of a request its firewall refused (`403`); a missing role
answers `403` without it."""
UNREACHABLE_STATUSES: Final = frozenset({408, 429, 503})
"""A timeout, throttling left after the SDK's retries, or an unavailable service."""
_SINCE_RE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_SINCE_FORMAT: Final = "%Y-%m-%dT%H:%M:%SZ"
_EXPORT_FILES: Final = (
    "subject/profile.json",
    "subject/memberships.json",
    "subject/sessions.json",
    "subject/memory.json",
    "subject/usage.json",
    "internal/conversations.jsonl",
    "internal/memory.json",
    "internal/usage.jsonl",
    "internal/turn_traces.jsonl",
    "internal/audit_events.json",
    "internal/actor_references.json",
    "internal/user_index_overrides.json",
)

type PartitionKey = str | list[str]

# --- The Cosmos DB containers ---------------------------------------------------------------------


class QueryPages(Protocol):
    """A query's pager: pages fetched one request at a time."""

    def by_page(
        self, continuation_token: str | None = None
    ) -> AsyncIterator[AsyncIterator[Mapping[str, Any]]]: ...


class CosmosContainer(Protocol):
    """The calls the commands make on an `azure.cosmos.aio` container."""

    async def read_item(
        self, item: str, partition_key: PartitionKey, **kwargs: Any
    ) -> Mapping[str, Any]: ...

    def query_items(self, query: str, **kwargs: Any) -> QueryPages: ...

    async def replace_item(
        self, item: str, body: dict[str, Any], **kwargs: Any
    ) -> Mapping[str, Any]: ...

    async def delete_item(self, item: str, partition_key: PartitionKey, **kwargs: Any) -> None: ...


@dataclass(frozen=True)
class UserStores:
    """The three containers of database `docchat`, and how to close their client."""

    conversations: CosmosContainer
    memories: CosmosContainer
    usage: CosmosContainer
    close: Callable[[], Awaitable[None]]


def cosmos_stores(endpoint: str, database: str, credential: AsyncTokenCredential) -> UserStores:
    """The async `CosmosClient` under the operator's own credential, with the connection timeout
    and consistency of `docchat_core.clients`; each call adds the 10-second operation timeout."""
    client = CosmosClient(
        endpoint,
        credential,
        consistency_level=COSMOS_CONSISTENCY_LEVEL,
        connection_timeout=COSMOS_CONNECTION_TIMEOUT_SECONDS,
    )
    proxy = client.get_database_client(database)
    return UserStores(
        conversations=cast("CosmosContainer", proxy.get_container_client(CONVERSATIONS.name)),
        memories=cast("CosmosContainer", proxy.get_container_client(MEMORIES.name)),
        usage=cast("CosmosContainer", proxy.get_container_client(USAGE.name)),
        close=client.close,
    )


def cosmos_failure(error: AzureError) -> OpsError:
    """The exit a Cosmos DB failure maps to; the SDK's message is read, never repeated."""
    if isinstance(error, ClientAuthenticationError):
        return classify(error)
    if isinstance(error, (CosmosClientTimeoutError, ServiceRequestError, ServiceResponseError)):
        return Unreachable(
            "Cosmos DB didn't answer in time",
            "check the network path and the operator allowlist, then run the command again",
        )
    if isinstance(error, CosmosHttpResponseError):
        status = error.status_code or 0
        if status == 403 and FIREWALL_MARKER in str(error.message):  # noqa: PLR2004
            return Unreachable(
                "the Cosmos DB firewall refused this address",
                "run from an address in the environment's operator allowlist",
            )
        if status in {401, 403}:
            return AccessDenied(
                f"Cosmos DB answered {status}",
                "check the just-in-time level, then az logout and az login",
            )
        if status in UNREACHABLE_STATUSES:
            return Unreachable(
                f"Cosmos DB answered {status}", "check that the store is healthy, then run again"
            )
    return classify(error)


@contextmanager
def _cosmos_errors() -> Iterator[None]:
    try:
        yield
    except AzureError as error:
        raise cosmos_failure(error) from None


async def _query(
    container: CosmosContainer,
    query: str,
    parameters: Mapping[str, object],
    *,
    partition_key: PartitionKey | None = None,
    page_size: int | None = None,
) -> AsyncIterator[list[dict[str, Any]]]:
    """The pages of a parameterised query: single-partition or prefix with `partition_key`,
    cross-partition without it. The async client queries across partitions whenever no
    partition key is given and takes no `enable_cross_partition_query` keyword: it would reach
    the HTTP transport and fail there."""
    options: dict[str, Any] = {
        "parameters": [{"name": name, "value": value} for name, value in parameters.items()],
        "timeout": COSMOS_OPERATION_TIMEOUT_SECONDS,
    }
    if partition_key is not None:
        options["partition_key"] = partition_key
    if page_size is not None:
        options["max_item_count"] = page_size
    async for page in container.query_items(query, **options).by_page():
        yield [dict(row) async for row in page]


async def _query_all(
    container: CosmosContainer,
    query: str,
    parameters: Mapping[str, object],
    *,
    partition_key: PartitionKey,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    async for page in _query(container, query, parameters, partition_key=partition_key):
        rows.extend(page)
    return rows


def without_system_fields(item: Mapping[str, Any]) -> dict[str, Any]:
    """A stored item as the output files hold it."""
    return {key: value for key, value in item.items() if key not in SYSTEM_FIELDS}


# --- Arguments ------------------------------------------------------------------------------------


def output_directory(value: str) -> Path:
    """`--out`: the directory to create, checked again before anything is written in it."""
    return Path(value)


def utc_time(value: str) -> datetime:
    """`--since`: ISO 8601 UTC to the second, `yyyy-MM-ddTHH:mm:ssZ`."""
    message = "a time is UTC in the form yyyy-MM-ddTHH:mm:ssZ"
    if not _SINCE_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(message)
    try:
        return datetime.strptime(value, _SINCE_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        raise argparse.ArgumentTypeError(message) from None


def read_conversation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--user", type=identifier, required=True, help="the user's object ID")
    parser.add_argument("--session", type=identifier, required=True, help="the session's ID")
    parser.add_argument(
        "--turn",
        type=identifier,
        action="append",
        default=[],
        help=f"a turn to read, up to {TURNS_MAX} times; the newest {TURNS_MAX} when absent",
    )
    parser.add_argument(
        "--out", type=output_directory, required=True, help="an empty or new directory"
    )


def export_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--user", type=identifier, required=True, help="the user's object ID")
    parser.add_argument(
        "--out", type=output_directory, required=True, help="an empty or new directory"
    )


def delete_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--user", type=identifier, required=True, help="the user's object ID")
    parser.add_argument(
        "--confirm", type=identifier, required=True, help="the user's object ID once more"
    )
    parser.add_argument(
        "--assignment-removed",
        action="store_true",
        help="attests that the user's DocChat assignments and memberships were removed first",
    )


def verify_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--user", type=identifier, required=True, help="the user's object ID")


def redact_document_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--document-id", type=identifier, required=True, help="the deleted document's ID"
    )


def deletion_register_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--since", type=utc_time, required=True, help="the restore point, yyyy-MM-ddTHH:mm:ssZ"
    )


def audit_events_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--actor", type=identifier, required=True, help="the person's object ID")
    parser.add_argument(
        "--since", type=utc_time, required=True, help="the earliest time, yyyy-MM-ddTHH:mm:ssZ"
    )


def _refuse_future(run: Run, since: datetime) -> None:
    if since > run.toolkit.clock.now():
        raise UsageError(f"--since {format_utc(since)} is in the future", "give a past UTC time")


def _naive(value: datetime) -> datetime:
    # `DATETIME2` holds UTC without an offset.
    return value.astimezone(UTC).replace(tzinfo=None)


# --- Output files ---------------------------------------------------------------------------------


def refuse_output(out: Path) -> None:
    """Before any sign-in: `--out` is a new directory under an existing one, or an empty one."""
    step = "choose a new or empty --out directory; nothing was written"
    if out.is_symlink():
        raise UsageError(f"--out {out} is a symbolic link", step)
    if out.exists():
        if not out.is_dir():
            raise UsageError(f"--out {out} isn't a directory", step)
        if any(out.iterdir()):
            raise UsageError(f"--out {out} already holds files", step)
    elif not out.parent.is_dir():
        raise UsageError(f"the directory above --out {out} doesn't exist", step)


class OutputFile:
    """One new file under `--out`, hashed as it is written."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            self._descriptor = os.open(path, FILE_FLAGS, FILE_MODE)
        except OSError:
            raise UsageError(
                f"{path} can't be created", "choose a new or empty --out directory"
            ) from None
        # Exact whatever the operator's umask holds: read and write for the owner only.
        os.fchmod(self._descriptor, FILE_MODE)
        self._digest = hashlib.sha256()
        self._open = True

    def write(self, data: bytes) -> None:
        self._digest.update(data)
        view = memoryview(data)
        while view:
            view = view[os.write(self._descriptor, view) :]

    def close(self) -> None:
        if not self._open:
            return
        self._open = False
        try:
            os.fsync(self._descriptor)
        finally:
            os.close(self._descriptor)

    def sha256(self) -> str:
        return self._digest.hexdigest()


@dataclass
class OutputDirectory:
    """`--out` and what a run wrote in it, so a failed run leaves nothing behind."""

    root: Path
    created: bool
    files: dict[str, OutputFile] = field(default_factory=dict)
    directories: list[Path] = field(default_factory=list)

    @classmethod
    def open(cls, out: Path) -> OutputDirectory:
        """Create `--out` with mode 0700, or take the empty directory that is there."""
        try:
            out.mkdir(mode=DIRECTORY_MODE)
        except FileExistsError:
            refuse_output(out)
            return cls(out, created=False)
        except OSError:
            raise UsageError(
                f"--out {out} can't be created", "choose a new or empty --out directory"
            ) from None
        out.chmod(DIRECTORY_MODE)
        return cls(out, created=True)

    def file(self, relative: str) -> OutputFile:
        """A new file at `relative`, its directories created with mode 0700."""
        path = self.root / relative
        parent = path.parent
        if parent != self.root and parent not in self.directories:
            parent.mkdir(mode=DIRECTORY_MODE)
            parent.chmod(DIRECTORY_MODE)
            self.directories.append(parent)
        output = OutputFile(path)
        self.files[relative] = output
        return output

    def write(self, relative: str, data: bytes) -> str:
        """Write a whole file and close it; returns its SHA-256."""
        output = self.file(relative)
        try:
            output.write(data)
        finally:
            output.close()
        return output.sha256()

    def close(self) -> None:
        for output in self.files.values():
            output.close()

    def remove(self) -> None:
        """Remove every file and directory this run created."""
        for output in self.files.values():
            output.close()
            output.path.unlink(missing_ok=True)
        for directory in reversed(self.directories):
            directory.rmdir()
        if self.created:
            self.root.rmdir()


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def json_line(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def sql_value(value: object) -> JsonValue:
    """A SQL value as JSON: UUIDs lower-case, times ISO 8601 UTC with milliseconds."""
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return utc_text(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value).lower()


def _lower(value: object) -> str | None:
    return None if value is None else str(value).lower()


# --- Sessions -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class UserSession:
    """An operator's sign-in for one `user` command."""

    credential: AsyncTokenCredential
    database: OpsDatabase
    actor: str
    stores: UserStores | None

    def containers(self) -> UserStores:
        if self.stores is None:
            raise RuntimeError("this command opened no Cosmos DB client")
        return self.stores


def cosmos_target(environment: AzdEnvironment) -> tuple[str, str]:
    """`DOCCHAT_COSMOS_ENDPOINT` (an `https` endpoint) and `DOCCHAT_COSMOS_DATABASE` (exit 2)."""
    endpoint = storage_endpoint(environment, COSMOS_ENDPOINT)
    database = environment.require(COSMOS_DATABASE)
    if database != COSMOS_DATABASE_NAME:
        name = environment.environment
        raise UsageError(
            f"{COSMOS_DATABASE} in .azure/{name}/.env isn't {COSMOS_DATABASE_NAME}",
            f"run azd env refresh -e {name}",
        )
    return endpoint, database


@asynccontextmanager
async def user_session(run: Run, *, cosmos: bool) -> AsyncIterator[UserSession]:
    """Refuse a service principal for a command that changes data outside `load` (exit 3), sign
    in to SQL, which names the actor, then open the Cosmos DB client when the command reads it."""
    environment = run.azd()
    target = environment.sql_target()
    endpoint_and_database = cosmos_target(environment) if cosmos else None
    if run.spec.changes_data:
        await run.refuse_service_principal()
    async with run.toolkit.credential() as credential:
        database = run.toolkit.database(target, credential)
        try:
            actor = await connect(database)
            stores: UserStores | None = None
            if endpoint_and_database is not None:
                endpoint, name = endpoint_and_database
                stores = run.toolkit.cosmos(endpoint, name, credential)
            try:
                yield UserSession(credential, database, actor, stores)
            finally:
                if stores is not None:
                    await stores.close()
        finally:
            await database.dispose()


def _ticket(run: Run) -> str:
    return run.ticket or ""


async def _remove_on_failure(directory: OutputDirectory) -> None:
    await asyncio.to_thread(directory.remove)


# --- read-conversation ----------------------------------------------------------------------------


def _seq(item: Mapping[str, Any]) -> int:
    value = item.get("seq")
    return value if isinstance(value, int) else 0


async def _read_conversation_documents(
    container: CosmosContainer, user: UUID, session_id: UUID, turn_ids: Sequence[UUID]
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    key = [str(user), str(session_id)]
    try:
        session = await container.read_item(
            str(session_id), key, timeout=COSMOS_OPERATION_TIMEOUT_SECONDS
        )
    except CosmosResourceNotFoundError:
        raise NotFound(
            f"no session {session_id} for user {user}", "check the IDs in the ticket"
        ) from None
    base = {"@userId": str(user), "@sessionId": str(session_id)}
    if turn_ids:
        wanted = [str(turn) for turn in turn_ids]
        turns = await _query_all(
            container,
            TURNS_BY_ID_QUERY,
            {**base, "@type": TURN_TYPE, "@turnIds": wanted},
            partition_key=key,
        )
    else:
        wanted = []
        every_turn = await _query_all(
            container, TURNS_QUERY, {**base, "@type": TURN_TYPE}, partition_key=key
        )
        turns = sorted(every_turn, key=_seq, reverse=True)[:TURNS_MAX]
    turns.sort(key=_seq)
    trace_turns = wanted or [str(turn["id"]) for turn in turns]
    traces: list[dict[str, Any]] = []
    if trace_turns:
        traces = await _query_all(
            container,
            TRACES_BY_TURN_QUERY,
            {**base, "@type": TRACE_TYPE, "@turnIds": trace_turns},
            partition_key=key,
        )
    order = {str(turn["id"]): index for index, turn in enumerate(turns)}
    traces.sort(key=lambda trace: order.get(str(trace.get("turnId")), len(order)))
    return (
        without_system_fields(session),
        [without_system_fields(turn) for turn in turns],
        [without_system_fields(trace) for trace in traces],
    )


async def read_conversation(run: Run, args: argparse.Namespace) -> None:
    """Level `read`: one session's turns and traces into `<out>/<sessionId>.json`, then the
    `conversation.read` row."""
    user: UUID = args.user
    session_id: UUID = args.session
    given: list[UUID] = args.turn
    out: Path = args.out
    if len(given) > TURNS_MAX:
        raise UsageError(
            f"--turn is given {len(given)} times; at most {TURNS_MAX} are read",
            f"give at most {TURNS_MAX} turn IDs",
        )
    turn_ids = list(dict.fromkeys(given))
    refuse_output(out)
    async with user_session(run, cosmos=True) as session:
        with _cosmos_errors():
            document, turns, traces = await _read_conversation_documents(
                session.containers().conversations, user, session_id, turn_ids
            )
        read_ids = [str(turn["id"]) for turn in turns]
        # Built before the file exists, so a row the catalogue refuses fails before any content
        # leaves the store.
        event = operator_event(
            action="conversation.read",
            actor_object_id=session.actor,
            subject_object_id=str(user),
            target_type="session",
            target_id=str(session_id),
            metadata=ConversationReadMetadata(
                turn_ids=[UUID(turn) for turn in read_ids],
                ticket=_ticket(run),
                level=READ_LEVEL,
            ),
        )
        content = json_bytes({"session": document, "turns": turns, "turnTraces": traces})
        relative = f"{session_id}.json"
        directory = await asyncio.to_thread(OutputDirectory.open, out)
        try:
            await asyncio.to_thread(directory.write, relative, content)
            await write_event(session.database, event)
        except BaseException:
            # No conversation text leaves the store without its audit row.
            await _remove_on_failure(directory)
            raise
    run.report(
        "read",
        {
            "userId": str(user),
            "sessionId": str(session_id),
            "turns": len(turns),
            "turnTraces": len(traces),
            "file": str(out / relative),
        },
    )


# --- Counting -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ConversationKey:
    item_id: str
    session_id: str
    is_session: bool


@dataclass(frozen=True)
class CosmosItems:
    """The keys of every item of one user's partitions."""

    conversations: list[ConversationKey]
    memories: list[str]
    usage: list[str]


async def cosmos_items(stores: UserStores, user: UUID) -> CosmosItems:
    parameters = {"@userId": str(user)}
    conversations = [
        ConversationKey(str(row["id"]), str(row["sessionId"]), row.get("type") == SESSION_TYPE)
        for row in await _query_all(
            stores.conversations, CONVERSATION_KEYS_QUERY, parameters, partition_key=[str(user)]
        )
    ]
    memories = [
        str(row["id"])
        for row in await _query_all(
            stores.memories, ITEM_IDS_QUERY, parameters, partition_key=str(user)
        )
    ]
    usage = [
        str(row["id"])
        for row in await _query_all(
            stores.usage, ITEM_IDS_QUERY, parameters, partition_key=str(user)
        )
    ]
    return CosmosItems(conversations, memories, usage)


@dataclass(frozen=True)
class SqlRows:
    users: int
    project_members: int
    user_index_overrides: int


def _count(connection: Connection, table: Table, column: str, user: UUID) -> int:
    statement = select(func.count()).select_from(table).where(table.c[column] == user)
    return int(connection.execute(statement).scalar_one())


async def sql_rows(database: OpsDatabase, user: UUID) -> SqlRows:
    def read(connection: Connection) -> SqlRows:
        return SqlRows(
            users=_count(connection, users, "object_id", user),
            project_members=_count(connection, project_members, "user_object_id", user),
            user_index_overrides=_count(connection, user_index_overrides, "user_object_id", user),
        )

    return await database.run(read, name=COUNT_UNIT, retry="transient")


def deletion_counts(items: CosmosItems, rows: SqlRows) -> DeletionCounts:
    return DeletionCounts(
        conversations=len(items.conversations),
        memories=len(items.memories),
        usage=len(items.usage),
        users=rows.users,
        project_members=rows.project_members,
        user_index_overrides=rows.user_index_overrides,
    )


def counts_report(counts: DeletionCounts) -> dict[str, JsonValue]:
    report: dict[str, JsonValue] = counts.model_dump(by_alias=True)
    return report


async def count_everything(session: UserSession, user: UUID) -> DeletionCounts:
    with _cosmos_errors():
        items = await cosmos_items(session.containers(), user)
    return deletion_counts(items, await sql_rows(session.database, user))


def _anything(counts: DeletionCounts) -> bool:
    return any(value > 0 for value in counts.model_dump().values())


# --- verify ---------------------------------------------------------------------------------------


async def verify(run: Run, args: argparse.Namespace) -> None:
    """Every count zero is exit 0; anything left is exit 5. No audit row."""
    user: UUID = args.user
    async with user_session(run, cosmos=True) as session:
        counts = await count_everything(session, user)
    remaining = _anything(counts)
    run.report(
        "remaining" if remaining else "verified",
        {"userId": str(user), "counts": counts_report(counts)},
    )
    if remaining:
        raise Incomplete(
            f"user {user} still has data in a store",
            "a request recreated data: run user delete once more, then user verify",
        )


# --- delete ---------------------------------------------------------------------------------------


class _Stopped(Exception):  # noqa: N818 - a signal carrying the deletes made before it
    def __init__(self, deleted: int, error: BaseException) -> None:
        super().__init__(type(error).__name__)
        self.deleted = deleted
        self.error = error


async def _delete_one(container: CosmosContainer, item_id: str, key: PartitionKey) -> None:
    try:
        await container.delete_item(item_id, key, timeout=COSMOS_OPERATION_TIMEOUT_SECONDS)
    except CosmosResourceNotFoundError:
        # Gone already (a rerun, or the item's retention): counted as deleted.
        return


async def delete_in_batches(
    container: CosmosContainer, keys: Sequence[tuple[str, PartitionKey]]
) -> int:
    """Delete `keys` `DELETE_BATCH` at a time; returns the number deleted. A failure lets the
    batch in flight finish, then raises `_Stopped` with the count deleted before it."""
    deleted = 0
    for start in range(0, len(keys), DELETE_BATCH):
        batch = keys[start : start + DELETE_BATCH]
        results = await asyncio.gather(
            *(_delete_one(container, item_id, key) for item_id, key in batch),
            return_exceptions=True,
        )
        failures = [result for result in results if isinstance(result, BaseException)]
        deleted += len(results) - len(failures)
        if failures:
            if not all(isinstance(failure, Exception) for failure in failures):
                raise next(f for f in failures if not isinstance(f, Exception))
            raise _Stopped(deleted, failures[0])
    return deleted


@dataclass
class _Deletion:
    """The Cosmos DB items a `delete` run removed so far, per container, so a failure after the
    first delete exits 5 and one before it keeps its own exit code."""

    deleted: dict[str, int] = field(
        default_factory=lambda: {CONVERSATIONS.name: 0, MEMORIES.name: 0, USAGE.name: 0}
    )

    @property
    def started(self) -> bool:
        return any(self.deleted.values())

    def stopped(self, error: BaseException) -> OpsError:
        failure = cosmos_failure(error) if isinstance(error, AzureError) else classify(error)
        if not self.started:
            return failure
        return Incomplete(f"the deletion stopped part-way ({failure.cause})", RERUN)


async def _delete_cosmos(stores: UserStores, user: UUID, items: CosmosItems) -> _Deletion:
    deletion = _Deletion()
    items_first: list[tuple[str, PartitionKey]] = []
    sessions_last: list[tuple[str, PartitionKey]] = []
    for key in items.conversations:
        target = sessions_last if key.is_session else items_first
        target.append((key.item_id, [str(user), key.session_id]))
    # Traces and turns go before sessions, so a run stopped part-way never leaves turns whose
    # session is already gone.
    phases: list[tuple[CosmosContainer, list[tuple[str, PartitionKey]], str]] = [
        (stores.conversations, items_first, CONVERSATIONS.name),
        (stores.conversations, sessions_last, CONVERSATIONS.name),
        (stores.memories, [(item, str(user)) for item in items.memories], MEMORIES.name),
        (stores.usage, [(item, str(user)) for item in items.usage], USAGE.name),
    ]
    for container, keys, store in phases:
        try:
            count = await delete_in_batches(container, keys)
        except _Stopped as stopped:
            deletion.deleted[store] += stopped.deleted
            raise deletion.stopped(stopped.error) from None
        deletion.deleted[store] += count
    return deletion


def _delete_summary(user: UUID, counts: DeletionCounts, items: CosmosItems) -> list[str]:
    sessions = sum(1 for key in items.conversations if key.is_session)
    return [
        f"user: {user}",
        (
            f"conversations: {counts.conversations} items "
            f"({counts.conversations - sessions} turns and traces, {sessions} sessions)"
        ),
        f"memories: {counts.memories} items",
        f"usage: {counts.usage} items",
        f"users: {counts.users} rows",
        f"project_members: {counts.project_members} rows",
        f"user_index_overrides: {counts.user_index_overrides} rows",
        (
            "change: every item and row above is deleted; audit rows, actor columns and "
            "organisational documents stay"
        ),
    ]


async def delete_user(run: Run, args: argparse.Namespace) -> None:
    """The user's Cosmos DB items, then their SQL rows with `user_data.deleted` in one
    transaction, then a recount: anything left is exit 5, and a rerun finishes the work."""
    user: UUID = args.user
    if args.confirm != user:
        raise UsageError("--confirm differs from --user; nothing was written", "give the same ID")
    if not args.assignment_removed:
        raise UsageError(
            "--assignment-removed is missing; nothing was written",
            "remove the user's DocChat assignments and memberships first (R06 step B1), "
            "then rerun with --assignment-removed",
        )
    async with user_session(run, cosmos=True) as session:
        stores = session.containers()
        with _cosmos_errors():
            items = await cosmos_items(stores, user)
        rows = await sql_rows(session.database, user)
        counts = deletion_counts(items, rows)
        run.progress(
            "counts to delete: "
            + ", ".join(f"{name}={value}" for name, value in counts_report(counts).items())
        )
        await run.confirm(_delete_summary(user, counts, items))
        deletion = await _delete_cosmos(stores, user, items)
        try:
            deleted = await _delete_rows(run, session, user, deletion)
        except Exception as error:  # noqa: BLE001 - a SQL failure after the Cosmos DB deletes
            raise deletion.stopped(error) from None
        try:
            remaining = await count_everything(session, user)
        except Exception as error:  # noqa: BLE001 - the work is done but can't be proved
            raise Incomplete(f"the recount failed ({classify(error).cause})", RERUN) from None
    left = _anything(remaining)
    run.report(
        "incomplete" if left else "deleted",
        {
            "userId": str(user),
            "counts": counts_report(deleted),
            "remaining": counts_report(remaining),
        },
    )
    if left:
        raise Incomplete(f"user {user} still has data in a store", RERUN)


async def _delete_rows(
    run: Run, session: UserSession, user: UUID, deletion: _Deletion
) -> DeletionCounts:
    """The SQL rows and the audit row in one transaction, never retried."""

    def work(connection: Connection) -> DeletionCounts:
        # The referencing rows first, so the delete never depends on a cascade's permission.
        overrides = connection.execute(
            delete(user_index_overrides).where(user_index_overrides.c.user_object_id == user)
        ).rowcount
        members = connection.execute(
            delete(project_members).where(project_members.c.user_object_id == user)
        ).rowcount
        removed = connection.execute(delete(users).where(users.c.object_id == user)).rowcount
        counts = DeletionCounts(
            conversations=deletion.deleted[CONVERSATIONS.name],
            memories=deletion.deleted[MEMORIES.name],
            usage=deletion.deleted[USAGE.name],
            users=max(removed, 0),
            project_members=max(members, 0),
            user_index_overrides=max(overrides, 0),
        )
        event = operator_event(
            action="user_data.deleted",
            actor_object_id=session.actor,
            subject_object_id=str(user),
            target_type="user",
            target_id=str(user),
            metadata=UserDataDeletedMetadata(ticket=_ticket(run), counts=counts),
        )
        write_audit_event(connection, event)
        return counts

    return await session.database.run(work, name=DELETE_UNIT, retry="none")


# --- export ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SqlExport:
    """The user's SQL rows, read in one snapshot transaction."""

    user: dict[str, JsonValue] | None
    memberships: list[dict[str, JsonValue]]
    overrides: list[dict[str, JsonValue]]
    audit_events: list[dict[str, JsonValue]]
    actor_references: list[dict[str, JsonValue]]

    def any(self) -> bool:
        return bool(
            self.user
            or self.memberships
            or self.overrides
            or self.audit_events
            or self.actor_references
        )


def _row(row: Any, columns: Iterable[str]) -> dict[str, JsonValue]:
    return {column: sql_value(getattr(row, column)) for column in columns}


def actor_reference_statements(user: UUID) -> list[tuple[str, str, Any]]:
    """For each column marked as an actor column, the statement reading the primary keys of the
    rows that hold the user's ID; built from the metadata, so a column a later revision marks is
    exported from the release that declares it."""
    statements: list[tuple[str, str, Any]] = []
    for table_name in sorted(ACTOR_COLUMNS):
        table = metadata.tables[f"{metadata.schema}.{table_name}"]
        keys = list(table.primary_key.columns)
        for column in ACTOR_COLUMNS[table_name]:
            statement = select(*keys).where(table.c[column] == user).order_by(*keys)
            statements.append((table_name, column, statement))
    return statements


def _start_snapshot(connection: Connection) -> None:
    """Make the unit's transaction a `SNAPSHOT` one. The ODBC driver opens its transaction before
    the unit's first statement, and SQL Server refuses a read at `SNAPSHOT` in a transaction that
    started at another level (error 3951); so the transaction holding only the `SET` is committed
    and the driver's next one starts at `SNAPSHOT`, as SQLAlchemy's own `SNAPSHOT` isolation
    level does. Nothing has been read yet, so that commit changes nothing."""
    connection.execute(SET_SNAPSHOT_ISOLATION)
    connection.connection.commit()


def _read_sql_export(connection: Connection, user: UUID) -> SqlExport:
    _start_snapshot(connection)
    user_columns = [column.name for column in users.columns]
    user_row = connection.execute(select(users).where(users.c.object_id == user)).first()
    members = project_members.c
    memberships = connection.execute(
        select(
            members.user_object_id,
            members.project_id,
            members.grant_source,
            members.granted_by,
            members.granted_at,
            projects.c.name.label("project_name"),
        )
        .join_from(project_members, projects, members.project_id == projects.c.id)
        .where(members.user_object_id == user)
        .order_by(members.project_id)
    ).all()
    # Never the reason: it is the operator's text, kept only in the row.
    override_columns = [
        column.name for column in user_index_overrides.columns if column.name != "reason"
    ]
    overrides = connection.execute(
        select(*(user_index_overrides.c[column] for column in override_columns))
        .where(user_index_overrides.c.user_object_id == user)
        .order_by(user_index_overrides.c.project_id)
    ).all()
    # Two statements, so each is a seek on its own index (`IX_audit_events_actor`,
    # `IX_audit_events_subject`).
    event_columns = [column.name for column in audit_events.columns]
    events: dict[int, Any] = {}
    for condition in (_events.actor_object_id == user, _events.subject_object_id == user):
        for row in connection.execute(select(audit_events).where(condition)).all():
            events[int(row.id)] = row
    references: list[dict[str, JsonValue]] = []
    for table_name, column, statement in actor_reference_statements(user):
        key_names = [key.name for key in statement.selected_columns]
        references.extend(
            {"table": table_name, "column": column, "primaryKey": _row(row, key_names)}
            for row in connection.execute(statement).all()
        )
    connection.execute(SET_READ_COMMITTED_ISOLATION)
    return SqlExport(
        user=None if user_row is None else _row(user_row, user_columns),
        memberships=[
            _row(
                row,
                (
                    "user_object_id",
                    "project_id",
                    "grant_source",
                    "granted_by",
                    "granted_at",
                    "project_name",
                ),
            )
            for row in memberships
        ],
        overrides=[_row(row, override_columns) for row in overrides],
        audit_events=[_row(events[key], event_columns) for key in sorted(events)],
        actor_references=references,
    )


async def _project_names(database: OpsDatabase, project_ids: Sequence[str]) -> dict[str, str]:
    """The names of the sessions' projects, hidden ones included; a missing row stays null."""
    batches = [
        [UUID(value) for value in project_ids[start : start + PROJECT_LOOKUP_BATCH]]
        for start in range(0, len(project_ids), PROJECT_LOOKUP_BATCH)
    ]

    def read(connection: Connection) -> dict[str, str]:
        names: dict[str, str] = {}
        for batch in batches:
            rows = connection.execute(
                select(projects.c.id, projects.c.name).where(projects.c.id.in_(batch))
            ).all()
            for row in rows:
                names[str(row.id).lower()] = str(row.name)
        return names

    if not batches:
        return {}
    return await database.run(read, name=PROJECT_NAMES_UNIT, retry="transient")


@dataclass
class CosmosExport:
    """What the export keeps in memory for the subject package; traces go straight to their
    file, because each can be 1.3 MB."""

    sessions: list[dict[str, Any]] = field(default_factory=list)
    turns: list[dict[str, Any]] = field(default_factory=list)
    conversations: int = 0
    memory: dict[str, Any] | None = None
    memories: int = 0
    usage: list[dict[str, Any]] = field(default_factory=list)

    def any(self) -> bool:
        return bool(self.conversations or self.memories or self.usage)


async def _export_cosmos(
    stores: UserStores, user: UUID, directory: OutputDirectory
) -> CosmosExport:
    exported = CosmosExport()
    parameters = {"@userId": str(user)}
    conversations = await asyncio.to_thread(directory.file, "internal/conversations.jsonl")
    traces = await asyncio.to_thread(directory.file, "internal/turn_traces.jsonl")
    async for page in _query(
        stores.conversations, USER_ITEMS_QUERY, parameters, partition_key=[str(user)]
    ):
        session_lines: list[bytes] = []
        trace_lines: list[bytes] = []
        for item in page:
            document = without_system_fields(item)
            exported.conversations += 1
            kind = document.get("type")
            if kind == TRACE_TYPE:
                trace_lines.append(json_line(document))
                continue
            session_lines.append(json_line(document))
            if kind == SESSION_TYPE:
                exported.sessions.append(document)
            elif kind == TURN_TYPE:
                exported.turns.append(document)
        await asyncio.to_thread(conversations.write, b"".join(session_lines))
        await asyncio.to_thread(traces.write, b"".join(trace_lines))
    await asyncio.to_thread(conversations.close)
    await asyncio.to_thread(traces.close)
    memory_items = await _query_all(
        stores.memories, USER_ITEMS_QUERY, parameters, partition_key=str(user)
    )
    exported.memories = len(memory_items)
    for item in memory_items:
        if item.get("type") == MEMORY_TYPE:
            exported.memory = without_system_fields(item)
    usage_lines: list[bytes] = []
    async for page in _query(stores.usage, USER_ITEMS_QUERY, parameters, partition_key=str(user)):
        for item in page:
            document = without_system_fields(item)
            exported.usage.append(document)
            usage_lines.append(json_line(document))
    await asyncio.to_thread(directory.write, "internal/usage.jsonl", b"".join(usage_lines))
    return exported


def citation_view(citation: Mapping[str, Any]) -> dict[str, Any]:
    """The `CitationView` of a stored citation: the cited chunk's IDs never leave the server."""
    view: dict[str, Any] = CitationView.model_validate(citation, extra="ignore").model_dump(
        mode="json"
    )
    return view


def subject_turn(turn: Mapping[str, Any]) -> dict[str, Any]:
    citations = turn.get("citations") or []
    answer = turn.get("answer")
    return {
        "seq": turn.get("seq"),
        "status": turn.get("status"),
        "question": turn.get("question"),
        # A citation token's number points at a source of its own turn only, so it leaves
        # without its tokens (`12-chat-pipeline.md` section 11.6).
        "answer": strip_citation_tokens(answer) if isinstance(answer, str) else answer,
        "citations": [citation_view(citation) for citation in citations],
        "followups": turn.get("followups") or [],
        "feedback": turn.get("feedback"),
        "createdAt": turn.get("createdAt"),
        "endedAt": turn.get("endedAt"),
    }


def subject_sessions(exported: CosmosExport, names: Mapping[str, str]) -> list[dict[str, Any]]:
    """Every session with its turns, hidden sessions included; turns whose session document has
    expired are listed under their session ID with no title."""
    turns_by_session: dict[str, list[Mapping[str, Any]]] = {}
    for turn in exported.turns:
        turns_by_session.setdefault(str(turn.get("sessionId")), []).append(turn)
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()

    def entry(session_id: str, project_id: object, values: Mapping[str, Any]) -> dict[str, Any]:
        turns = sorted(turns_by_session.get(session_id, []), key=_seq)
        return {
            "sessionId": session_id,
            "projectId": project_id,
            "projectName": names.get(str(project_id)),
            "title": values.get("title"),
            "createdAt": values.get("createdAt"),
            "updatedAt": values.get("updatedAt"),
            "lastTurnAt": values.get("lastTurnAt"),
            "turns": [subject_turn(turn) for turn in turns],
        }

    for session in sorted(
        exported.sessions,
        key=lambda item: (str(item.get("createdAt")), str(item.get("sessionId"))),
    ):
        session_id = str(session.get("sessionId"))
        seen.add(session_id)
        entries.append(entry(session_id, session.get("projectId"), session))
    for session_id in sorted(set(turns_by_session) - seen):
        project_id = turns_by_session[session_id][0].get("projectId")
        entries.append(entry(session_id, project_id, {}))
    return entries


def subject_memory(memory: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if memory is None:
        return None
    return {
        "paused": memory.get("paused", False),
        "facts": [
            {
                "text": fact.get("text"),
                "category": fact.get("category"),
                "createdAt": fact.get("createdAt"),
                "updatedAt": fact.get("updatedAt"),
            }
            for fact in memory.get("facts") or []
        ],
    }


COUNTER_KEYS: Final = (
    "period",
    "promptTokens",
    "completionTokens",
    "reasoningTokens",
    "cachedTokens",
    "cacheWriteTokens",
    "totalTokens",
    "calls",
    "updatedAt",
)


def subject_usage(usage: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    def counters(kind: str) -> list[dict[str, Any]]:
        chosen = [item for item in usage if item.get("type") == kind]
        chosen.sort(key=lambda item: str(item.get("period")))
        return [{key: item.get(key) for key in COUNTER_KEYS} for item in chosen]

    return {"days": counters(DAY_COUNTER_TYPE), "months": counters(MONTH_COUNTER_TYPE)}


def subject_profile(user: Mapping[str, JsonValue] | None) -> dict[str, JsonValue] | None:
    if user is None:
        return None
    return {
        "displayName": user.get("display_name"),
        "email": user.get("email"),
        "firstSeenAt": user.get("first_seen_at"),
        "lastSeenAt": user.get("last_seen_at"),
        "lastKnownRole": user.get("last_known_role"),
    }


def subject_memberships(rows: Sequence[Mapping[str, JsonValue]]) -> list[dict[str, JsonValue]]:
    return [
        {
            "projectId": row.get("project_id"),
            "projectName": row.get("project_name"),
            "grantedAt": row.get("granted_at"),
        }
        for row in rows
    ]


def export_counts(sql: SqlExport, cosmos: CosmosExport) -> ExportCounts:
    return ExportCounts(
        conversations=cosmos.conversations,
        memories=cosmos.memories,
        usage=len(cosmos.usage),
        users=0 if sql.user is None else 1,
        project_members=len(sql.memberships),
        user_index_overrides=len(sql.overrides),
        audit_events=len(sql.audit_events),
        actor_references=len(sql.actor_references),
    )


def command_version() -> str:
    """The `docchat-ops` distribution's version, as the checkout installed it."""
    try:
        return package_metadata.version("docchat-ops")
    except package_metadata.PackageNotFoundError:
        return "unknown"


def _write_export_files(
    directory: OutputDirectory,
    sql: SqlExport,
    cosmos: CosmosExport,
    names: Mapping[str, str],
) -> None:
    directory.write("subject/profile.json", json_bytes(subject_profile(sql.user)))
    directory.write("subject/memberships.json", json_bytes(subject_memberships(sql.memberships)))
    directory.write("subject/sessions.json", json_bytes(subject_sessions(cosmos, names)))
    directory.write("subject/memory.json", json_bytes(subject_memory(cosmos.memory)))
    directory.write("subject/usage.json", json_bytes(subject_usage(cosmos.usage)))
    directory.write("internal/memory.json", json_bytes(cosmos.memory))
    directory.write("internal/audit_events.json", json_bytes(sql.audit_events))
    directory.write("internal/actor_references.json", json_bytes(sql.actor_references))
    directory.write("internal/user_index_overrides.json", json_bytes(sql.overrides))


def _require_data(user: UUID, sql: SqlExport, cosmos: CosmosExport) -> None:
    if not sql.any() and not cosmos.any():
        raise NotFound(f"no data for user {user} in any store", "check the object ID")


async def export(run: Run, args: argparse.Namespace) -> None:
    """Level `user_data`: the packages `subject/` and `internal/` and `manifest.json` under
    `--out`, then the `user_data.exported` row."""
    user: UUID = args.user
    out: Path = args.out
    refuse_output(out)
    async with user_session(run, cosmos=True) as session:
        sql = await session.database.run(
            lambda connection: _read_sql_export(connection, user),
            name=EXPORT_READ_UNIT,
            retry="transient",
        )
        directory = await asyncio.to_thread(OutputDirectory.open, out)
        try:
            with _cosmos_errors():
                cosmos = await _export_cosmos(session.containers(), user, directory)
            _require_data(user, sql, cosmos)
            project_ids = sorted(
                {
                    str(project)
                    for item in (*cosmos.sessions, *cosmos.turns)
                    if (project := item.get("projectId")) is not None
                }
            )
            names = await _project_names(session.database, project_ids)
            counts = export_counts(sql, cosmos)
            await asyncio.to_thread(_write_export_files, directory, sql, cosmos, names)
            manifest = {
                "user": str(user),
                "environment": run.environment,
                "ticket": _ticket(run),
                "operator": session.actor,
                "createdAt": format_utc(run.toolkit.clock.now()),
                "commandVersion": command_version(),
                "counts": counts.model_dump(by_alias=True),
                "files": {
                    relative: directory.files[relative].sha256() for relative in _EXPORT_FILES
                },
            }
            manifest_sha256 = await asyncio.to_thread(
                directory.write, "manifest.json", json_bytes(manifest)
            )
            event = operator_event(
                action="user_data.exported",
                actor_object_id=session.actor,
                subject_object_id=str(user),
                target_type="user",
                target_id=str(user),
                metadata=UserDataExportedMetadata(
                    ticket=_ticket(run), counts=counts, manifest_sha256=manifest_sha256
                ),
            )
            await write_event(session.database, event)
        except BaseException:
            # A failed export leaves no partial package, and no package without its audit row.
            await _remove_on_failure(directory)
            raise
    run.report(
        "exported",
        {
            "userId": str(user),
            "counts": counts.model_dump(by_alias=True),
            "manifestSha256": manifest_sha256,
            "out": str(out),
        },
    )


# --- redact-document ------------------------------------------------------------------------------


def redact_content(content: str, source_ids: set[str], marker: str) -> str:
    """`content` with the body of each named `SOURCE` block replaced by the single line
    `[removed]`; a block already redacted stays as it is."""
    lines = content.split("\n")
    for source_id in sorted(source_ids):
        begin = f"[[BEGIN SOURCE {source_id} {marker}]]"
        end = f"[[END SOURCE {source_id} {marker}]]"
        index = 0
        while index < len(lines):
            if lines[index] != begin:
                index += 1
                continue
            try:
                closing = lines.index(end, index + 1)
            except ValueError:
                break
            lines[index + 1 : closing] = [REDACTED_BODY]
            index += 3
    return "\n".join(lines)


def redacted_trace(item: Mapping[str, Any], document: UUID, now: datetime) -> dict[str, Any] | None:
    """The trace with the document's `SOURCE` block bodies replaced and `redactedAt` set, every
    other field unchanged; `None` when no block is left to change (already redacted, `modelInput`
    null or omitted, or the source never sent).

    The trace model reads which sources were the document's and the block marker; the change is
    made on the stored item itself, so fields a newer release added stay as they are."""
    trace = TurnTraceDocument.from_item(item)
    if trace.retrieval is None or not isinstance(trace.model_input, ModelInput):
        return None
    source_ids = {
        source_id
        for kept in trace.retrieval.kept
        if kept.document_id == document and (source_id := kept.source_id) is not None
    }
    if not source_ids:
        return None
    marker = trace.model_input.marker
    model_input = dict(item["modelInput"])
    messages = model_input["messages"]
    changed = False
    new_messages: list[Any] = []
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            new_messages.append(message)
            continue
        redacted = redact_content(content, source_ids, marker)
        if redacted != content:
            changed = True
            new_messages.append({**message, "content": redacted})
        else:
            new_messages.append(message)
    if not changed:
        return None
    body = without_system_fields(item)
    body["modelInput"] = {**model_input, "messages": new_messages}
    body["redactedAt"] = format_utc(now)
    # The trace model's limits hold for what is written, so the app can still read it.
    TurnTraceDocument.from_item(body).to_item()
    return body


@dataclass(frozen=True)
class TraceHit:
    item_id: str
    user_id: str
    session_id: str


async def find_traces(conversations: CosmosContainer, document_id: str) -> list[TraceHit]:
    hits: list[TraceHit] = []
    parameters = {"@type": TRACE_TYPE, "@documentId": document_id}
    async for page in _query(
        conversations, REDACTION_QUERY, parameters, page_size=REDACTION_PAGE_SIZE
    ):
        hits.extend(
            TraceHit(str(row["id"]), str(row["userId"]), str(row["sessionId"])) for row in page
        )
    return hits


async def redact_trace(
    conversations: CosmosContainer, hit: TraceHit, document: UUID, now: datetime
) -> bool:
    """Redact one trace under its ETag; True when it was written. A `412` re-reads and
    reapplies, at most 3 attempts; a trace still conflicting is exit 6."""
    key = [hit.user_id, hit.session_id]
    for _ in range(REDACTION_ATTEMPTS):
        try:
            item = await conversations.read_item(
                hit.item_id, key, timeout=COSMOS_OPERATION_TIMEOUT_SECONDS
            )
        except CosmosResourceNotFoundError:
            return False
        body = redacted_trace(item, document, now)
        if body is None:
            return False
        try:
            await conversations.replace_item(
                hit.item_id,
                body,
                etag=item.get("_etag"),
                match_condition=MatchConditions.IfNotModified,
                timeout=COSMOS_OPERATION_TIMEOUT_SECONDS,
            )
        except CosmosAccessConditionFailedError:
            continue
        except CosmosResourceNotFoundError:
            return False
        return True
    raise Conflict(
        f"trace {hit.item_id} kept changing over {REDACTION_ATTEMPTS} attempts; traces changed "
        "before it stay changed",
        "run the same command once more to finish the rest",
    )


def _session_pairs(hits: Iterable[TraceHit]) -> list[JsonValue]:
    pairs = sorted({(hit.user_id, hit.session_id) for hit in hits})
    return [{"userId": user_id, "sessionId": session_id} for user_id, session_id in pairs]


async def redact_document(run: Run, args: argparse.Namespace) -> None:
    """Level `user_data`: replace a withdrawn document's text in every stored trace that kept it,
    then the `document.traces_redacted` row; answers and summaries stay unchanged."""
    document: UUID = args.document_id
    document_id = str(document)
    async with user_session(run, cosmos=True) as session:
        conversations = session.containers().conversations
        with _cosmos_errors():
            hits = await find_traces(conversations, document_id)
        run.progress(f"matching turn traces: {len(hits)}")
        await run.confirm(
            [
                f"document: {document_id}",
                f"matching turn traces: {len(hits)}",
                (
                    "change: each of the document's SOURCE block bodies becomes [removed] and "
                    "redactedAt is set; answers, summaries and follow-ups stay unchanged"
                ),
            ]
        )
        changed = 0
        now = run.toolkit.clock.now()
        for hit in hits:
            # An ETag conflict left after 3 attempts leaves as exit 6 (`Conflict`); any other
            # store failure is exit 5 once a trace has changed, its own exit before that.
            try:
                if await redact_trace(conversations, hit, document, now):
                    changed += 1
            except AzureError as error:
                failure = cosmos_failure(error)
                if changed == 0:
                    raise failure from None
                raise Incomplete(
                    f"the redaction stopped part-way ({failure.cause}); {changed} traces changed",
                    RERUN,
                ) from None
        recorded = DocumentTracesRedactedMetadata(
            document_id=document, trace_count=changed, ticket=_ticket(run)
        )
        event = operator_event(
            action="document.traces_redacted",
            actor_object_id=session.actor,
            target_type="document",
            target_id=document_id,
            metadata=recorded,
        )
        try:
            await write_event(session.database, event)
        except Exception as error:  # noqa: BLE001 - a SQL failure after the trace changes
            failure = classify(error)
            if changed:
                raise Incomplete(
                    f"the audit row wasn't written ({failure.cause}); {changed} traces changed",
                    RERUN,
                ) from None
            raise failure from None
    # The report names the document and the trace count as the audit row does.
    summary: dict[str, JsonValue] = recorded.model_dump(
        mode="json", by_alias=True, exclude={"ticket"}
    )
    run.report(
        "redacted",
        {**summary, "tracesFound": len(hits), "sessions": _session_pairs(hits)},
        rows="sessions",
        columns=SESSION_COLUMNS,
    )


# --- deletion-register and audit-events -----------------------------------------------------------


async def deletion_register(run: Run, args: argparse.Namespace) -> None:
    """The `user_data.deleted` and `document.traces_redacted` rows since a restore point, oldest
    first; `--json` gives the list a restore works from. No audit row."""
    since: datetime = args.since
    _refuse_future(run, since)
    async with user_session(run, cosmos=False) as session:
        rows = await session.database.run(
            lambda connection: list(
                connection.execute(DELETION_REGISTER, {"restore_point": _naive(since)}).all()
            ),
            name=REGISTER_UNIT,
            retry="transient",
        )
    register: list[JsonValue] = [
        {
            "action": str(row.action),
            "subject_object_id": _lower(row.subject_object_id),
            "target_id": _lower(row.target_id),
            "occurred_at": utc_text(row.occurred_at),
        }
        for row in rows
    ]
    run.report(
        "listed",
        {"since": format_utc(since), "register": register},
        rows="register",
        columns=REGISTER_COLUMNS,
    )


async def list_audit_events(run: Run, args: argparse.Namespace) -> None:
    """A person's audit rows since a time, newest first, to review their recent admin changes;
    the metadata is content-free by the catalogue's rule. No audit row."""
    actor: UUID = args.actor
    since: datetime = args.since
    _refuse_future(run, since)
    if run.toolkit.clock.now() - since > timedelta(days=AUDIT_LOOKBACK_DAYS):
        raise UsageError(
            f"--since is more than {AUDIT_LOOKBACK_DAYS} days back",
            f"give a time within the last {AUDIT_LOOKBACK_DAYS} days",
        )
    async with user_session(run, cosmos=False) as session:
        rows = await session.database.run(
            lambda connection: list(
                connection.execute(
                    AUDIT_EVENTS_OF_ACTOR, {"actor": actor, "since": _naive(since)}
                ).all()
            ),
            name=AUDIT_EVENTS_UNIT,
            retry="transient",
        )
    events: list[JsonValue] = [
        {
            "occurred_at": utc_text(row.occurred_at),
            "action": str(row.action),
            "target_type": str(row.target_type),
            "target_id": row.target_id,
            "project_id": _lower(row.project_id),
            "metadata_json": row.metadata_json,
        }
        for row in rows
    ]
    run.report(
        "listed",
        {"actor": str(actor), "since": format_utc(since), "events": events},
        rows="events",
        columns=AUDIT_COLUMNS,
    )
