"""The data-access audits: T-566, the Cosmos DB audit meta-test, `CosmosFaults`, the request-path
registry reader, and the plan audit's parts (`84-testing.md` section 2.11). The plan audit
against SQL Server is T-567 in `tests/support/tests/integration/test_plan_audit.py`."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any, cast, override
from uuid import UUID

import pytest
from azure.core.exceptions import ServiceRequestError
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Span, SpanKind, Tracer
from sqlalchemy import Engine, column, create_engine, select, table

from docchat_core.sql.engine import RequestPathEntry
from docchat_testing.data_audit import (
    AFTER_RESPONSE_SPAN,
    AuditContext,
    CapturedStatement,
    CosmosAudit,
    CosmosFaults,
    PlanAudit,
    PlanFinding,
    PlanScan,
    SpanTracker,
    format_plan_findings,
    plan_batch,
    request_paths,
    scans_in_plan,
)

USER = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
SESSION = "0f8fad5b-d9cb-469f-a165-70867728950e"
TURN = "3f2c0d9e-8b7a-41c6-a5d4-e3f2c1b0a998"
OPERATIONS = {
    ("POST", "/v1/chat/turns"): "createChatTurn",
    ("GET", "/v1/sessions"): "listSessions",
    ("GET", "/v1/sessions/{sessionId}"): "getSession",
}
TURN_QUERY = "SELECT * FROM c WHERE c.userId = @userId AND c.type = @type"


class FakeContainer:
    """Stands for an `azure.cosmos.aio.ContainerProxy`: it answers every call and keeps them."""

    def __init__(self, name: str) -> None:
        self.id = name
        self.calls: list[str] = []
        self.throughput = "an unaudited attribute"

    async def read_item(self, item: Any, partition_key: Any, **_: Any) -> dict[str, Any]:
        self.calls.append("read_item")
        return {"id": item}

    def query_items(self, query: str, **_: Any) -> AsyncIterator[dict[str, Any]]:
        self.calls.append("query_items")

        async def pages() -> AsyncIterator[dict[str, Any]]:
            yield {"query": query}

        return pages()

    async def create_item(self, body: Any, **_: Any) -> Any:
        self.calls.append("create_item")
        return body

    async def upsert_item(self, body: Any, **_: Any) -> Any:
        self.calls.append("upsert_item")
        return body

    async def replace_item(self, item: Any, body: Any, **_: Any) -> Any:
        self.calls.append("replace_item")
        return body

    async def patch_item(
        self, item: Any, partition_key: Any, patch_operations: Any, **_: Any
    ) -> Any:
        self.calls.append("patch_item")
        return {"id": item}

    async def delete_item(self, item: Any, partition_key: Any, **_: Any) -> None:
        self.calls.append("delete_item")

    async def execute_item_batch(self, batch_operations: Any, partition_key: Any, **_: Any) -> Any:
        self.calls.append("execute_item_batch")
        return []


@pytest.fixture
def tracker() -> SpanTracker:
    return SpanTracker()


@pytest.fixture
def tracer(tracker: SpanTracker) -> Tracer:
    provider = TracerProvider()
    provider.add_span_processor(tracker)
    return provider.get_tracer("docchat.meta")


@pytest.fixture
def audit(tracker: SpanTracker) -> CosmosAudit:
    return CosmosAudit(operations=OPERATIONS, tracker=tracker)


@pytest.fixture
def conversations(audit: CosmosAudit) -> Any:
    return audit(cast("Any", FakeContainer("conversations")))


def _request(tracer: Tracer, method: str, route: str) -> AbstractContextManager[Span]:
    return tracer.start_as_current_span(
        f"{method} {route}",
        kind=SpanKind.SERVER,
        attributes={"http.request.method": method, "http.route": route},
    )


async def _drain(pages: AsyncIterator[Any]) -> list[Any]:
    return [item async for item in pages]


def _turn(status: str) -> dict[str, Any]:
    return {"id": TURN, "type": "turn", "userId": USER, "sessionId": SESSION, "status": status}


# T-566


async def test_t566_a_chat_path_call_without_the_full_key_fails(
    audit: CosmosAudit, conversations: Any, tracer: Tracer
) -> None:
    with _request(tracer, "GET", "/v1/sessions/{sessionId}"):
        await conversations.read_item(SESSION, partition_key=[USER])
        await conversations.delete_item(TURN, partition_key=None)
        await _drain(conversations.query_items(TURN_QUERY, parameters=[]))

    assert audit.violations == [
        (
            "read_item on conversations in operation getSession: chat-path call without the full "
            "partition key"
        ),
        (
            "delete_item on conversations in operation getSession: chat-path call without a "
            "partition key"
        ),
        (
            "query_items on conversations in operation getSession: chat-path call without a "
            "partition key"
        ),
    ]
    with pytest.raises(AssertionError, match="Cosmos DB audit:\n  read_item on conversations"):
        audit.check()


async def test_t566_a_prefix_query_outside_the_two_fails(
    audit: CosmosAudit, conversations: Any, tracer: Tracer
) -> None:
    with _request(tracer, "POST", "/v1/chat/turns"):
        await _drain(conversations.query_items(TURN_QUERY, partition_key=[USER]))
    with _request(tracer, "GET", "/v1/admin/projects"):
        await _drain(conversations.query_items(TURN_QUERY, partition_key=[USER]))
    await _drain(conversations.query_items(TURN_QUERY, partition_key=[USER]))

    assert audit.violations == [
        (
            "query_items on conversations in operation createChatTurn: [userId] prefix query "
            "outside listSessions and the after-response re-check"
        ),
        (
            "query_items on conversations in operation GET /v1/admin/projects: [userId] prefix "
            "query outside listSessions and the after-response re-check"
        ),
        (
            "query_items on conversations in no operation: [userId] prefix query outside "
            "listSessions and the after-response re-check"
        ),
    ]


async def test_t566_the_session_list_and_the_after_response_recheck_are_allowed(
    audit: CosmosAudit, conversations: Any, tracer: Tracer
) -> None:
    with _request(tracer, "GET", "/v1/sessions"):
        await _drain(conversations.query_items(TURN_QUERY, partition_key=[USER]))
    with (
        _request(tracer, "POST", "/v1/chat/turns"),
        tracer.start_as_current_span(AFTER_RESPONSE_SPAN),
    ):
        await _drain(conversations.query_items(TURN_QUERY, partition_key=[USER]))
        await conversations.read_item(TURN, partition_key=[USER, SESSION])

    assert audit.violations == []
    assert [call.context for call in audit.calls] == [
        AuditContext(operation="listSessions"),
        AuditContext(after_response=AFTER_RESPONSE_SPAN),
        AuditContext(after_response=AFTER_RESPONSE_SPAN),
    ]
    audit.check()


async def test_t566_full_keys_on_the_chat_path_pass(
    audit: CosmosAudit, conversations: Any, tracer: Tracer
) -> None:
    with _request(tracer, "POST", "/v1/chat/turns"):
        await conversations.read_item(SESSION, partition_key=[USER, SESSION])
        await _drain(conversations.query_items(TURN_QUERY, partition_key=[USER, SESSION]))
        await conversations.create_item(_turn("streaming"))
        await conversations.patch_item(TURN, [USER, SESSION], [{"op": "set", "path": "/x"}])
        await conversations.execute_item_batch([], partition_key=[USER, SESSION])

    assert audit.violations == []
    assert audit.user_ids() == {USER}


async def test_a_prefix_query_while_a_turn_streams_fails(
    audit: CosmosAudit, conversations: Any, tracer: Tracer
) -> None:
    await conversations.create_item(_turn("streaming"))
    with _request(tracer, "GET", "/v1/sessions"):
        await _drain(conversations.query_items(TURN_QUERY, partition_key=[USER]))
    with tracer.start_as_current_span(AFTER_RESPONSE_SPAN):
        await _drain(conversations.query_items(TURN_QUERY, partition_key=[USER]))
    await conversations.patch_item(
        TURN, [USER, SESSION], [{"op": "set", "path": "/status", "value": "completed"}]
    )
    with _request(tracer, "GET", "/v1/sessions"):
        await _drain(conversations.query_items(TURN_QUERY, partition_key=[USER]))

    assert audit.violations == [
        (
            "query_items on conversations in operation listSessions: [userId] prefix query between "
            "begin_turn and the terminal event"
        )
    ]
    assert [call.turn_open for call in audit.calls if call.method == "query_items"] == [
        True,
        True,
        False,
    ]


async def test_a_turn_replaced_with_a_terminal_status_closes_its_window(
    audit: CosmosAudit, conversations: Any
) -> None:
    await conversations.upsert_item(_turn("streaming"))
    await conversations.replace_item(TURN, _turn("failed"))

    with audit.operation("listSessions"):
        await _drain(conversations.query_items(TURN_QUERY, partition_key=[USER]))

    assert audit.violations == []


async def test_a_literal_in_query_text_fails(audit: CosmosAudit, conversations: Any) -> None:
    with audit.operation("listSessions"):
        await _drain(
            conversations.query_items(
                "SELECT * FROM c WHERE c.userId = @userId AND c.type = 'turn'",
                partition_key=[USER],
            )
        )

    assert audit.violations == [
        (
            "query_items on conversations in operation listSessions: query text holds a literal "
            "instead of a parameter"
        )
    ]


async def test_the_wrapper_delegates_and_records_every_audited_method(
    audit: CosmosAudit,
) -> None:
    inner = FakeContainer("memories")
    memories: Any = audit(cast("Any", inner))

    await memories.read_item("f", partition_key=USER)
    await _drain(memories.query_items(TURN_QUERY, partition_key=USER))
    await memories.create_item({"id": "f", "userId": USER})
    await memories.upsert_item({"id": "f", "userId": USER})
    await memories.replace_item("f", {"id": "f", "userId": USER})
    await memories.patch_item("f", USER, [])
    await memories.delete_item("f", partition_key=USER)
    await memories.execute_item_batch([], partition_key=USER)

    assert inner.calls == [call.method for call in audit.calls]
    assert {call.partition_key for call in audit.calls} == {(USER,)}
    assert all(call.full_key and not call.prefix_query for call in audit.calls)
    assert memories.throughput == "an unaudited attribute"


async def test_an_explicit_context_names_calls_without_a_request(
    audit: CosmosAudit, conversations: Any
) -> None:
    with audit.operation("getSession"):
        await conversations.read_item(SESSION, partition_key=[USER, SESSION])
    await conversations.read_item(SESSION, partition_key=[USER, SESSION])

    assert [call.context for call in audit.calls] == [
        AuditContext(operation="getSession"),
        AuditContext(),
    ]


def test_without_an_sdk_provider_the_audit_reads_explicit_contexts_only() -> None:
    audit = CosmosAudit(tracker=None)

    with audit.operation("putTurnFeedback"):
        assert audit.context() == AuditContext(operation="putTurnFeedback")
    assert audit.context().chat_path is False


def test_the_tracker_forgets_ended_spans(tracker: SpanTracker, tracer: Tracer) -> None:
    with tracer.start_as_current_span("outer"), tracer.start_as_current_span("inner"):
        assert [span.name for span in tracker.ancestry()] == ["inner", "outer"]

    assert tracker.ancestry() == []


# CosmosFaults


async def test_cosmos_faults_fail_every_call_on_the_named_container() -> None:
    faults = CosmosFaults("usage")
    usage: Any = faults(cast("Any", FakeContainer("usage")))

    with pytest.raises(ServiceRequestError):
        await usage.read_item("x", partition_key=USER)
    with pytest.raises(ServiceRequestError):
        await usage.upsert_item({"id": "x"})
    with pytest.raises(ServiceRequestError):
        await _drain(usage.query_items(TURN_QUERY, partition_key=USER))
    with pytest.raises(ServiceRequestError):
        await _drain(usage.query_items(TURN_QUERY, partition_key=USER).by_page())
    assert usage.throughput == "an unaudited attribute"


async def test_cosmos_faults_leave_other_containers_alone() -> None:
    inner = cast("Any", FakeContainer("conversations"))

    assert CosmosFaults("usage")(inner) is inner


# The request-path registry


def test_request_paths_reads_the_given_registry() -> None:
    async def lookup(engine: object, *, user_id: str) -> None:
        return None

    entry = RequestPathEntry(function=lookup, example_params={"user_id": USER})

    paths = request_paths({"repo.lookup": entry})

    assert dict(paths) == {"repo.lookup": entry}
    with pytest.raises(TypeError):
        paths["other"] = entry  # type: ignore[index]


def test_request_paths_imports_the_repositories_into_the_process_registry() -> None:
    # Whatever the repositories register, each entry names a function and its example values.
    for key, entry in request_paths().items():
        assert key.endswith(entry.function.__qualname__)


# The plan audit's parts

_PLAN = """<?xml version="1.0" encoding="utf-16"?>
<ShowPlanXML xmlns="http://schemas.microsoft.com/sqlserver/2004/07/showplan" Version="1.6">
 <BatchSequence><Batch><Statements><StmtSimple><QueryPlan>
  <RelOp NodeId="0" PhysicalOp="Nested Loops" LogicalOp="Inner Join">
   <NestedLoops>
    <RelOp NodeId="1" PhysicalOp="Clustered Index Scan" LogicalOp="Clustered Index Scan">
     <IndexScan Ordered="0">
      <Object Database="[docchat]" Schema="[app]" Table="[users]" Index="[PK_users]"/>
     </IndexScan>
    </RelOp>
    <RelOp NodeId="2" PhysicalOp="Index Seek" LogicalOp="Index Seek">
     <IndexScan Ordered="1">
      <Object Database="[docchat]" Schema="[app]" Table="[projects]" Index="[IX_projects]"/>
     </IndexScan>
    </RelOp>
    <RelOp NodeId="3" PhysicalOp="Table Scan" LogicalOp="Table Scan">
     <TableScan Ordered="0"><Object Database="[docchat]" Schema="[dbo]" Table="[heap]"/></TableScan>
    </RelOp>
    <RelOp NodeId="4" PhysicalOp="Index Scan" LogicalOp="Index Scan">
     <IndexScan Ordered="0">
      <Object Database="[docchat]" Schema="[app]" Table="[documents]" Index="[IX_documents]"/>
     </IndexScan>
    </RelOp>
   </NestedLoops>
  </RelOp>
 </QueryPlan></StmtSimple></Statements></Batch></BatchSequence>
</ShowPlanXML>"""


def test_the_scans_of_a_plan_are_found() -> None:
    assert scans_in_plan(_PLAN) == [
        PlanScan("Clustered Index Scan", "app", "users", "PK_users"),
        PlanScan("Table Scan", "dbo", "heap", None),
        PlanScan("Index Scan", "app", "documents", "IX_documents"),
    ]


def _scan(table: str, *, predicate: bool = False) -> str:
    residual = "<Predicate><ScalarOperator/></Predicate>" if predicate else ""
    return (
        f'<RelOp PhysicalOp="Index Scan" LogicalOp="Index Scan"><IndexScan Ordered="1">'
        f'<Object Schema="[app]" Table="[{table}]" Index="[IX_{table}]"/>{residual}</IndexScan>'
        "</RelOp>"
    )


def _top(count: str, child: str, **flags: str) -> str:
    attributes = " ".join(f'{name}="{value}"' for name, value in flags.items())
    return (
        f'<RelOp PhysicalOp="Top" LogicalOp="Top"><Top {attributes}>'
        f"<TopExpression><ScalarOperator>{count}</ScalarOperator></TopExpression>{child}</Top>"
        "</RelOp>"
    )


_FIFTY_ONE = '<Const ConstValue="(51)"/>'
_TOP_PLAN = (
    '<ShowPlanXML xmlns="http://schemas.microsoft.com/sqlserver/2004/07/showplan">'
    "<BatchSequence><Batch><Statements><StmtSimple><QueryPlan>"
    '<RelOp PhysicalOp="Nested Loops" LogicalOp="Inner Join"><NestedLoops>'
    # A keyset list's first page: read from the start of its index, cut after 51 rows.
    + _top(_FIFTY_ONE, _scan("first_page"), IsPercent="0", WithTies="0")
    # Bounded too, but by more rows than the limit, past a `Compute Scalar`.
    + _top(
        '<Const ConstValue="(2000)"/>',
        '<RelOp PhysicalOp="Compute Scalar"><ComputeScalar>'
        + _scan("large_page")
        + "</ComputeScalar></RelOp>",
    )
    # Unbounded: the scan reads rows it doesn't return, or all its rows before the `TOP`.
    + _top(_FIFTY_ONE, _scan("filtered", predicate=True))
    + _top(_FIFTY_ONE, '<RelOp PhysicalOp="Sort"><Sort>' + _scan("sorted") + "</Sort></RelOp>")
    + _top(_FIFTY_ONE, _scan("percent"), IsPercent="1")
    + _top('<Identifier><ColumnReference Column="@n"/></Identifier>', _scan("variable"))
    + "</NestedLoops></RelOp></QueryPlan></StmtSimple></Statements></Batch></BatchSequence>"
    "</ShowPlanXML>"
)


def test_a_top_bounds_a_scan_it_reads_directly_without_a_residual_predicate() -> None:
    assert [(scan.table, scan.top_rows) for scan in scans_in_plan(_TOP_PLAN)] == [
        ("first_page", 51),
        ("large_page", 2_000),
        ("filtered", None),
        ("sorted", None),
        ("percent", None),
        ("variable", None),
    ]


def test_a_plan_batch_writes_each_parameter_as_a_constant_of_its_bound_type() -> None:
    statement = (
        "SELECT a FROM app.t WHERE id = ? AND name = ? AND [odd?] = N'what?' AND n IN (?, ?) "
        "AND at < ? AND hash = ? AND flag = ? -- a comment?\nAND gone IS ? /* ? */"
    )
    parameters = (
        UUID(USER),
        "it's",
        7,
        2**40,
        datetime(2026, 11, 7, 6, 1, 2, 345000, tzinfo=UTC).replace(tzinfo=None),
        b"\x01\xff",
        True,
        None,
    )

    expected = (
        "SELECT a FROM app.t WHERE id = <id> AND name = N'it''s' "
        "AND [odd?] = N'what?' AND n IN (CAST(7 AS INT), CAST(1099511627776 AS BIGINT)) "
        "AND at < CAST('2026-11-07T06:01:02.345000' AS DATETIME2(7)) AND hash = 0x01ff "
        "AND flag = CAST(1 AS BIT) -- a comment?\nAND gone IS NULL /* ? */"
    ).replace("<id>", f"CAST('{USER}' AS UNIQUEIDENTIFIER)")
    assert plan_batch(statement, parameters) == expected


def test_a_plan_batch_keeps_a_statement_without_parameters_and_checks_the_count() -> None:
    assert plan_batch("SET NOCOUNT ON", ()) == "SET NOCOUNT ON"
    aware = datetime(2026, 1, 2, tzinfo=UTC)
    assert plan_batch("SELECT ?", (aware,)) == (
        "SELECT CAST('2026-01-02T00:00:00.000000+00:00' AS DATETIMEOFFSET(7))"
    )
    with pytest.raises(ValueError, match="more parameter markers"):
        plan_batch("SELECT ?, ?", (1,))
    with pytest.raises(ValueError, match="more parameters"):
        plan_batch("SELECT ?", (1, 2))
    with pytest.raises(TypeError, match="object"):
        plan_batch("SELECT ?", (object(),))


class CannedPlanAudit(PlanAudit):
    """The plan audit with the server's answers canned: one plan for every statement (the first
    plan above unless another is given) and the given row counts by table."""

    def __init__(
        self, engine: Engine, rows: dict[str, int], *, plan: str = _PLAN, **options: Any
    ) -> None:
        super().__init__(engine, **options)
        self.rows = rows
        self.plan = plan
        self.planned: list[CapturedStatement] = []

    @override
    def plan_of(self, captured: CapturedStatement) -> str:
        self.planned.append(captured)
        return self.plan

    @override
    def rows_of(self, scan: PlanScan) -> int:
        return self.rows.get(scan.table, 0)


_ROWS = table("rows", column("id"))


@pytest.fixture
def sqlite() -> Iterator[Engine]:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE rows (id INTEGER PRIMARY KEY)")
    yield engine
    engine.dispose()


def _registry(sqlite: Engine) -> dict[str, RequestPathEntry]:
    async def read_row(engine: Engine, *, row_id: int) -> None:
        with engine.connect() as connection:
            connection.execute(select(_ROWS.c.id).where(_ROWS.c.id == row_id)).all()

    return {"repo.read_row": RequestPathEntry(function=read_row, example_params={"row_id": 7})}


async def test_a_statement_scanning_a_large_table_is_a_finding(sqlite: Engine) -> None:
    audit = CannedPlanAudit(
        sqlite, {"users": 2_000, "heap": 1_000, "documents": 50}, registry=_registry(sqlite)
    )

    findings = await audit.run(sqlite)

    (captured,) = audit.planned
    assert captured.parameters == (7,)
    assert "FROM rows" in captured.statement
    assert findings == [
        PlanFinding(
            "repo.read_row",
            captured.statement,
            PlanScan("Clustered Index Scan", "app", "users", "PK_users"),
            2_000,
        )
    ]
    assert format_plan_findings(findings).splitlines() == [
        "SQL plan audit: 1 request-path statement(s) scan a large table:",
        (
            f"  repo.read_row: Clustered Index Scan on app.users (PK_users), 2000 rows: "
            f"{' '.join(captured.statement.split())}"
        ),
    ]


async def test_scans_of_small_tables_pass(sqlite: Engine) -> None:
    audit = CannedPlanAudit(sqlite, {"users": 1_000}, registry=_registry(sqlite))

    assert await audit.run(sqlite) == []


async def test_a_scan_is_judged_by_the_rows_a_top_lets_it_read(sqlite: Engine) -> None:
    tables = ("first_page", "large_page", "filtered", "sorted", "percent", "variable")
    audit = CannedPlanAudit(
        sqlite, dict.fromkeys(tables, 50_000), registry=_registry(sqlite), plan=_TOP_PLAN
    )

    findings = await audit.run(sqlite)

    assert [(finding.scan.table, finding.rows) for finding in findings] == [
        ("large_page", 2_000),
        ("filtered", 50_000),
        ("sorted", 50_000),
        ("percent", 50_000),
        ("variable", 50_000),
    ]


class _StateRuleRefusedError(Exception):
    pass


def _refusing_registry(sqlite: Engine, error: Exception) -> dict[str, RequestPathEntry]:
    async def read_then_refuse(engine: Engine, *, row_id: int) -> None:
        with engine.connect() as connection:
            connection.execute(select(_ROWS.c.id).where(_ROWS.c.id == row_id)).all()
        raise error

    return {
        "repo.refuse": RequestPathEntry(function=read_then_refuse, example_params={"row_id": 9})
    }


async def test_a_tolerated_refusal_still_has_its_statements_audited(sqlite: Engine) -> None:
    audit = CannedPlanAudit(
        sqlite,
        {"users": 2_000},
        registry=_refusing_registry(sqlite, _StateRuleRefusedError()),
        tolerated=(_StateRuleRefusedError,),
    )

    findings = await audit.run(sqlite)

    assert [finding.function for finding in findings] == ["repo.refuse"]
    assert [captured.parameters for captured in audit.planned] == [(9,)]


async def test_any_other_failure_of_a_function_fails_the_audit(sqlite: Engine) -> None:
    audit = CannedPlanAudit(
        sqlite,
        {"users": 2_000},
        registry=_refusing_registry(sqlite, ValueError("not a refusal")),
        tolerated=(_StateRuleRefusedError,),
    )

    with pytest.raises(ValueError, match="not a refusal"):
        await audit.run(sqlite)


async def test_an_exempt_function_is_not_run(sqlite: Engine) -> None:
    audit = CannedPlanAudit(
        sqlite, {"users": 2_000}, registry=_registry(sqlite), exempt={"repo.read_row"}
    )

    assert await audit.run(sqlite) == []
    assert audit.planned == []


def test_capture_records_only_inside_its_block(sqlite: Engine) -> None:
    audit = PlanAudit(sqlite, registry={})

    with audit.capture() as captured, sqlite.connect() as connection:
        connection.execute(select(_ROWS.c.id).where(_ROWS.c.id == 3)).all()
    with sqlite.connect() as connection:
        connection.execute(select(_ROWS.c.id)).all()

    assert [statement.parameters for statement in captured] == [(3,)]
