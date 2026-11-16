"""Unit tests of the audit writers (`80-data-model.md` section 2.21 rule 2; ADR 0029 items 6
and 7)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast
from uuid import UUID

import pytest
from opentelemetry.sdk.trace import TracerProvider
from sqlalchemy import Connection
from sqlalchemy.dialects import mssql
from sqlalchemy.sql.dml import Insert

from docchat_core.audit import AuditEvent, write_audit_event, write_audit_event_after_commit
from docchat_core.audit.models import MemoryFactsExtractedMetadata, ProjectCreatedMetadata
from docchat_core.audit.writer import AFTER_COMMIT_RETRY_DELAYS, audit_row, current_correlation_id
from docchat_core.sql.engine import SqlEngine, SqlUnavailable

U = "3f2c0d9e-8b7a-41c6-a5d4-e3f2c1b0a998"
TRACER = TracerProvider().get_tracer("test")
DIALECT = mssql.dialect()  # type: ignore[no-untyped-call]


def project_created() -> AuditEvent:
    return AuditEvent(
        action="project.created",
        actor_type="user",
        actor_object_id=UUID(U),
        target_type="project",
        target_id=U,
        project_id=UUID(U),
        metadata=ProjectCreatedMetadata(is_default=False),
    )


class RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[tuple[Insert, dict[str, Any]]] = []

    def execute(self, statement: Insert, parameters: dict[str, Any]) -> None:
        self.statements.append((statement, parameters))


class FlakyEngine:
    """Fails the first `failures` units with `SqlUnavailable`, then runs them on a recording
    connection."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls: list[tuple[str, str]] = []
        self.connection = RecordingConnection()

    async def run(self, work: Callable[[Connection], None], *, name: str, retry: str) -> None:
        self.calls.append((name, retry))
        if len(self.calls) <= self.failures:
            raise SqlUnavailable("exhausted", 40613, "08S01")
        work(cast("Connection", self.connection))


def test_write_audit_event_inserts_on_the_callers_connection() -> None:
    connection = RecordingConnection()
    with TRACER.start_as_current_span("request") as span:
        write_audit_event(cast("Connection", connection), project_created())
        trace_id = format(span.get_span_context().trace_id, "032x")
    ((statement, parameters),) = connection.statements
    assert str(statement.compile(dialect=DIALECT)).startswith("INSERT INTO app.audit_events")
    assert parameters == {
        "actor_type": "user",
        "actor_object_id": UUID(U),
        "subject_object_id": None,
        "action": "project.created",
        "target_type": "project",
        "target_id": U,
        "project_id": UUID(U),
        "metadata_json": '{"isDefault":false}',
        "correlation_id": trace_id,
    }


def test_no_correlation_outside_a_trace() -> None:
    assert current_correlation_id() is None
    assert audit_row(project_created(), None)["correlation_id"] is None


async def test_after_commit_succeeds_after_retries() -> None:
    engine = FlakyEngine(failures=2)
    sleeps: list[float] = []
    reports: list[AuditEvent] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    with TRACER.start_as_current_span("turn") as span:
        written = await write_audit_event_after_commit(
            cast("SqlEngine", engine), project_created(), sleep=sleep, report=reports.append
        )
        trace_id = format(span.get_span_context().trace_id, "032x")
    assert written is True
    assert sleeps == [0.2, 0.8]
    assert engine.calls == [("write_audit_event", "none")] * 3
    assert reports == []
    ((_, parameters),) = engine.connection.statements
    assert parameters["correlation_id"] == trace_id


async def test_after_commit_reports_and_never_raises() -> None:
    engine = FlakyEngine(failures=10)
    sleeps: list[float] = []
    reports: list[AuditEvent] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    event = AuditEvent(
        action="memory.facts_extracted",
        actor_type="system",
        actor_object_id=UUID(U),
        subject_object_id=UUID(U),
        target_type="memory",
        target_id=U,
        metadata=MemoryFactsExtractedMetadata(
            added_fact_ids=[UUID(U)],
            replaced_fact_ids=[],
            categories=["topic"],
            session_id=UUID(U),
            turn_id=UUID(U),
        ),
    )
    written = await write_audit_event_after_commit(
        cast("SqlEngine", engine), event, sleep=sleep, report=reports.append
    )
    assert written is False
    assert sleeps == list(AFTER_COMMIT_RETRY_DELAYS)
    assert len(engine.calls) == 4
    assert reports == [event]


async def test_after_commit_lets_other_errors_through() -> None:
    class BrokenEngine:
        async def run(self, work: object, *, name: str, retry: str) -> None:
            raise KeyError("bug")

    with pytest.raises(KeyError):
        await write_audit_event_after_commit(cast("SqlEngine", BrokenEngine()), project_created())
