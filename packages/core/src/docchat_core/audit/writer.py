"""Writing audit rows (`80-data-model.md` section 2.21 rule 2; `82-security.md` SEC-84;
ADR 0029 items 6 and 7).

A SQL change and its row share the caller's transaction, so neither exists without the other.
A change committed in Cosmos DB can't share one, so its row follows in a unit of work of its own,
retried three times, and a row that still can't be written is reported instead of raised: the
change it records is already committed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any, Final

from opentelemetry import trace
from sqlalchemy import Connection

from docchat_core.audit import telemetry
from docchat_core.audit.models import AuditEvent
from docchat_core.sql.engine import SqlEngine, SqlError

# Imported as a module: the access tables' check constraints read the audit catalogue, so this
# package can be loaded while the table module is still being created.
from docchat_core.sql.tables import access as access_tables

AFTER_COMMIT_RETRY_DELAYS: Final = (0.2, 0.8, 2.0)
"""The waits before the three retries of `write_audit_event_after_commit`, in seconds."""

UNIT_OF_WORK: Final = "write_audit_event"


def current_correlation_id() -> str | None:
    """The 32-character trace ID of the current span, or `None` outside a trace."""
    context = trace.get_current_span().get_span_context()
    return format(context.trace_id, "032x") if context.is_valid else None


def audit_row(event: AuditEvent, correlation_id: str | None) -> dict[str, Any]:
    """The bound values of one `app.audit_events` insert; the database fills `id` and
    `occurred_at`."""
    return {
        "actor_type": event.actor_type,
        "actor_object_id": event.actor_object_id,
        "subject_object_id": event.subject_object_id,
        "action": event.action,
        "target_type": event.target_type,
        "target_id": event.target_id,
        "project_id": event.project_id,
        "metadata_json": event.metadata_json(),
        "correlation_id": correlation_id,
    }


def _insert(connection: Connection, event: AuditEvent, correlation_id: str | None) -> None:
    connection.execute(access_tables.audit_events.insert(), audit_row(event, correlation_id))


def write_audit_event(connection: Connection, event: AuditEvent) -> None:
    """Insert one row on the caller's connection, inside the caller's transaction: a failed
    insert fails the change it records."""
    _insert(connection, event, current_correlation_id())


async def write_audit_event_after_commit(
    engine: SqlEngine,
    event: AuditEvent,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    report: Callable[[AuditEvent], None] = telemetry.report_write_failure,
) -> bool:
    """Write the row of a change already committed outside SQL; `False` when four attempts
    failed, after the failure is reported. Never raises a SQL error."""
    # Taken once, on the caller's span, so every attempt carries the same correlation ID.
    correlation_id = current_correlation_id()
    work = partial(_insert, event=event, correlation_id=correlation_id)
    for delay in (0.0, *AFTER_COMMIT_RETRY_DELAYS):
        if delay:
            await sleep(delay)
        try:
            await engine.run(work, name=UNIT_OF_WORK, retry="none")
        except SqlError:
            continue
        return True
    report(event)
    return False
