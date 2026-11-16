"""Run finalisation on a scripted connection (`14-ingestion.md` section 6.13; the effect on SQL
Server is part of T-161)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from docchat_core.ingestion.models import IndexStatus, RunKind, RunState
from docchat_ingestion.activities import finalisation

INDEX = UUID("3f2c0d9e-8b7a-41c6-a5d4-e3f2c1b0a998")
RUN = UUID("2d3e4f5a-6b7c-4d8e-9f0a-1b2c3d4e5f60")
CREATED = datetime(2026, 1, 1, 12, 0, tzinfo=UTC).replace(tzinfo=None)
"""SQL reads `DATETIME2(3)` back as naive UTC."""


def test_finalisable_runs_skip_rebuild_runs_a_building_index_names(fakes: SimpleNamespace) -> None:
    connection = fakes.FakeSql(
        fakes.Rows([fakes.row(id=RUN, kind="upload", search_index_id=INDEX, traceparent=None)])
    ).connection

    (run,) = finalisation.finalisable_runs(connection)

    assert run.kind is RunKind.UPLOAD
    sql = connection.executed[0].sql
    assert "kind != N'index_rebuild'" in sql
    assert "build_run_id = app.ingestion_runs.id" in sql
    assert "ORDER BY app.ingestion_runs.created_at" in sql


def _finish(
    fakes: SimpleNamespace, kind: RunKind, states: dict[str, int], *after: Any
) -> tuple[Any, Any]:
    connection = fakes.FakeSql(
        fakes.Rows([fakes.row(state=state, items=count) for state, count in states.items()]),
        fakes.Rows([fakes.row(created_at=CREATED, completed_at=CREATED + timedelta(seconds=90))]),
        *after,
    ).connection
    run = finalisation.FinalisableRun(
        RUN, kind, INDEX, "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    )
    return finalisation.finalise(connection, run), connection


def test_an_upload_run_ends_with_its_totals_and_recomputes_a_ready_index(
    fakes: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        finalisation, "compute_index_status", lambda connection, index: IndexStatus.READY
    )
    finished, connection = _finish(
        fakes, RunKind.UPLOAD, {"succeeded": 3, "failed": 1, "cancelled": 1}, fakes.Rows(rowcount=1)
    )

    assert finished is not None
    assert finished.state is RunState.COMPLETED_WITH_ERRORS
    assert (finished.total, finished.succeeded, finished.failed, finished.cancelled) == (5, 3, 1, 1)
    assert finished.duration_seconds == 90
    update_run = connection.executed[1]
    assert "OUTPUT inserted.created_at, inserted.completed_at" in update_run.sql
    assert update_run.params["total_items"] == 5
    status = connection.executed[2]
    assert "status IN (N'ready', N'reingest_required')" in status.sql


def test_an_index_delete_run_marks_the_index_deleted(fakes: SimpleNamespace) -> None:
    _, connection = _finish(fakes, RunKind.INDEX_DELETE, {"succeeded": 2}, fakes.Rows(rowcount=1))

    status = connection.executed[-1]
    assert "status IN (N'deleting')" in status.sql
    assert status.params["status"] == "deleted"


def test_a_failed_index_create_marks_the_index_failed(fakes: SimpleNamespace) -> None:
    _, connection = _finish(
        fakes,
        RunKind.INDEX_CREATE,
        {"failed": 1},
        fakes.Rows([fakes.row(id=RUN)]),
        fakes.Rows(rowcount=1),
    )

    status = connection.executed[-1]
    assert status.params["status"] == "failed"
    assert "status IN (N'provisioning')" in status.sql


def test_a_successful_index_create_leaves_the_status(fakes: SimpleNamespace) -> None:
    _, connection = _finish(fakes, RunKind.INDEX_CREATE, {"succeeded": 1}, fakes.Rows([]))

    assert not any("search_indexes SET" in item.sql for item in connection.executed)


def test_a_maintenance_run_changes_no_status(fakes: SimpleNamespace) -> None:
    finished, connection = _finish(fakes, RunKind.MAINTENANCE, {})

    assert finished is not None
    assert finished.state is RunState.COMPLETED
    assert len(connection.executed) == 2


def test_a_run_another_writer_ended_is_left(fakes: SimpleNamespace) -> None:
    connection = fakes.FakeSql(fakes.Rows([]), fakes.Rows([])).connection
    run = finalisation.FinalisableRun(RUN, RunKind.UPLOAD, INDEX, None)

    assert finalisation.finalise(connection, run) is None
