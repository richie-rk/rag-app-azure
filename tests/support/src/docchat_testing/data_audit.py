"""The data-access audits (`84-testing.md` section 2.11; `82-security.md` SEC-18 and SEC-26).

**Cosmos DB.** `CosmosAudit` wraps the container clients the composition root builds and records
every `read_item`, `query_items`, `create_item`, `upsert_item`, `replace_item`, `patch_item`,
`delete_item` and `execute_item_batch` with its container, its partition key and the audit
context that made it: the API operation, or the after-response step. The context comes from the
telemetry the code under test already produces (the server span of the request, the
`docchat.maintenance.post_turn` span of the after-response task), so production code carries no
hook for the audit; a test that calls a store directly names its context with
`CosmosAudit.operation`. `CosmosFaults` wraps one named container so that every call fails as a
connection failure.

**SQL.** `request_paths` reads the `@request_path` registry of `docchat_core.sql.engine` after
importing every repository module, and `PlanAudit` runs each registered function, captures the
statements it sends, and fails a statement whose `SHOWPLAN_XML` plan holds a scan that can read
more than 1,000 rows: index seeks pass, and so do the bounded range scans of T-067, a scan of a
filtered index whose filter admits at most 1,000 rows (the rows a scan of it can read) and a scan
that a `TOP` of at most 1,000 rows reads from directly without a residual predicate.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import pkgutil
import re
import threading
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, cast, override
from uuid import UUID
from xml.etree import ElementTree

from azure.core.exceptions import ServiceRequestError
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider
from opentelemetry.trace import SpanKind
from sqlalchemy import Engine, and_, column, event, func, select, table

from docchat_core.cosmos.containers import CONTAINERS
from docchat_core.sql.engine import REQUEST_PATH_REGISTRY, RequestPathEntry

if TYPE_CHECKING:
    from azure.cosmos.aio import ContainerProxy
    from opentelemetry.context import Context
    from sqlalchemy.engine.interfaces import DBAPICursor, ExecutionContext

# --- Cosmos DB ---------------------------------------------------------------------------------

CHAT_PATH_OPERATIONS: Final = frozenset(
    {
        "createChatTurn",
        "getCitation",
        "getCitationContent",
        "getCitationDocument",
        "getSession",
        "putTurnFeedback",
    }
)
"""The chat path: these operations, and the after-response work `createChatTurn` starts."""

LIST_SESSIONS: Final = "listSessions"
"""The one operation whose `[userId]` prefix query is allowed (the session list)."""

AFTER_RESPONSE_SPAN: Final = "docchat.maintenance.post_turn"
"""The after-response task's span (`13-conversations-and-memory.md` section 6.4 item 7); its
pending re-check is the other allowed prefix query."""

CONVERSATIONS: Final = "conversations"
AUDITED_METHODS: Final = (
    "read_item",
    "query_items",
    "create_item",
    "upsert_item",
    "replace_item",
    "patch_item",
    "delete_item",
    "execute_item_batch",
)
PARTITION_KEY_PATHS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {definition.name: definition.partition_key_paths for definition in CONTAINERS}
)
TURN_TYPE: Final = "turn"
STREAMING: Final = "streaming"
TERMINAL_STATUSES: Final = frozenset({"completed", "failed", "cancelled"})

# A quoted literal in query text: SEC-26 has every value, type and state literal travel as an
# `@` parameter.
_QUOTED_LITERAL_RE: Final = re.compile(r"'[^']*'|\"[^\"]*\"")


@dataclass(frozen=True, slots=True)
class AuditContext:
    """What made a call: an API operation (its operationId), an after-response step, or
    neither."""

    operation: str | None = None
    after_response: str | None = None

    @property
    def chat_path(self) -> bool:
        return self.operation in CHAT_PATH_OPERATIONS or self.after_response is not None

    def describe(self) -> str:
        if self.after_response is not None:
            return f"after-response step {self.after_response}"
        if self.operation is not None:
            return f"operation {self.operation}"
        return "no operation"


@dataclass(frozen=True, slots=True)
class AuditedCall:
    """One recorded container call."""

    container: str
    method: str
    partition_key: tuple[str, ...] | None
    """The key's values, or `None` when the call named none."""
    cross_partition: bool
    query: str | None
    context: AuditContext
    turn_open: bool
    """Whether a turn of the key's user was between `begin_turn` and its terminal status."""

    @property
    def full_key(self) -> bool:
        paths = PARTITION_KEY_PATHS.get(self.container)
        if self.partition_key is None or paths is None:
            return False
        return len(self.partition_key) == len(paths) and all(self.partition_key)

    @property
    def prefix_query(self) -> bool:
        return (
            self.method == "query_items"
            and self.partition_key is not None
            and not self.cross_partition
            and not self.full_key
        )


class SpanTracker(SpanProcessor):
    """Keeps the spans that have started and not ended, so the audit can find a call's
    enclosing request or after-response span."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._live: dict[int, Span] = {}

    @override
    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        with self._lock:
            self._live[span.get_span_context().span_id] = span

    @override
    def on_end(self, span: ReadableSpan) -> None:
        context = span.get_span_context()
        if context is not None:
            with self._lock:
                self._live.pop(context.span_id, None)

    def ancestry(self) -> list[Span]:
        """The current span and its live ancestors, innermost first."""
        span_id = trace.get_current_span().get_span_context().span_id
        chain: list[Span] = []
        with self._lock:
            while span_id and (span := self._live.get(span_id)) is not None:
                chain.append(span)
                span_id = span.parent.span_id if span.parent is not None else 0
        return chain


_TRACKER_LOCK: Final = threading.Lock()
_TRACKERS: dict[int, SpanTracker] = {}


def span_tracker() -> SpanTracker | None:
    """The tracker on the process's SDK tracer provider (the telemetry capture's), attached on
    first use; `None` while no SDK provider is installed, when only explicit contexts count."""
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        return None
    with _TRACKER_LOCK:
        tracker = _TRACKERS.get(id(provider))
        if tracker is None:
            tracker = _TRACKERS[id(provider)] = SpanTracker()
            provider.add_span_processor(tracker)
    return tracker


_EXPLICIT_CONTEXT: Final[ContextVar[AuditContext | None]] = ContextVar(
    "docchat_testing_audit_context", default=None
)


def _key_values(partition_key: object) -> tuple[str, ...] | None:
    if partition_key is None:
        return None
    if isinstance(partition_key, str):
        return (partition_key,)
    if isinstance(partition_key, Sequence):
        return tuple("" if value is None else str(value) for value in partition_key)
    return (str(partition_key),)


def _body_key(container: str, body: object) -> tuple[str, ...] | None:
    paths = PARTITION_KEY_PATHS.get(container)
    if paths is None or not isinstance(body, Mapping):
        return None
    values = [body.get(path.lstrip("/")) for path in paths]
    if any(value is None for value in values):
        return None
    return tuple(str(value) for value in values)


@dataclass(slots=True)
class _Turns:
    """Turns between `begin_turn` (created `streaming`) and their terminal status, by user."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    open: dict[str, set[str]] = field(default_factory=dict)

    def begin(self, user_id: str, turn_id: str) -> None:
        with self.lock:
            self.open.setdefault(user_id, set()).add(turn_id)

    def end(self, user_id: str, turn_id: str) -> None:
        with self.lock:
            self.open.get(user_id, set()).discard(turn_id)

    def any_open(self, user_id: str) -> bool:
        with self.lock:
            return bool(self.open.get(user_id))


class CosmosAudit:
    """The recording wrapper of the Cosmos DB container clients.

    Install it where the composition root builds the containers (`audit(container)` returns the
    wrapped client); every call is recorded in `calls`, every breach of the rules in
    `violations`, and `check()` fails the test naming each breach.
    """

    def __init__(
        self,
        *,
        operations: Mapping[tuple[str, str], str] | None = None,
        tracker: SpanTracker | None = None,
    ) -> None:
        """`operations` maps `(HTTP method, route template)` to the operationId, so a call inside
        a request's server span is attributed to its operation."""
        self._operations = dict(operations or {})
        self._tracker = tracker if tracker is not None else span_tracker()
        self._turns = _Turns()
        self._lock = threading.Lock()
        self.calls: list[AuditedCall] = []
        self.violations: list[str] = []

    def __call__(self, container: ContainerProxy) -> ContainerProxy:
        return cast("ContainerProxy", _AuditedContainer(container, self))

    @contextmanager
    def operation(self, operation_id: str) -> Iterator[None]:
        """Attribute the calls made inside the block to an operation, for a test that calls a
        store without a request."""
        token = _EXPLICIT_CONTEXT.set(AuditContext(operation=operation_id))
        try:
            yield
        finally:
            _EXPLICIT_CONTEXT.reset(token)

    def context(self) -> AuditContext:
        """The context of a call made now: the enclosing after-response span first (its task
        inherits the request's context), then an explicit context, then the request's operation."""
        ancestry = self._tracker.ancestry() if self._tracker is not None else []
        for span in ancestry:
            if span.name == AFTER_RESPONSE_SPAN:
                return AuditContext(after_response=span.name)
        explicit = _EXPLICIT_CONTEXT.get()
        if explicit is not None:
            return explicit
        for span in ancestry:
            if span.kind is SpanKind.SERVER:
                attributes = span.attributes or {}
                method = str(attributes.get("http.request.method", attributes.get("http.method")))
                route = str(attributes.get("http.route"))
                return AuditContext(
                    operation=self._operations.get((method, route), f"{method} {route}")
                )
        return AuditContext()

    def record(
        self,
        container: str,
        method: str,
        *,
        partition_key: tuple[str, ...] | None,
        cross_partition: bool = False,
        query: str | None = None,
    ) -> AuditedCall:
        """Record one call and check it against the rules of section 2.11."""
        user_id = partition_key[0] if partition_key else None
        call = AuditedCall(
            container=container,
            method=method,
            partition_key=partition_key,
            cross_partition=cross_partition,
            query=query,
            context=self.context(),
            turn_open=user_id is not None and self._turns.any_open(user_id),
        )
        breaches = self._breaches(call)
        with self._lock:
            self.calls.append(call)
            self.violations.extend(breaches)
        return call

    def _breaches(self, call: AuditedCall) -> list[str]:
        where = f"{call.method} on {call.container} in {call.context.describe()}"
        breaches: list[str] = []
        if call.query is not None and _QUOTED_LITERAL_RE.search(call.query):
            breaches.append(f"{where}: query text holds a literal instead of a parameter")
        if call.context.chat_path:
            if call.partition_key is None:
                breaches.append(f"{where}: chat-path call without a partition key")
            elif call.cross_partition:
                breaches.append(f"{where}: chat-path call with cross-partition querying")
            elif not call.full_key and not call.prefix_query:
                breaches.append(f"{where}: chat-path call without the full partition key")
        if call.prefix_query:
            allowed = call.container == CONVERSATIONS and (
                call.context.operation == LIST_SESSIONS or call.context.after_response is not None
            )
            if not allowed:
                breaches.append(
                    f"{where}: [userId] prefix query outside listSessions and the "
                    "after-response re-check"
                )
            elif call.turn_open and call.context.after_response is None:
                breaches.append(
                    f"{where}: [userId] prefix query between begin_turn and the terminal event"
                )
        return breaches

    def observe_write(self, container: str, body: object) -> None:
        """Follow the turn window: a turn document written `streaming` opens it, one written with
        a terminal status closes it."""
        if container != CONVERSATIONS or not isinstance(body, Mapping):
            return
        if body.get("type") != TURN_TYPE:
            return
        user_id, turn_id, status = body.get("userId"), body.get("id"), body.get("status")
        if not isinstance(user_id, str) or not isinstance(turn_id, str):
            return
        if status == STREAMING:
            self._turns.begin(user_id, turn_id)
        elif status in TERMINAL_STATUSES:
            self._turns.end(user_id, turn_id)

    def observe_patch(
        self, container: str, item: object, key: tuple[str, ...] | None, operations: object
    ) -> None:
        """A patch that sets a turn's `/status` to a terminal value closes its window."""
        if container != CONVERSATIONS or not key or not isinstance(operations, Sequence):
            return
        turn_id = item if isinstance(item, str) else None
        if turn_id is None and isinstance(item, Mapping):
            turn_id = item.get("id")
        for operation in operations:
            if (
                isinstance(operation, Mapping)
                and operation.get("path") == "/status"
                and operation.get("value") in TERMINAL_STATUSES
                and isinstance(turn_id, str)
            ):
                self._turns.end(key[0], turn_id)

    def user_ids(self) -> set[str]:
        """Every user whose partition a recorded call named, for the isolation suite's check that
        each is the caller's own."""
        return {call.partition_key[0] for call in self.calls if call.partition_key}

    def check(self) -> None:
        """Fail the test naming every breach."""
        if self.violations:
            raise AssertionError(
                "Cosmos DB audit:\n" + "\n".join(f"  {line}" for line in self.violations)
            )


class _AuditedContainer:
    """A container client that records each audited call before delegating it."""

    def __init__(self, inner: ContainerProxy, audit: CosmosAudit) -> None:
        self._inner = inner
        self._audit = audit
        self._name: str = inner.id

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def read_item(self, item: Any, partition_key: Any, **kwargs: Any) -> Any:
        self._audit.record(self._name, "read_item", partition_key=_key_values(partition_key))
        return await self._inner.read_item(item, partition_key, **kwargs)

    def query_items(self, query: str, **kwargs: Any) -> Any:
        self._audit.record(
            self._name,
            "query_items",
            partition_key=_key_values(kwargs.get("partition_key")),
            cross_partition=bool(kwargs.get("enable_cross_partition_query"))
            or kwargs.get("partition_key") is None,
            query=query,
        )
        return self._inner.query_items(query, **kwargs)

    async def create_item(self, body: Any, **kwargs: Any) -> Any:
        self._audit.record(self._name, "create_item", partition_key=_body_key(self._name, body))
        result = await self._inner.create_item(body, **kwargs)
        self._audit.observe_write(self._name, body)
        return result

    async def upsert_item(self, body: Any, **kwargs: Any) -> Any:
        self._audit.record(self._name, "upsert_item", partition_key=_body_key(self._name, body))
        result = await self._inner.upsert_item(body, **kwargs)
        self._audit.observe_write(self._name, body)
        return result

    async def replace_item(self, item: Any, body: Any, **kwargs: Any) -> Any:
        self._audit.record(self._name, "replace_item", partition_key=_body_key(self._name, body))
        result = await self._inner.replace_item(item, body, **kwargs)
        self._audit.observe_write(self._name, body)
        return result

    async def patch_item(
        self, item: Any, partition_key: Any, patch_operations: Any, **kwargs: Any
    ) -> Any:
        key = _key_values(partition_key)
        self._audit.record(self._name, "patch_item", partition_key=key)
        result = await self._inner.patch_item(item, partition_key, patch_operations, **kwargs)
        self._audit.observe_patch(self._name, item, key, patch_operations)
        return result

    async def delete_item(self, item: Any, partition_key: Any, **kwargs: Any) -> Any:
        self._audit.record(self._name, "delete_item", partition_key=_key_values(partition_key))
        return await self._inner.delete_item(item, partition_key, **kwargs)

    async def execute_item_batch(
        self, batch_operations: Any, partition_key: Any, **kwargs: Any
    ) -> Any:
        self._audit.record(
            self._name, "execute_item_batch", partition_key=_key_values(partition_key)
        )
        return await self._inner.execute_item_batch(batch_operations, partition_key, **kwargs)


FAULT_MESSAGE: Final = "connection failed (injected by CosmosFaults)"


class _FailingPages:
    """What `query_items` returns on a faulted container: the SDK's pager does its I/O while it
    is iterated, so that is where the failure surfaces."""

    def __aiter__(self) -> _FailingPages:
        return self

    async def __anext__(self) -> Any:
        raise ServiceRequestError(FAULT_MESSAGE)

    def by_page(self, continuation_token: str | None = None) -> _FailingPages:
        return self


class _FaultyContainer:
    def __init__(self, inner: ContainerProxy) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        if name in AUDITED_METHODS:
            return self._fail
        return getattr(self._inner, name)

    async def _fail(self, *_args: Any, **_kwargs: Any) -> Any:
        raise ServiceRequestError(FAULT_MESSAGE)

    def query_items(self, *_args: Any, **_kwargs: Any) -> _FailingPages:
        return _FailingPages()


class CosmosFaults:
    """Installed like `CosmosAudit`, on one named container: every audited call to it fails as a
    connection failure (`azure.core.exceptions.ServiceRequestError`); the others pass through."""

    def __init__(self, container_name: str) -> None:
        self.container_name = container_name

    def __call__(self, container: ContainerProxy) -> ContainerProxy:
        if container.id != self.container_name:
            return container
        return cast("ContainerProxy", _FaultyContainer(container))


# --- The request-path registry -------------------------------------------------------------------

REPOSITORY_PACKAGE: Final = "docchat_core.sql.repositories"


def request_paths(
    registry: Mapping[str, RequestPathEntry] = REQUEST_PATH_REGISTRY,
) -> Mapping[str, RequestPathEntry]:
    """Every `@request_path` function by qualified name, after importing every repository module,
    since a function registers itself only when its module is imported."""
    if importlib.util.find_spec(REPOSITORY_PACKAGE) is not None:
        package = importlib.import_module(REPOSITORY_PACKAGE)
        for module in pkgutil.walk_packages(package.__path__, f"{REPOSITORY_PACKAGE}."):
            importlib.import_module(module.name)
    return MappingProxyType(dict(registry))


# --- The SQL plan audit ------------------------------------------------------------------------

SHOWPLAN_NAMESPACE: Final = "http://schemas.microsoft.com/sqlserver/2004/07/showplan"
SCAN_OPERATORS: Final = frozenset({"Table Scan", "Clustered Index Scan", "Index Scan"})
MAX_SCANNED_ROWS: Final = 1_000
SHOWPLAN_ON: Final = "SET SHOWPLAN_XML ON"
SHOWPLAN_OFF: Final = "SET SHOWPLAN_XML OFF"

_PARTITIONS = table(
    "partitions", column("object_id"), column("index_id"), column("rows"), schema="sys"
)
_TABLES = table("tables", column("object_id"), column("name"), column("schema_id"), schema="sys")
_INDEXES = table("indexes", column("object_id"), column("index_id"), column("name"), schema="sys")


@dataclass(frozen=True, slots=True)
class PlanScan:
    """One scan operator of a plan, the table and index it reads, and `top_rows`: the row count
    of a `TOP` that bounds what the scan reads (`None` when nothing bounds it)."""

    operator: str
    schema: str
    table: str
    index: str | None
    top_rows: int | None = None


@dataclass(frozen=True, slots=True)
class CapturedStatement:
    statement: str
    parameters: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class PlanFinding:
    """A registered function's statement whose plan scans a table above the limit."""

    function: str
    statement: str
    scan: PlanScan
    rows: int

    def describe(self) -> str:
        index = f" ({self.scan.index})" if self.scan.index else ""
        statement = " ".join(self.statement.split())
        return (
            f"{self.function}: {self.scan.operator} on {self.scan.schema}.{self.scan.table}{index},"
            f" {self.rows} rows: {statement}"
        )


def _unbracket(name: str | None) -> str | None:
    return None if name is None else name.removeprefix("[").removesuffix("]")


def _showplan(name: str) -> str:
    return f"{{{SHOWPLAN_NAMESPACE}}}{name}"


def _scan_element(operator: ElementTree.Element) -> ElementTree.Element | None:
    """A scan operator's own element (`TableScan`, `IndexScan`), the one naming its object."""
    for element in operator:
        if element.find(_showplan("Object")) is not None:
            return element
    return None


def _flag(element: ElementTree.Element, name: str) -> bool:
    return element.get(name) in {"1", "true"}


def _top_bound(operator: ElementTree.Element) -> tuple[ElementTree.Element, int] | None:
    """For a `Top` operator of a constant row count (no `PERCENT`, no `WITH TIES`, no
    `SET ROWCOUNT`): the operator it reads from, past any `Compute Scalar`, and that count."""
    top = operator.find(_showplan("Top"))
    if top is None or any(_flag(top, name) for name in ("IsPercent", "WithTies", "RowCount")):
        return None
    expression = top.find(_showplan("TopExpression"))
    # A count read from a variable or a column isn't a constant bound.
    if expression is None or expression.find(f".//{_showplan('Identifier')}") is not None:
        return None
    constant = expression.find(f".//{_showplan('Const')}")
    value = "" if constant is None else constant.get("ConstValue") or ""
    try:
        # SQL Server writes the constant in parentheses: `(51)`.
        count = int(value.strip("()"))
    except ValueError:
        return None
    child = top.find(_showplan("RelOp"))
    while child is not None and child.get("PhysicalOp") == "Compute Scalar":
        child = child.find(f"{_showplan('ComputeScalar')}/{_showplan('RelOp')}")
    return None if child is None else (child, count)


def _top_bounded_scans(root: ElementTree.Element) -> dict[int, int]:
    """The scans a `TOP` bounds, by element identity: a scan without a residual predicate that a
    `TOP` of N rows reads from directly (past `Compute Scalar` only) returns every row it reads
    and stops when the `TOP` has its N rows, so it reads at most N rows; that is the bounded range
    scan of `84-testing.md` T-067, such as a keyset list's first page read from the start of its
    ordering index."""
    bounded: dict[int, int] = {}
    for operator in root.iter(_showplan("RelOp")):
        if operator.get("PhysicalOp") != "Top" or (bound := _top_bound(operator)) is None:
            continue
        child, count = bound
        element = _scan_element(child)
        if (
            child.get("PhysicalOp") in SCAN_OPERATORS
            and element is not None
            and element.find(_showplan("Predicate")) is None
        ):
            bounded[id(child)] = count
    return bounded


def scans_in_plan(plan_xml: str) -> list[PlanScan]:
    """Every table scan, clustered index scan and index scan of a `SHOWPLAN_XML` plan, each with
    the row count of the `TOP` that bounds it, if one does."""
    # The plan comes from the test's own SQL Server, not from any outside input.
    root = ElementTree.fromstring(plan_xml)  # noqa: S314
    bounded = _top_bounded_scans(root)
    scans: list[PlanScan] = []
    for operator in root.iter(_showplan("RelOp")):
        physical = operator.get("PhysicalOp")
        if physical not in SCAN_OPERATORS:
            continue
        element = _scan_element(operator)
        if element is None:
            continue
        target = element.find(_showplan("Object"))
        if target is not None:
            scans.append(
                PlanScan(
                    operator=physical,
                    schema=_unbracket(target.get("Schema")) or "dbo",
                    table=_unbracket(target.get("Table")) or "",
                    index=_unbracket(target.get("Index")),
                    top_rows=bounded.get(id(operator)),
                )
            )
    return scans


_NO_VALUE: Final = object()


def _int_constant(value: int) -> str:
    kind = "INT" if -(2**31) <= value < 2**31 else "BIGINT"
    return f"CAST({value} AS {kind})"


def _datetime_constant(value: datetime) -> str:
    kind = "DATETIME2(7)" if value.tzinfo is None else "DATETIMEOFFSET(7)"
    return f"CAST('{value.isoformat(timespec='microseconds')}' AS {kind})"


_CONSTANTS: Final[tuple[tuple[type | tuple[type, ...], Callable[[Any], str]], ...]] = (
    # `bool` before `int` and `datetime` before `date`: each is a subclass of the next.
    (bool, lambda value: f"CAST({int(value)} AS BIT)"),
    (int, _int_constant),
    (float, lambda value: f"CAST({value!r} AS FLOAT)"),
    (Decimal, str),
    (str, lambda value: "N'" + value.replace("'", "''") + "'"),
    ((bytes, bytearray, memoryview), lambda value: "0x" + bytes(value).hex()),
    (UUID, lambda value: f"CAST('{value}' AS UNIQUEIDENTIFIER)"),
    (datetime, _datetime_constant),
    (date, lambda value: f"CAST('{value.isoformat()}' AS DATE)"),
    (time, lambda value: f"CAST('{value.isoformat(timespec='microseconds')}' AS TIME(7))"),
)


def sql_constant(value: object) -> str:
    """`value` as a T-SQL constant of the type `pyodbc` binds it as: `NVARCHAR` for text,
    `UNIQUEIDENTIFIER`, `DATETIME2(7)` or `DATETIMEOFFSET(7)`, `VARBINARY`, `BIT`, `INT` or
    `BIGINT`, `FLOAT`, `DECIMAL`, `DATE` and `TIME(7)`."""
    if value is None:
        return "NULL"
    for kind, constant in _CONSTANTS:
        if isinstance(value, kind):
            return constant(value)
    raise TypeError(f"no T-SQL constant for a parameter of type {type(value).__name__}")


def plan_batch(statement: str, parameters: Sequence[Any]) -> str:
    """`statement` with each `?` marker outside quotes, brackets and comments replaced by its
    parameter as a constant, so the batch carries no marker."""
    if not parameters:
        return statement
    values = iter(parameters)
    out: list[str] = []
    index, length = 0, len(statement)
    closers = {"'": "'", '"': '"', "[": "]"}
    while index < length:
        char = statement[index]
        if char in closers:
            closer = closers[char]
            end = index + 1
            while end < length:
                if statement[end] == closer:
                    # A doubled closer is an escaped one inside the quoted text.
                    if end + 1 < length and statement[end + 1] == closer:
                        end += 2
                        continue
                    break
                end += 1
            out.append(statement[index : end + 1])
            index = end + 1
        elif statement.startswith("--", index):
            end = statement.find("\n", index)
            end = length if end == -1 else end
            out.append(statement[index:end])
            index = end
        elif statement.startswith("/*", index):
            end = statement.find("*/", index + 2)
            end = length if end == -1 else end + 2
            out.append(statement[index:end])
            index = end
        elif char == "?":
            try:
                out.append(sql_constant(next(values)))
            except StopIteration:
                raise ValueError("more parameter markers than parameters") from None
            index += 1
        else:
            out.append(char)
            index += 1
    if next(values, _NO_VALUE) is not _NO_VALUE:
        raise ValueError("more parameters than parameter markers")
    return "".join(out)


class PlanAudit:
    """Runs registered request-path functions and audits the plan of every statement they send.

    `plan_engine` is a synchronous engine on the same database (seeded as the plan test seeds
    it); the plans are read on it, outside any transaction, after each function has run.
    Each registered function takes the `SqlEngine` as its first argument and its
    `example_params` as keywords. `exempt` names registry keys that may scan (the full
    `app.ops_config` read). A function that ends in one of the `tolerated` refusals (a state rule
    its example rows don't meet) still has every statement it sent audited.

    Each scan is judged by the rows it can read (`84-testing.md` section 2.11 and T-067: index
    seeks or bounded range scans on tables above 1,000 rows): the rows of the index it reads,
    which for a filtered index are the rows its filter admits, or the row count of a `TOP` that
    bounds it, whichever is smaller.
    """

    def __init__(
        self,
        plan_engine: Engine,
        *,
        registry: Mapping[str, RequestPathEntry] | None = None,
        exempt: Collection[str] = (),
        max_rows: int = MAX_SCANNED_ROWS,
        tolerated: tuple[type[Exception], ...] = (),
    ) -> None:
        self._plan_engine = plan_engine
        self._registry = registry
        self._exempt = frozenset(exempt)
        self._max_rows = max_rows
        self._tolerated = tolerated

    @contextmanager
    def capture(self) -> Iterator[list[CapturedStatement]]:
        """Record every statement any engine of the process sends while the block runs."""
        captured: list[CapturedStatement] = []

        def record(
            _connection: object,
            _cursor: DBAPICursor,
            statement: str,
            parameters: Any,
            _context: ExecutionContext | None,
            executemany: bool,
        ) -> None:
            first = parameters[0] if executemany and parameters else parameters
            values = tuple(first) if isinstance(first, Sequence) else ()
            captured.append(CapturedStatement(statement, values))

        event.listen(Engine, "before_cursor_execute", record)
        try:
            yield captured
        finally:
            event.remove(Engine, "before_cursor_execute", record)

    def plan_of(self, captured: CapturedStatement) -> str | None:
        """The statement's estimated plan, or `None` for a statement without one (a `SET`);
        with `SHOWPLAN_XML` on, nothing runs.

        SQL Server returns no plan for a statement sent with parameter markers (the driver sends
        it as a remote procedure call), and for `sp_executesql` it plans only the `EXEC`; so the
        statement goes as a plain batch with each parameter written as a constant of the type
        the driver binds it as (`plan_batch`), which the optimiser sees as it sees the sniffed
        value of a parameter."""
        batch = plan_batch(captured.statement, captured.parameters)
        with self._plan_engine.connect() as base:
            connection = base.execution_options(isolation_level="AUTOCOMMIT")
            connection.exec_driver_sql(SHOWPLAN_ON)
            try:
                plan = connection.exec_driver_sql(batch)
                if not plan.returns_rows:
                    return None
                return str(plan.scalar_one())
            finally:
                connection.exec_driver_sql(SHOWPLAN_OFF)

    def rows_of(self, scan: PlanScan) -> int:
        """The row count of the index the scan reads (the heap's for a table scan). A clustered
        or unfiltered index holds every row of its table; a filtered index holds the rows its
        filter admits, which are all a scan of it can read."""
        index = _INDEXES.c.index_id == 0 if scan.index is None else _INDEXES.c.name == scan.index
        statement = (
            select(func.coalesce(func.sum(_PARTITIONS.c.rows), 0))
            .select_from(
                _PARTITIONS.join(
                    _INDEXES,
                    and_(
                        _INDEXES.c.object_id == _PARTITIONS.c.object_id,
                        _INDEXES.c.index_id == _PARTITIONS.c.index_id,
                    ),
                ).join(_TABLES, _TABLES.c.object_id == _PARTITIONS.c.object_id)
            )
            .where(
                index,
                _TABLES.c.name == scan.table,
                func.schema_name(_TABLES.c.schema_id) == scan.schema,
            )
        )
        with self._plan_engine.connect() as connection:
            return int(connection.execute(statement).scalar_one())

    def findings_for(
        self, function: str, statements: Sequence[CapturedStatement]
    ) -> list[PlanFinding]:
        """Each scan judged by the rows it can read: its index's rows, or fewer when a `TOP`
        bounds it; above `max_rows` it is a finding."""
        findings: list[PlanFinding] = []
        for captured in statements:
            plan = self.plan_of(captured)
            if plan is None:
                continue
            for scan in scans_in_plan(plan):
                rows = self.rows_of(scan)
                if scan.top_rows is not None:
                    rows = min(rows, scan.top_rows)
                if rows > self._max_rows:
                    findings.append(PlanFinding(function, captured.statement, scan, rows))
        return findings

    async def run(self, engine: object) -> list[PlanFinding]:
        """Run every registered function once with its example parameters and return every
        scan above the limit; an empty list passes."""
        registry = request_paths() if self._registry is None else self._registry
        findings: list[PlanFinding] = []
        for key in sorted(registry):
            if key in self._exempt:
                continue
            entry = registry[key]
            with self.capture() as statements, suppress(*self._tolerated):
                await _call(entry.function, engine, entry.example_params)
            # Reading plans opens connections and waits on the server: off the event loop.
            findings.extend(await asyncio.to_thread(self.findings_for, key, list(statements)))
        return findings


async def _call(function: Callable[..., Any], engine: object, params: Mapping[str, Any]) -> None:
    result = function(engine, **params)
    if hasattr(result, "__await__"):
        await result


def format_plan_findings(findings: Sequence[PlanFinding]) -> str:
    """The plan audit's failure text."""
    lines = [f"SQL plan audit: {len(findings)} request-path statement(s) scan a large table:"]
    lines.extend(f"  {finding.describe()}" for finding in findings)
    return "\n".join(lines)
