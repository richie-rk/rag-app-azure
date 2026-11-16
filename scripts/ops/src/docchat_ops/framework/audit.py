"""Correlation and audit rows of an operator run (`86-operations.md` section 2.5; `80-data-model.md`
section 2.21; ADR 0139 item 5).

Each run draws one W3C trace ID and makes it the current span context, so
`docchat_core.audit.write_audit_event` stores it as `audit_events.correlation_id` without the
command passing it; the CLI configures no telemetry, so no span is ever exported. Audit rows are
`AuditEvent` values with `actor_type` `operator`, whose actor is the `oid` of the caller's SQL
access token.
"""

from __future__ import annotations

import secrets
from contextlib import contextmanager
from typing import TYPE_CHECKING, Final, Protocol

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags
from sqlalchemy import literal, select

from docchat_core.audit import AuditEvent, write_audit_event
from docchat_core.settings import LocalSettings
from docchat_core.sql.engine import SqlEngine
from docchat_core.sql.settings import SqlSettings
from docchat_ops.framework.auth import token_object_id
from docchat_ops.framework.exitcodes import SIGN_IN, AccessDenied

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from azure.core.credentials_async import AsyncTokenCredential
    from sqlalchemy import Connection

    from docchat_core.audit import AuditAction, AuditMetadata, AuditTargetType
    from docchat_core.sql.engine import Retry
    from docchat_ops.framework.endpoints import SqlTarget

TRACE_ID_BYTES: Final = 16
SPAN_ID_BYTES: Final = 8
OPERATOR: Final = "operator"

CONNECTION_CHECK: Final = "ops_connection_check"
WRITE_AUDIT_EVENT: Final = "write_audit_event"

# The CLI holds one connection at a time; two threads is the layer's minimum. The timeouts are
# the layer's defaults, set here so an operator's shell variables can't change them.
OPS_SQL_THREADS: Final = 2
OPS_SQL_POOL_TIMEOUT_SECONDS: Final = 10
OPS_SQL_CONNECT_TIMEOUT_SECONDS: Final = 30
OPS_SQL_COMMAND_TIMEOUT_SECONDS: Final = 30
OPS_SQL_IDLE_RELEASE_MINUTES: Final = 10


def new_correlation_id(draw: Callable[[int], str] = secrets.token_hex) -> str:
    """32 lower-case hexadecimal characters, never all zero (an invalid W3C trace ID)."""
    while True:
        candidate = draw(TRACE_ID_BYTES)
        if int(candidate, 16):
            return candidate


@contextmanager
def correlation_scope(correlation_id: str) -> Iterator[None]:
    """Make the run's trace ID the current span context for the duration of the run."""
    span_id = 0
    while not span_id:
        span_id = int(secrets.token_hex(SPAN_ID_BYTES), 16)
    span = NonRecordingSpan(
        SpanContext(
            trace_id=int(correlation_id, 16),
            span_id=span_id,
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.DEFAULT),
        )
    )
    token = otel_context.attach(trace.set_span_in_context(span))
    try:
        yield
    finally:
        otel_context.detach(token)


class OpsDatabase(Protocol):
    """The part of `docchat_core.sql.engine.SqlEngine` a command uses."""

    async def ensure_token(self) -> str | None: ...

    async def run[T](self, work: Callable[[Connection], T], *, name: str, retry: Retry) -> T: ...

    async def dispose(self) -> None: ...


def ops_database(target: SqlTarget, credential: AsyncTokenCredential) -> OpsDatabase:
    """The data-access layer for one environment's database as the operator: an Entra token for
    `https://database.windows.net/.default`, `Encrypt=strict` and `APP=docchat-ops`."""
    sql = SqlSettings(
        sql_server=target.server,
        sql_database="docchat",
        sql_threads=OPS_SQL_THREADS,
        sql_pool_timeout_seconds=OPS_SQL_POOL_TIMEOUT_SECONDS,
        sql_connect_timeout_seconds=OPS_SQL_CONNECT_TIMEOUT_SECONDS,
        sql_command_timeout_seconds=OPS_SQL_COMMAND_TIMEOUT_SECONDS,
        sql_idle_release_minutes=OPS_SQL_IDLE_RELEASE_MINUTES,
    )
    # Never the compose services, whatever the operator's shell holds.
    local = LocalSettings(local_emulators=False, local_sql_sa_password=None)
    return SqlEngine("ops", sql=sql, local=local, credential=credential)


def _check(connection: Connection) -> int:
    return int(connection.execute(select(literal(1))).scalar_one())


async def connect(database: OpsDatabase) -> str:
    """Open a connection as the operator and return the actor of the run's audit rows: the `oid`
    of the SQL access token."""
    token = await database.ensure_token()
    if token is None:
        raise AccessDenied("no access token for SQL", SIGN_IN)
    actor = token_object_id(token)
    await database.run(_check, name=CONNECTION_CHECK, retry="transient")
    return actor


def operator_event(
    *,
    action: AuditAction,
    actor_object_id: str,
    target_type: AuditTargetType,
    target_id: str | None,
    metadata: AuditMetadata | None,
    subject_object_id: str | None = None,
    project_id: str | None = None,
) -> AuditEvent:
    """An audit row of an operator command, validated against the catalogue."""
    return AuditEvent.model_validate(
        {
            "action": action,
            "actor_type": OPERATOR,
            "actor_object_id": actor_object_id,
            "subject_object_id": subject_object_id,
            "target_type": target_type,
            "target_id": target_id,
            "project_id": project_id,
            "metadata": metadata,
        }
    )


async def write_event(database: OpsDatabase, event: AuditEvent) -> None:
    """Insert one row in a transaction of its own, never retried: a row that records a change
    already made elsewhere must not be written twice."""

    def insert(connection: Connection) -> None:
        write_audit_event(connection, event)

    await database.run(insert, name=WRITE_AUDIT_EVENT, retry="none")
