"""The audit helper of the API's writes (`80-data-model.md` section 2.21; `82-security.md` SEC-84;
`85-observability.md` sections 4.2 and 4.12; ADR 0029).

A SQL change and its audit row share one transaction, so neither exists without the other: an
operation's unit of work calls `write_audit` for each row on its own connection and returns the
events it wrote; after the commit it calls `report_audit` with them, which sets
`docchat.audit.action` on the request's server span and, for an administrator write, emits one
`admin_change` per row. The events come back from the unit of work rather than being collected on
the side, because a retried unit of work runs its function again and only the committed attempt's
rows exist. A change committed in Cosmos DB can't share a SQL transaction, so its row follows with
`write_audit_after_commit`, which retries and reports a row it can't write instead of raising.

    def work(connection: Connection) -> tuple[ProjectRow, list[AuditEvent]]:
        row = insert_project(connection, ...)
        return row, [write_audit(connection, AuditEvent(action="project.created", ...))]

    row, events = await process.sql.run(work, name="admin_create_project", retry="none")
    report_audit(request, events, admin=True)
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from fastapi import Request
from sqlalchemy import Connection

from docchat_api.http.context import server_span_of
from docchat_api.http.telemetry import record_audit_action, report_admin_change
from docchat_core.audit import AuditEvent, write_audit_event, write_audit_event_after_commit
from docchat_core.sql.engine import SqlEngine

AUDIT_ACTION_STATE: Final = "docchat_audit_action"


def write_audit(connection: Connection, event: AuditEvent) -> AuditEvent:
    """Insert `event`'s row in the caller's transaction (a failed insert fails the change) and
    return it for `report_audit`."""
    write_audit_event(connection, event)
    return event


def _record_action(request: Request, event: AuditEvent) -> None:
    # The span keeps the request's first audited action: the operation's own, written before
    # the rows that follow from it (initial members after `project.created`).
    if getattr(request.state, AUDIT_ACTION_STATE, None) is None:
        setattr(request.state, AUDIT_ACTION_STATE, event.action)
        record_audit_action(server_span_of(request.scope), event.action)


def report_audit(request: Request, events: Iterable[AuditEvent], *, admin: bool) -> None:
    """After the commit: the server span's `docchat.audit.action`, and `admin_change` per row of
    an administrator write."""
    for event in events:
        _record_action(request, event)
        if admin:
            report_admin_change(event.action, event.target_type, event.project_id)


async def write_audit_after_commit(request: Request, engine: SqlEngine, event: AuditEvent) -> bool:
    """The row of a change already committed outside SQL (the memory changes): written in its own
    unit of work with three retries; `False`, after the failure is reported, when it couldn't be."""
    written = await write_audit_event_after_commit(engine, event)
    if written:
        _record_action(request, event)
    return written
