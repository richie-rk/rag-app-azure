"""`ingestion status`, `requeue-item`, `fail-item`, `cancel-run`, `abandon-rebuild`,
`set-deployments` and `drain-poison` (T-541, T-542; `86-operations.md` section 2.8.6 and acceptance
criterion 26; `14-ingestion.md` sections 6.11, 8.10 and 19; `80-data-model.md` sections 2.21 and
8.6).

Every command runs through the real parser against the scripted SQL layer, the faked poison queue
and the faked Azure CLI of `conftest.py`. The behaviour against SQL Server under
`docchat_config_operator` is the maintenance integration test's (T-199).
"""

from __future__ import annotations

import argparse
import base64
import json
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pytest
from azure.core.exceptions import (
    AzureError,
    HttpResponseError,
    ServiceRequestError,
)
from sqlalchemy import Update
from sqlalchemy.sql.dml import Insert

from docchat_core.errors import ITEM_CODES
from docchat_core.ingestion.models import IngestionQueueMessage, TraceContext
from docchat_core.ingestion.settings import DEFAULT_EMBEDDING_MODELS_JSON
from docchat_core.sql.engine import SqlDeadlock, SqlUnavailable, SqlUniqueViolation
from docchat_core.sql.tables.ingestion import ingestion_run_items
from docchat_ops.commands import ingestion
from docchat_ops.framework.auth import cli_credential
from docchat_ops.framework.endpoints import AzdEnvironment
from docchat_ops.framework.exitcodes import (
    AccessDenied,
    ExitCode,
    NotFound,
    UnexpectedError,
    Unreachable,
    UsageError,
)

if TYPE_CHECKING:
    from ops.tests.unit.conftest import OpsHarness, Outcome

TICKET = ("--ticket", "INC-2201", "--reason", "recover a stuck ingestion item")
INDEX_ID = UUID("3f2c0d9e-8b7a-41c6-a5d4-e3f2c1b0a998")
RUN_ID = UUID("6c1f3e2a-9b4d-4f0e-8a71-2d5c9e0b7f13")
ITEM_ID = UUID("0b8d7c6e-5f4a-4b3c-9d2e-1f0a9b8c7d6e")
PHYSICAL_ID = UUID("9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d")
OTHER_PHYSICAL_ID = UUID("1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d")
CANARY_ID = UUID("5e6f7a8b-9c0d-4e1f-8a2b-3c4d5e6f7a8b")
ROW_VERSION = b"\x00\x00\x00\x00\x00\x00\x07\xd1"
DB_NOW = datetime(2026, 10, 30, 12, 0)  # noqa: DTZ001 - DATETIME2 holds UTC without a zone
EXPIRED = datetime(2026, 10, 30, 11, 30)  # noqa: DTZ001
VALID = datetime(2026, 10, 30, 12, 20)  # noqa: DTZ001
TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
QUEUE_ENDPOINT = "https://stdocchatdevabc123.queue.core.windows.net/"


def _run(ops: OpsHarness, *argv: str, environment: str = "dev") -> Outcome:
    return ops.run("--env", environment, "--json", "ingestion", *argv, *TICKET, "--yes")


def _metadata(ops: OpsHarness) -> dict[str, Any]:
    (row,) = ops.database.audit_rows
    metadata: dict[str, Any] = json.loads(row["metadata_json"])
    return metadata


def _audit(ops: OpsHarness) -> dict[str, Any]:
    (row,) = ops.database.audit_rows
    return row


def _http_error(status: int, code: str | None = None) -> HttpResponseError:
    error = HttpResponseError(message="refused")
    error.status_code = status
    error.error_code = code  # type: ignore[attr-defined]
    return error


# --- shared helpers -------------------------------------------------------------------------------


def test_identifier_and_deployment_arguments() -> None:
    assert ingestion.identifier(str(INDEX_ID).upper()) == INDEX_ID
    with pytest.raises(argparse.ArgumentTypeError):
        ingestion.identifier("not-a-uuid")
    assert ingestion.deployment("embed-query-te3-large-v1") == "embed-query-te3-large-v1"
    for bad in ("x", "has space", "a" * 65, "semi;colon"):
        with pytest.raises(argparse.ArgumentTypeError):
            ingestion.deployment(bad)


def test_times_are_utc_text_and_whole_seconds() -> None:
    assert ingestion.utc_text(DB_NOW) == "2026-10-30T12:00:00.000Z"
    assert ingestion.utc_text(None) is None
    assert ingestion.age_seconds(EXPIRED, DB_NOW) == 1800
    assert ingestion.age_seconds(VALID, DB_NOW) == 0
    assert ingestion.age_seconds(None, DB_NOW) is None


def _environment(**values: str) -> AzdEnvironment:
    return AzdEnvironment("dev", dict(values))


def test_ledger_values_are_validated_by_the_apps_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The operator's shell never reaches the catalogue.
    monkeypatch.setenv("DOCCHAT_OPENAI_EMBEDDING_MODELS", "not json")
    good = _environment(
        DOCCHAT_SEARCH_MAX_INDEXES="50",
        DOCCHAT_OPENAI_EMBEDDING_MODELS=DEFAULT_EMBEDDING_MODELS_JSON,
    )
    assert ingestion.max_indexes(good) == 50
    assert ingestion.embedding_catalog(good).find("text-embedding-3-large") is not None
    for value in ("1", "201", "many"):
        with pytest.raises(UsageError, match="isn't 2 to 200"):
            ingestion.max_indexes(_environment(DOCCHAT_SEARCH_MAX_INDEXES=value))
    for value in ("[", "[]", '[{"model": "x"}]', DEFAULT_EMBEDDING_MODELS_JSON.replace("]", ",1]")):
        with pytest.raises(UsageError, match="isn't an embedding catalogue"):
            ingestion.embedding_catalog(_environment(DOCCHAT_OPENAI_EMBEDDING_MODELS=value))


@pytest.mark.parametrize(
    "value",
    [
        "http://stdocchat.queue.core.windows.net/",
        "https://localhost:10001/",
        "https://127.0.0.1/",
        "not a url",
        "https://user:pw@stdocchat.queue.core.windows.net/",
    ],
)
def test_storage_endpoints_must_be_secure_remote_urls(value: str) -> None:
    with pytest.raises(UsageError, match="isn't an https endpoint"):
        ingestion.storage_endpoint(
            _environment(DOCCHAT_STORAGE_QUEUE_ENDPOINT=value), "DOCCHAT_STORAGE_QUEUE_ENDPOINT"
        )


def test_storage_failures_map_to_their_exit_codes() -> None:
    assert isinstance(
        ingestion.storage_failure(ServiceRequestError("down"), "Queue Storage", "config"),
        Unreachable,
    )
    firewall = ingestion.storage_failure(
        _http_error(403, "AuthorizationFailure"), "Blob Storage", "user_data"
    )
    assert isinstance(firewall, Unreachable)
    assert "firewall" in firewall.cause
    denied = ingestion.storage_failure(
        _http_error(403, "AuthorizationPermissionMismatch"), "Blob Storage", "user_data"
    )
    assert isinstance(denied, AccessDenied)
    assert "user_data" in denied.next_step
    assert isinstance(
        ingestion.storage_failure(_http_error(401), "Blob Storage", "user_data"), AccessDenied
    )
    assert isinstance(
        ingestion.storage_failure(_http_error(404), "Queue Storage", "config"), NotFound
    )
    assert isinstance(
        ingestion.storage_failure(AzureError("other"), "Queue Storage", "config"),
        UnexpectedError,
    )


def _message(run_id: UUID) -> str:
    message = IngestionQueueMessage.for_run(run_id, TraceContext(TRACEPARENT))
    return base64.b64encode(message.encode().encode("utf-8")).decode("ascii")


def test_run_id_of_reads_only_a_valid_queue_message() -> None:
    assert ingestion.run_id_of(_message(RUN_ID)) == str(RUN_ID)
    assert ingestion.run_id_of("%%% not base64") is None
    assert ingestion.run_id_of(base64.b64encode(b"\xff\xfe").decode()) is None
    assert ingestion.run_id_of(base64.b64encode(b'{"runId": "x"}').decode()) is None


# --- status ---------------------------------------------------------------------------------------


def _script_status(ops: OpsHarness, *, empty: bool = False) -> None:
    database = ops.database
    database.answer({"now": DB_NOW})
    if empty:
        database.answer()
        database.answer()
        database.answer()
        database.answer()
        database.answer()
        return
    database.answer(
        {
            "generation": 3,
            "last_sweep_at": datetime(2026, 10, 30, 11, 59, 30),  # noqa: DTZ001
            "last_claim_at": None,
            "running_items": 1,
            "updated_at": datetime(2026, 10, 30, 11, 59, 30),  # noqa: DTZ001
        }
    )
    database.answer(
        {"priority": 0, "items": 2, "oldest": datetime(2026, 10, 30, 11, 50)},  # noqa: DTZ001
        {"priority": 1, "items": 7, "oldest": datetime(2026, 10, 30, 11, 40)},  # noqa: DTZ001
    )
    database.answer({"priority": 1, "items": 1})
    database.answer(
        {
            "id": RUN_ID,
            "kind": "index_rebuild",
            "search_index_id": INDEX_ID,
            "priority": 1,
            "state": "running",
            "created_at": datetime(2026, 10, 30, 11, 40),  # noqa: DTZ001
            "started_at": datetime(2026, 10, 30, 11, 41),  # noqa: DTZ001
        }
    )
    database.answer(
        {
            "id": CANARY_ID,
            "written_at": datetime(2026, 10, 30, 2, 40),  # noqa: DTZ001
            "result": "clean",
            "scanned_at": datetime(2026, 10, 30, 2, 52),  # noqa: DTZ001
            "minutes_to_result": 12,
        }
    )


def test_t542_status_is_read_only_and_writes_no_audit_row(ops: OpsHarness) -> None:
    _script_status(ops)
    outcome = ops.run("--env", "dev", "--json", "ingestion", "status")
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    report = outcome.json()
    assert report["result"] == "listed"
    assert report["dispatcher"] == {
        "generation": 3,
        "lastSweepAt": "2026-10-30T11:59:30.000Z",
        "lastClaimAt": None,
        "runningItems": 1,
        "updatedAt": "2026-10-30T11:59:30.000Z",
        "heartbeatAgeSeconds": 30,
    }
    assert report["queued"] == {"interactive": 2, "bulk": 7}
    assert report["running"] == {"interactive": 0, "bulk": 1}
    assert report["oldestQueuedAgeSeconds"] == 1200
    assert report["openRuns"] == [
        {
            "runId": str(RUN_ID),
            "kind": "index_rebuild",
            "indexId": str(INDEX_ID),
            "priority": "bulk",
            "state": "running",
            "createdAt": "2026-10-30T11:40:00.000Z",
            "startedAt": "2026-10-30T11:41:00.000Z",
        }
    ]
    assert report["canaries"] == [
        {
            "canaryId": str(CANARY_ID),
            "writtenAt": "2026-10-30T02:40:00.000Z",
            "result": "clean",
            "scannedAt": "2026-10-30T02:52:00.000Z",
            "minutesToResult": 12,
        }
    ]
    assert ops.database.units == [("ingestion_status_read", "transient")]
    assert ops.database.audit_rows == []
    assert ops.database.disposed
    assert "Dry run" not in outcome.stderr
    assert ops.az.calls == []
    assert not ops.database.script
    sql = ops.database.sql("ingestion_status_read")
    assert "state = N'queued'" in sql[2]
    assert "state = N'running'" in sql[3]
    assert sql[5].startswith("SELECT TOP ")
    assert sql[5].endswith("ORDER BY app.scan_canaries.written_at DESC")


def test_t542_status_accepts_a_service_principal_in_prod(ops: OpsHarness) -> None:
    ops.az.on("account", "show", stdout="servicePrincipal\n")
    _script_status(ops, empty=True)
    outcome = ops.run("--env", "prod", "--json", "ingestion", "status")
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    report = outcome.json()
    assert report["dispatcher"] is None
    assert report["queued"] == {"interactive": 0, "bulk": 0}
    assert report["oldestQueuedAgeSeconds"] is None
    assert report["openRuns"] == []


def test_status_prints_the_open_runs_as_a_table(ops: OpsHarness) -> None:
    _script_status(ops)
    outcome = ops.run("--env", "dev", "ingestion", "status")
    assert outcome.code == ExitCode.SUCCESS
    lines = outcome.stdout.splitlines()
    header = next(index for index, line in enumerate(lines) if line.startswith("runId"))
    assert lines[header].split() == list(ingestion.OPEN_RUN_COLUMNS)
    assert lines[header + 1].split()[:2] == [str(RUN_ID), "index_rebuild"]


def test_status_without_sql_access_exits_3(ops: OpsHarness) -> None:
    ops.database.unit_errors["ingestion_status_read"] = SqlUnavailable("connection", 18456)
    assert ops.run("--env", "dev", "ingestion", "status").code == ExitCode.ACCESS_DENIED


# --- requeue-item -----------------------------------------------------------------------------


def _item(state: str, *, lease: datetime | None = EXPIRED, claim: int = 2) -> dict[str, Any]:
    return {
        "id": ITEM_ID,
        "run_id": RUN_ID,
        "state": state,
        "claim_count": claim,
        "lease_expires_at": lease,
        "now": DB_NOW,
    }


def test_requeue_item_requeues_an_expired_lease_and_writes_its_audit_row(
    ops: OpsHarness,
) -> None:
    ops.database.answer(_item("running"))
    ops.database.answer(rowcount=1)
    outcome = _run(ops, "requeue-item", "--item-id", str(ITEM_ID))
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert ops.database.units == [
        ("ops_connection_check", "transient"),
        ("ingestion_item_read", "transient"),
        ("ingestion_item_requeue", "none"),
    ]
    (update, insert) = ops.database.sql("ingestion_item_requeue")
    assert update.startswith("UPDATE app.ingestion_run_items SET state=N'queued'")
    assert "lease_expires_at=" in update
    assert "claim_count = " in update
    assert "lease_expires_at < SYSUTCDATETIME()" in update
    assert "state = N'running'" in update
    assert insert.startswith("INSERT INTO app.audit_events")
    audit = _audit(ops)
    assert audit["action"] == "ingestion.item_requeued"
    assert audit["actor_type"] == "operator"
    assert str(audit["actor_object_id"]) == ops.operator_oid
    assert audit["target_type"] == "ingestion_run_item"
    assert audit["target_id"] == str(ITEM_ID)
    assert audit["correlation_id"] == outcome.correlation_id
    assert _metadata(ops) == {"runId": str(RUN_ID), "claimCount": 2, "ticket": "INC-2201"}
    assert outcome.json()["state"] == "queued"
    assert "lease expires at: 2026-10-30T11:30:00.000Z" in outcome.stderr
    assert "recover a stuck" not in outcome.stdout


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (_item("queued"), "not running"),
        (_item("succeeded"), "not running"),
        (_item("running", lease=VALID), "lease is valid until 2026-10-30T12:20:00.000Z"),
        (_item("running", lease=None), "lease is valid"),
    ],
)
def test_requeue_item_refuses_with_exit_2(
    ops: OpsHarness, row: dict[str, Any], message: str
) -> None:
    ops.database.answer(row)
    outcome = _run(ops, "requeue-item", "--item-id", str(ITEM_ID))
    assert outcome.code == ExitCode.USAGE
    assert message in outcome.failure
    assert ops.database.audit_rows == []
    assert "Dry run" not in outcome.stderr


def test_requeue_of_an_unknown_item_exits_4(ops: OpsHarness) -> None:
    ops.database.answer()
    outcome = _run(ops, "requeue-item", "--item-id", str(ITEM_ID))
    assert outcome.code == ExitCode.NOT_FOUND
    assert ops.database.disposed


def test_requeue_of_a_changed_row_exits_6_with_nothing_written(ops: OpsHarness) -> None:
    ops.database.answer(_item("running"))
    ops.database.answer(rowcount=0)
    outcome = _run(ops, "requeue-item", "--item-id", str(ITEM_ID))
    assert outcome.code == ExitCode.CONFLICT
    assert "changed by someone else" in outcome.failure
    assert ops.database.audit_rows == []


def test_requeue_in_prod_needs_the_typed_environment_name(ops: OpsHarness) -> None:
    ops.database.answer(_item("running"))
    ops.database.answer(rowcount=1)
    ops.answer("prod\n", terminal=True)
    outcome = _run(ops, "requeue-item", "--item-id", str(ITEM_ID), environment="prod")
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert "Type the environment name to continue: " in outcome.stderr


def test_a_declined_requeue_writes_nothing(ops: OpsHarness) -> None:
    ops.database.answer(_item("running"))
    ops.answer("no\n")
    outcome = ops.run(
        "--env", "dev", "ingestion", "requeue-item", "--item-id", str(ITEM_ID), *TICKET
    )
    assert outcome.code == ExitCode.USAGE
    assert ("ingestion_item_requeue", "none") not in ops.database.units


# --- fail-item ------------------------------------------------------------------------------------


def test_fail_item_fails_a_queued_item_with_every_failure_side_effect(ops: OpsHarness) -> None:
    database = ops.database
    database.answer(_item("queued", lease=None, claim=0))
    database.answer(rowcount=1)  # the item
    database.answer(rowcount=1)  # the chunk set
    database.answer(rowcount=1)  # the version
    outcome = _run(ops, "fail-item", "--item-id", str(ITEM_ID))
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert not database.script
    item, chunk_set, version, insert = database.sql("ingestion_item_fail")
    assert item.startswith("UPDATE app.ingestion_run_items SET state=N'failed'")
    assert "state = N'queued'" in item
    assert "claim_count = " in item
    # The side effects are the shared ones of `docchat_core.ingestion.runs`, keyed by the item.
    assert chunk_set.startswith("UPDATE app.chunk_sets SET state=N'retired'")
    assert "IN (N'building', N'ready')" in chunk_set
    assert "i.id = " in chunk_set
    assert version.startswith("UPDATE app.document_versions SET state=N'failed'")
    assert "IN (N'pending', N'ingesting')" in version
    assert "active_physical_index_id = i.physical_index_id" in version
    # Only the three columns of the role's column grant.
    assert version.split(" FROM ")[0].count("=") == 3
    assert insert.startswith("INSERT INTO app.audit_events")
    updates = [
        statement.compile().params
        for statement, _ in database.executed
        if isinstance(statement, Update)
    ]
    item_values = next(
        statement.compile().params
        for statement, _ in database.executed
        if isinstance(statement, Update) and statement.table is ingestion_run_items
    )
    assert item_values["outcome_code"] == "processing_interrupted"
    assert item_values["outcome_message"] == ITEM_CODES["processing_interrupted"].message
    assert updates[-1]["failure_code"] == "processing_interrupted"
    assert ITEM_ID in updates[1].values()
    assert _audit(ops)["action"] == "ingestion.item_failed"
    assert _audit(ops)["target_type"] == "ingestion_run_item"
    assert _metadata(ops) == {
        "runId": str(RUN_ID),
        "outcomeCode": "processing_interrupted",
        "ticket": "INC-2201",
    }
    report = outcome.json()
    assert report["chunkSetRetired"] is True
    assert report["versionFailed"] is True


def test_fail_item_of_an_expired_running_item_reports_the_side_effects_that_changed_rows(
    ops: OpsHarness,
) -> None:
    database = ops.database
    database.answer(_item("running"))
    database.answer(rowcount=1)
    database.answer(rowcount=1)  # the chunk set
    database.answer(rowcount=0)  # no version: another index, or one already finished
    outcome = _run(ops, "fail-item", "--item-id", str(ITEM_ID))
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert not database.script
    item = database.sql("ingestion_item_fail")[0]
    assert "lease_expires_at < SYSUTCDATETIME()" in item
    assert "state = N'running'" in item
    report = outcome.json()
    assert report["chunkSetRetired"] is True
    assert report["versionFailed"] is False


def test_fail_item_of_an_index_item_changes_only_the_item(ops: OpsHarness) -> None:
    database = ops.database
    database.answer(_item("queued", lease=None))
    database.answer(rowcount=1)
    outcome = _run(ops, "fail-item", "--item-id", str(ITEM_ID))
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    # An item without a chunk set or a version matches neither side effect's join.
    assert len(database.sql("ingestion_item_fail")) == 4
    report = outcome.json()
    assert report["chunkSetRetired"] is False
    assert report["versionFailed"] is False


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (_item("running", lease=VALID), "lease is valid"),
        (_item("failed", lease=None), "already failed"),
        (_item("cancelled", lease=None), "already cancelled"),
    ],
)
def test_fail_item_refuses_with_exit_2(ops: OpsHarness, row: dict[str, Any], message: str) -> None:
    ops.database.answer(row)
    outcome = _run(ops, "fail-item", "--item-id", str(ITEM_ID))
    assert outcome.code == ExitCode.USAGE
    assert message in outcome.failure
    assert ops.database.audit_rows == []


def test_fail_item_of_an_unknown_item_exits_4(ops: OpsHarness) -> None:
    ops.database.answer()
    assert _run(ops, "fail-item", "--item-id", str(ITEM_ID)).code == ExitCode.NOT_FOUND


def test_fail_item_of_a_changed_row_exits_6(ops: OpsHarness) -> None:
    ops.database.answer(_item("queued", lease=None))
    ops.database.answer(rowcount=0)
    outcome = _run(ops, "fail-item", "--item-id", str(ITEM_ID))
    assert outcome.code == ExitCode.CONFLICT
    assert ops.database.audit_rows == []


# --- cancel-run -----------------------------------------------------------------------------------


def _script_run(ops: OpsHarness, *, kind: str = "index_reingest", state: str = "running") -> None:
    ops.database.answer({"kind": kind, "search_index_id": INDEX_ID, "state": state})
    ops.database.answer({"count": 3})


def test_cancel_run_cancels_its_queued_items(ops: OpsHarness) -> None:
    _script_run(ops)
    ops.database.answer({"state": "running"})
    ops.database.answer(rowcount=3)
    outcome = _run(ops, "cancel-run", "--run-id", str(RUN_ID))
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    locked, cancel, insert = ops.database.sql("ingestion_run_cancel")
    assert "WITH (UPDLOCK, ROWLOCK)" in locked
    assert cancel.startswith("UPDATE app.ingestion_run_items SET state=N'cancelled'")
    assert "outcome_code" not in cancel
    assert insert.startswith("INSERT INTO app.audit_events")
    assert _audit(ops)["target_type"] == "ingestion_run"
    assert _audit(ops)["target_id"] == str(RUN_ID)
    assert _metadata(ops) == {
        "indexId": str(INDEX_ID),
        "cancelledItemCount": 3,
        "ticket": "INC-2201",
    }
    assert outcome.json()["cancelledItemCount"] == 3
    assert "queued items to cancel: 3; running items finish" in outcome.stderr
    assert ingestion.UPLOAD_RUN_NOTE not in outcome.stderr


def test_cancel_of_an_upload_run_names_the_orphaned_version_duty(ops: OpsHarness) -> None:
    _script_run(ops, kind="upload", state="queued")
    ops.database.answer({"state": "queued"})
    ops.database.answer(rowcount=1)
    outcome = _run(ops, "cancel-run", "--run-id", str(RUN_ID))
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert ingestion.UPLOAD_RUN_NOTE in outcome.stderr


@pytest.mark.parametrize("state", ["completed", "completed_with_errors", "failed"])
def test_cancel_of_a_finished_run_exits_2(ops: OpsHarness, state: str) -> None:
    _script_run(ops, state=state)
    outcome = _run(ops, "cancel-run", "--run-id", str(RUN_ID))
    assert outcome.code == ExitCode.USAGE
    assert f"already {state}" in outcome.failure


def test_cancel_of_an_unknown_run_exits_4(ops: OpsHarness) -> None:
    ops.database.answer()
    assert _run(ops, "cancel-run", "--run-id", str(RUN_ID)).code == ExitCode.NOT_FOUND


@pytest.mark.parametrize("now", [{"state": "completed"}, None])
def test_cancel_of_a_run_that_changed_meanwhile_exits_6(
    ops: OpsHarness, now: dict[str, Any] | None
) -> None:
    _script_run(ops)
    ops.database.answer(*([now] if now else []))
    outcome = _run(ops, "cancel-run", "--run-id", str(RUN_ID))
    assert outcome.code == ExitCode.CONFLICT
    assert ops.database.audit_rows == []


def test_a_deadlock_exits_6(ops: OpsHarness) -> None:
    _script_run(ops)
    ops.database.answer_error(SqlDeadlock(1205, "40001"))
    assert _run(ops, "cancel-run", "--run-id", str(RUN_ID)).code == ExitCode.CONFLICT


# --- abandon-rebuild ------------------------------------------------------------------------------


def _script_rebuild_read(ops: OpsHarness, *, status: str = "rebuilding") -> None:
    ops.database.answer({"status": status, "row_version": ROW_VERSION})
    ops.database.answer({"id": PHYSICAL_ID, "generation": 2})
    ops.database.answer({"count": 4})


def _index_status_row(*, active: UUID | None) -> dict[str, Any]:
    return {
        "method_key": "pagewise",
        "method_version": 1,
        "method_parameter_hash": b"\x01" * 32,
        "embedding_model": "text-embedding-3-large",
        "embedding_dimensions": 1024,
        "language_analyzer": "standard.lucene",
        "active_id": active,
        "active_model": "text-embedding-3-large" if active else None,
        "active_dimensions": 1024 if active else None,
        "active_analyzer": "standard.lucene" if active else None,
        "active_schema_version": 1 if active else None,
    }


def test_abandon_rebuild_retires_the_building_index_and_adds_its_delete_item(
    ops: OpsHarness,
) -> None:
    database = ops.database
    _script_rebuild_read(ops)
    database.answer({"row_version": ROW_VERSION})
    database.answer(rowcount=1)  # retire
    database.answer(rowcount=4)  # cancel
    database.answer(_index_status_row(active=OTHER_PHYSICAL_ID))
    database.answer()  # no stale served chunk set
    database.answer(rowcount=1)  # the status
    outcome = _run(ops, "abandon-rebuild", "--index-id", str(INDEX_ID))
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert not database.script
    sql = database.sql("ingestion_rebuild_abandon")
    assert "WITH (UPDLOCK, ROWLOCK)" in sql[0]
    assert sql[1].startswith("UPDATE app.physical_indexes SET state=N'retiring'")
    assert "retiring_at=SYSUTCDATETIME(), delete_after=SYSUTCDATETIME()" in sql[1]
    assert "build_run_id=" in sql[1]
    assert "state = N'building'" in sql[1]
    assert sql[2].startswith("UPDATE app.ingestion_run_items SET state=N'cancelled'")
    status_update = next(text for text in sql if text.startswith("UPDATE app.search_indexes"))
    assert "row_version = " in status_update
    audit = _audit(ops)
    assert audit["action"] == "ingestion.rebuild_abandoned"
    assert audit["target_type"] == "physical_index"
    assert audit["target_id"] == str(PHYSICAL_ID)
    metadata = _metadata(ops)
    assert metadata["indexId"] == str(INDEX_ID)
    assert metadata["generation"] == 2
    assert metadata["cancelledItemCount"] == 4
    assert metadata["ticket"] == "INC-2201"
    report = outcome.json()
    assert report["runId"] == metadata["runId"]
    assert report["status"] == "ready"
    assert report["physicalIndexId"] == str(PHYSICAL_ID)
    assert ingestion.NEXT_WAKE in outcome.stderr
    assert "queued items to cancel: 4" in outcome.stderr


def test_abandon_rebuild_creates_a_maintenance_run_with_one_delete_item(ops: OpsHarness) -> None:
    database = ops.database
    _script_rebuild_read(ops, status="failed")
    database.answer({"row_version": ROW_VERSION})
    database.answer(rowcount=1)
    database.answer(rowcount=0)
    database.answer(_index_status_row(active=None))
    database.answer(rowcount=1)
    outcome = _run(ops, "abandon-rebuild", "--index-id", str(INDEX_ID))
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    runs = [
        statement.compile().params
        for statement, _ in database.executed
        if isinstance(statement, Insert) and statement.table.name == "ingestion_runs"
    ]
    items = [
        parameters
        for statement, parameters in database.executed
        if isinstance(statement, Insert) and statement.table.name == "ingestion_run_items"
    ]
    assert runs[0]["kind"] == "maintenance"
    assert runs[0]["priority"] == 0
    assert runs[0]["requested_by"] is None
    assert isinstance(items[0], list)
    (item,) = items[0]
    assert item["operation"] == "delete_physical_index"
    assert item["physical_index_id"] == PHYSICAL_ID
    assert outcome.json()["status"] == "failed"
    assert outcome.json()["cancelledItemCount"] == 0


def test_abandon_rebuild_of_an_index_without_a_building_index_exits_2(ops: OpsHarness) -> None:
    ops.database.answer({"status": "ready", "row_version": ROW_VERSION})
    ops.database.answer()
    outcome = _run(ops, "abandon-rebuild", "--index-id", str(INDEX_ID))
    assert outcome.code == ExitCode.USAGE
    assert "no building physical index" in outcome.failure
    assert "Dry run" not in outcome.stderr


def test_abandon_rebuild_of_an_index_being_deleted_exits_2(ops: OpsHarness) -> None:
    _script_rebuild_read(ops, status="deleting")
    outcome = _run(ops, "abandon-rebuild", "--index-id", str(INDEX_ID))
    assert outcome.code == ExitCode.USAGE
    assert "index_busy" in outcome.failure


@pytest.mark.parametrize("row", [None, {"status": "deleted", "row_version": ROW_VERSION}])
def test_abandon_rebuild_of_an_unknown_index_exits_4(
    ops: OpsHarness, row: dict[str, Any] | None
) -> None:
    ops.database.answer(*([row] if row else []))
    outcome = _run(ops, "abandon-rebuild", "--index-id", str(INDEX_ID))
    assert outcome.code == ExitCode.NOT_FOUND
    assert "index_not_found" in outcome.failure


@pytest.mark.parametrize(
    "answers",
    [
        [{"row_version": b"\x00" * 8}],
        [None],
        [{"row_version": ROW_VERSION}, 0],
        [{"row_version": ROW_VERSION}, 1, 0, "status", 0],
    ],
)
def test_abandon_rebuild_of_a_changed_index_exits_6(ops: OpsHarness, answers: list[Any]) -> None:
    _script_rebuild_read(ops)
    for answer in answers:
        if answer is None:
            ops.database.answer()
        elif answer == "status":
            ops.database.answer(_index_status_row(active=None))
        elif isinstance(answer, int):
            ops.database.answer(rowcount=answer)
        else:
            ops.database.answer(answer)
    outcome = _run(ops, "abandon-rebuild", "--index-id", str(INDEX_ID))
    assert outcome.code == ExitCode.CONFLICT
    assert ops.database.audit_rows == []


# --- set-deployments ------------------------------------------------------------------------------

RENAMED = json.dumps(
    [
        {
            "model": "text-embedding-3-large",
            "modelVersion": "1",
            "queryDeployment": "embed-query-te3-large-v1b",
            "ingestionDeployment": "embed-ingest-te3-large-v1b",
            "dimensions": [256, 1024, 3072],
            "defaultDimensions": 1024,
            "maxInputTokens": 8192,
            "encoding": "cl100k_base",
        },
        {
            "model": "text-embedding-3-small",
            "modelVersion": "1",
            "queryDeployment": "embed-query-te3-small-v1",
            "ingestionDeployment": "embed-ingest-te3-small-v1",
            "dimensions": [256, 1024],
            "defaultDimensions": 1024,
            "maxInputTokens": 8192,
            "encoding": "cl100k_base",
        },
    ]
)


def _physical_row(*, state: str = "active", version: str = "1") -> dict[str, Any]:
    return {
        "search_index_id": INDEX_ID,
        "generation": 2,
        "embedding_model": "text-embedding-3-large",
        "embedding_model_version": version,
        "query_embedding_deployment": "embed-query-te3-large-v1",
        "ingestion_embedding_deployment": "embed-ingest-te3-large-v1",
        "state": state,
    }


def _set(ops: OpsHarness, query: str, ingestion_name: str) -> Outcome:
    return _run(
        ops,
        "set-deployments",
        "--physical-index-id",
        str(PHYSICAL_ID),
        "--query",
        query,
        "--ingestion",
        ingestion_name,
    )


def test_set_deployments_moves_a_physical_index_to_a_renamed_pair(ops: OpsHarness) -> None:
    ops.write_azd("dev", DOCCHAT_OPENAI_EMBEDDING_MODELS=RENAMED)
    ops.database.answer(_physical_row())
    ops.database.answer(rowcount=1)
    outcome = _set(ops, "embed-query-te3-large-v1b", "embed-ingest-te3-large-v1b")
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    update, insert = ops.database.sql("ingestion_deployments_change")
    assert update.startswith("UPDATE app.physical_indexes SET query_embedding_deployment=")
    assert "query_embedding_deployment = " in update.split(" WHERE ")[1]
    assert "state != N'deleted'" in update
    assert insert.startswith("INSERT INTO app.audit_events")
    assert _audit(ops)["action"] == "ingestion.deployments_changed"
    assert _audit(ops)["target_id"] == str(PHYSICAL_ID)
    assert _metadata(ops) == {
        "indexId": str(INDEX_ID),
        "generation": 2,
        "previousQueryDeployment": "embed-query-te3-large-v1",
        "queryDeployment": "embed-query-te3-large-v1b",
        "previousIngestionDeployment": "embed-ingest-te3-large-v1",
        "ingestionDeployment": "embed-ingest-te3-large-v1b",
        "ticket": "INC-2201",
    }
    assert outcome.json()["result"] == "changed"
    assert "query deployment: embed-query-te3-large-v1 -> embed-query-te3-large-v1b" in (
        outcome.stderr
    )


def test_set_deployments_to_the_current_pair_changes_nothing(ops: OpsHarness) -> None:
    ops.database.answer(_physical_row())
    outcome = _set(ops, "embed-query-te3-large-v1", "embed-ingest-te3-large-v1")
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert outcome.json()["result"] == "unchanged"
    assert ops.database.audit_rows == []
    assert "Dry run" not in outcome.stderr


@pytest.mark.parametrize(
    ("query", "ingestion_name", "row", "message"),
    [
        ("embed-query-te3-large-v1b", "embed-ingest-te3-small-v1", _physical_row(), "no entry"),
        ("embed-query-te3-small-v1", "embed-ingest-te3-small-v1", _physical_row(), "another"),
        (
            "embed-query-te3-large-v1b",
            "embed-ingest-te3-large-v1b",
            _physical_row(version="2"),
            "needs a rebuild",
        ),
    ],
)
def test_set_deployments_of_another_model_or_version_exits_2(
    ops: OpsHarness, query: str, ingestion_name: str, row: dict[str, Any], message: str
) -> None:
    ops.write_azd("dev", DOCCHAT_OPENAI_EMBEDDING_MODELS=RENAMED)
    ops.database.answer(row)
    outcome = _set(ops, query, ingestion_name)
    assert outcome.code == ExitCode.USAGE
    assert message in outcome.failure
    assert ops.database.audit_rows == []


@pytest.mark.parametrize("row", [None, _physical_row(state="deleted")])
def test_set_deployments_of_an_unknown_physical_index_exits_4(
    ops: OpsHarness, row: dict[str, Any] | None
) -> None:
    ops.database.answer(*([row] if row else []))
    outcome = _set(ops, "embed-query-te3-large-v1", "embed-ingest-te3-large-v1")
    assert outcome.code == ExitCode.NOT_FOUND


def test_set_deployments_of_a_changed_row_exits_6(ops: OpsHarness) -> None:
    ops.write_azd("dev", DOCCHAT_OPENAI_EMBEDDING_MODELS=RENAMED)
    ops.database.answer(_physical_row())
    ops.database.answer(rowcount=0)
    outcome = _set(ops, "embed-query-te3-large-v1b", "embed-ingest-te3-large-v1b")
    assert outcome.code == ExitCode.CONFLICT


def test_set_deployments_with_a_malformed_catalogue_exits_2_before_sign_in(
    ops: OpsHarness,
) -> None:
    ops.write_azd("dev", DOCCHAT_OPENAI_EMBEDDING_MODELS="[]")
    outcome = _set(ops, "embed-query-te3-large-v1", "embed-ingest-te3-large-v1")
    assert outcome.code == ExitCode.USAGE
    assert "azd env refresh -e dev" in outcome.failure
    assert ops.credentials_made == 0


def test_a_unique_violation_exits_6(ops: OpsHarness) -> None:
    ops.write_azd("dev", DOCCHAT_OPENAI_EMBEDDING_MODELS=RENAMED)
    ops.database.answer(_physical_row())
    ops.database.answer_error(SqlUniqueViolation(2601, "23000", "UX_physical_indexes_name"))
    outcome = _set(ops, "embed-query-te3-large-v1b", "embed-ingest-te3-large-v1b")
    assert outcome.code == ExitCode.CONFLICT


# --- drain-poison ---------------------------------------------------------------------------------


def _drain(ops: OpsHarness, *extra: str, environment: str = "dev") -> Outcome:
    return ops.run("--env", environment, "--json", "ingestion", "drain-poison", *TICKET, *extra)


@pytest.mark.parametrize(
    ("count", "shown", "receives"),
    [
        (0, "messages: 0", [32]),
        (31, "messages: 31", [32, 32]),
        (32, "messages: at least 32", [32, 32]),
        (70, "messages: at least 32", [32, 32, 32, 32]),
    ],
)
def test_drain_poison_peeks_then_drains_in_receives_of_32(
    ops: OpsHarness, count: int, shown: str, receives: list[int]
) -> None:
    ops.poison.fill([_message(RUN_ID)] * count)
    outcome = _drain(ops, "--yes")
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert ops.poison.peeks == [32]
    assert shown in outcome.stderr
    assert ops.poison.receives == receives
    assert len(ops.poison.deleted) == count
    assert ops.poison.messages == []
    assert ops.poison.closed
    assert ops.poison.endpoints == [ops.queue_endpoint]
    assert "https://storage.azure.com/.default" in ops.credential.scopes
    report = outcome.json()
    assert report["deletedCount"] == count
    assert report["queue"] == "ingestion-requests-poison"
    audit = _audit(ops)
    assert audit["action"] == "ingestion.poison_drained"
    assert audit["target_type"] == "ingestion_queue"
    assert audit["target_id"] == "ingestion-requests-poison"
    assert audit["correlation_id"] == outcome.correlation_id
    assert _metadata(ops) == {"deletedCount": count, "ticket": "INC-2201"}


def test_drain_poison_names_the_distinct_runs_of_the_peeked_messages(ops: OpsHarness) -> None:
    ops.poison.fill([_message(RUN_ID), _message(INDEX_ID), _message(RUN_ID), "%%%"])
    outcome = _drain(ops, "--yes")
    assert outcome.code == ExitCode.SUCCESS
    assert f"runs named: {INDEX_ID}, {RUN_ID}" in outcome.stderr
    assert "messages naming no run: 1" in outcome.stderr
    assert str(RUN_ID) not in outcome.stdout


def test_drain_poison_with_nothing_named_says_none(ops: OpsHarness) -> None:
    outcome = _drain(ops, "--yes")
    assert "runs named: none" in outcome.stderr


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ServiceRequestError("unreachable"), ExitCode.UNREACHABLE),
        (_http_error(403, "AuthorizationFailure"), ExitCode.UNREACHABLE),
        (_http_error(403, "AuthorizationPermissionMismatch"), ExitCode.ACCESS_DENIED),
    ],
)
def test_drain_poison_exits_before_any_delete_when_the_peek_fails(
    ops: OpsHarness, error: Exception, code: ExitCode
) -> None:
    ops.poison.fill([_message(RUN_ID)])
    ops.poison.peek_error = error
    outcome = _drain(ops, "--yes")
    assert outcome.code == code
    assert ops.poison.deleted == []
    assert ops.poison.receives == []
    assert ops.database.audit_rows == []
    assert ops.poison.closed


def test_drain_poison_exits_7_when_the_first_receive_fails(ops: OpsHarness) -> None:
    ops.poison.fill([_message(RUN_ID)])
    ops.poison.receive_errors[0] = ServiceRequestError("unreachable")
    outcome = _drain(ops, "--yes")
    assert outcome.code == ExitCode.UNREACHABLE
    assert ops.database.audit_rows == []


@pytest.mark.parametrize(
    ("receive_errors", "delete_errors", "drained"),
    [({}, {5: ServiceRequestError("reset")}, 5), ({1: ServiceRequestError("reset")}, {}, 32)],
)
def test_drain_poison_stopped_part_way_exits_5_and_records_what_it_drained(
    ops: OpsHarness,
    receive_errors: dict[int, Exception],
    delete_errors: dict[int, Exception],
    drained: int,
) -> None:
    ops.poison.fill([_message(RUN_ID)] * 40)
    ops.poison.receive_errors.update(receive_errors)
    ops.poison.delete_errors.update(delete_errors)
    outcome = _drain(ops, "--yes")
    assert outcome.code == ExitCode.INCOMPLETE
    assert f"drained {drained} messages before a receive or delete failed" in outcome.stderr
    assert f"after {drained} messages were drained" in outcome.failure
    assert "run the same command once more" in outcome.failure
    assert _metadata(ops) == {"deletedCount": drained, "ticket": "INC-2201"}
    assert ops.poison.closed


def test_drain_poison_declined_deletes_nothing(ops: OpsHarness) -> None:
    ops.poison.fill([_message(RUN_ID)])
    ops.answer("no\n")
    outcome = _drain(ops)
    assert outcome.code == ExitCode.USAGE
    assert ops.poison.deleted == []
    assert ops.database.audit_rows == []


def test_drain_poison_in_prod_needs_the_typed_environment_name(ops: OpsHarness) -> None:
    ops.poison.fill([_message(RUN_ID)])
    ops.answer("prod\n", terminal=True)
    outcome = _drain(ops, "--yes", environment="prod")
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert "Type the environment name to continue: " in outcome.stderr
    assert len(ops.poison.deleted) == 1


def test_drain_poison_needs_the_queue_endpoint(ops: OpsHarness) -> None:
    ops.write_azd("dev", DOCCHAT_STORAGE_QUEUE_ENDPOINT="")
    outcome = _drain(ops, "--yes")
    assert outcome.code == ExitCode.USAGE
    assert "DOCCHAT_STORAGE_QUEUE_ENDPOINT is missing" in outcome.failure
    assert ops.credentials_made == 0


def test_drain_poison_without_a_storage_token_exits_3_before_the_queue(ops: OpsHarness) -> None:
    ops.credential.refused.add("https://storage.azure.com/.default")
    outcome = _drain(ops, "--yes")
    assert outcome.code == ExitCode.ACCESS_DENIED
    assert ops.poison.endpoints == []


# --- the storage adapters -------------------------------------------------------------------------


class _QueueMessage:
    def __init__(self, message_id: str, content: object, pop_receipt: str | None = None) -> None:
        self.id = message_id
        self.content = content
        self.pop_receipt = pop_receipt


class _Pager:
    def __init__(self, messages: list[_QueueMessage]) -> None:
        self._messages = list(messages)

    def __aiter__(self) -> _Pager:
        return self

    async def __anext__(self) -> _QueueMessage:
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


class _QueueClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def peek_messages(self, max_messages: int) -> list[_QueueMessage]:
        self.calls.append(("peek", max_messages))
        return [_QueueMessage("a", "text"), _QueueMessage("b", b"bytes")]

    def receive_messages(self, *, max_messages: int) -> _Pager:
        self.calls.append(("receive", max_messages))
        return _Pager([_QueueMessage("a", "text", "receipt-a")])

    async def delete_message(self, message: str, pop_receipt: str | None) -> None:
        self.calls.append(("delete", (message, pop_receipt)))

    async def close(self) -> None:
        self.calls.append(("close", None))


async def test_the_queue_adapter_reads_raw_text_and_deletes_by_receipt() -> None:
    client = _QueueClient()
    queue = ingestion.StoragePoisonQueue(client)  # type: ignore[arg-type]
    assert await queue.peek(32) == [
        ingestion.PoisonMessage("a", None, "text"),
        ingestion.PoisonMessage("b", None, ""),
    ]
    received = await queue.receive(32)
    assert received == [ingestion.PoisonMessage("a", "receipt-a", "text")]
    await queue.delete(received[0])
    await queue.close()
    assert client.calls == [
        ("peek", 32),
        ("receive", 32),
        ("delete", ("a", "receipt-a")),
        ("close", None),
    ]


@pytest.fixture
def poison_client() -> tuple[Any, Any]:
    # Built outside the test's event loop, as the CLI's clients are cheap to build but a
    # transport's set-up belongs to no coroutine.
    credential = cli_credential()
    queue = ingestion.storage_poison_queue(QUEUE_ENDPOINT, credential)
    return queue, credential


async def test_the_poison_queue_client_has_the_platform_options(
    poison_client: tuple[Any, Any],
) -> None:
    queue, credential = poison_client
    client = queue._client
    assert client.queue_name == "ingestion-requests-poison"
    assert client.url.startswith(QUEUE_ENDPOINT)
    await queue.close()
    await credential.close()
