"""`reindex start` (T-541; `86-operations.md` section 2.8.6 and runbook R05; `14-ingestion.md`
sections 8.5 to 8.7 and 19.1; `80-data-model.md` section 2.21).

The command runs through the real parser against the scripted SQL layer of `conftest.py`. Most
tests stand in for `create_reingest_run`, whose statements its own unit tests prove; one runs the
real function on the script, so the wiring of the command to it is proven too.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pytest
from sqlalchemy import Insert

from docchat_core.ingestion.models import (
    IndexStatus,
    IngestionRefusedError,
    ReingestMode,
)
from docchat_core.ingestion.runs import ReingestResult
from docchat_core.sql.engine import SqlUniqueViolation
from docchat_ops.commands import reindex
from docchat_ops.framework.exitcodes import ExitCode

if TYPE_CHECKING:
    from ops.tests.unit.conftest import OpsHarness, Outcome

TICKET = ("--ticket", "CHG-3107", "--reason", "re-ingest after the method change")
INDEX_ID = UUID("3f2c0d9e-8b7a-41c6-a5d4-e3f2c1b0a998")
ACTIVE_ID = UUID("9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d")
RUN_ID = UUID("6c1f3e2a-9b4d-4f0e-8a71-2d5c9e0b7f13")
NEW_PHYSICAL_ID = UUID("1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d")
ROW_VERSION = b"\x00\x00\x00\x00\x00\x00\x07\xd1"
MODEL = "text-embedding-3-large"


def _start(ops: OpsHarness, *extra: str, environment: str = "dev") -> Outcome:
    return ops.run(
        "--env",
        environment,
        "--json",
        "reindex",
        "start",
        "--index-id",
        str(INDEX_ID),
        *TICKET,
        *extra,
    )


def _index_row(
    *, status: str = "reingest_required", active: UUID | None = ACTIVE_ID, dimensions: int = 1024
) -> dict[str, Any]:
    return {
        "status": status,
        "row_version": ROW_VERSION,
        "embedding_model": MODEL,
        "embedding_dimensions": dimensions,
        "language_analyzer": "standard.lucene",
        "active_physical_index_id": active,
    }


def _physical_row(*, analyzer: str = "standard.lucene") -> dict[str, Any]:
    return {
        "embedding_model": MODEL,
        "embedding_dimensions": 1024,
        "language_analyzer": analyzer,
        "schema_version": 1,
    }


def _script_plan(
    ops: OpsHarness,
    *,
    index: dict[str, Any] | None = None,
    physical: dict[str, Any] | None = None,
    live: int = 4,
    physical_count: int = 3,
) -> None:
    row = _index_row() if index is None else index
    ops.database.answer(row)
    if row["active_physical_index_id"] is not None:
        ops.database.answer(_physical_row() if physical is None else physical)
    ops.database.answer({"count": live})
    ops.database.answer({"count": physical_count})


class _Recorder:
    """Stands in for `create_reingest_run`, recording its keyword arguments."""

    def __init__(self, result: ReingestResult | None = None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def __call__(self, connection: Any, **kwargs: Any) -> ReingestResult:
        del connection
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def _same_index() -> ReingestResult:
    return ReingestResult(RUN_ID, ReingestMode.SAME_INDEX, 4, ACTIVE_ID, IndexStatus.REINGESTING)


def test_start_runs_the_transaction_of_admin_start_reingest(
    ops: OpsHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _Recorder(_same_index())
    monkeypatch.setattr(reindex, "create_reingest_run", recorder)
    _script_plan(ops)
    outcome = _start(ops, "--yes")
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    (call,) = recorder.calls
    assert call["index_id"] == INDEX_ID
    assert call["row_version"] == ROW_VERSION
    assert call["requested_by"] == UUID(ops.operator_oid)
    assert call["trace"] is None
    assert call["max_indexes"] == 15
    assert call["catalog"].find(MODEL) is not None
    assert ops.database.units == [
        ("ops_connection_check", "transient"),
        ("reindex_read", "transient"),
        ("reindex_start", "none"),
    ]
    (audit,) = ops.database.audit_rows
    assert audit["action"] == "ingestion.reindex_started"
    assert audit["actor_type"] == "operator"
    assert str(audit["actor_object_id"]) == ops.operator_oid
    assert audit["target_type"] == "search_index"
    assert audit["target_id"] == str(INDEX_ID)
    assert audit["project_id"] is None
    assert audit["correlation_id"] == outcome.correlation_id
    assert json.loads(audit["metadata_json"]) == {
        "runId": str(RUN_ID),
        "mode": "same_index",
        "itemCount": 4,
        "ticket": "CHG-3107",
    }
    assert outcome.json() | {"correlationId": None} == {
        "correlationId": None,
        "command": "reindex start",
        "environment": "dev",
        "result": "started",
        "indexId": str(INDEX_ID),
        "runId": str(RUN_ID),
        "mode": "same_index",
        "itemCount": 4,
        "physicalIndexId": str(ACTIVE_ID),
        "status": "reingesting",
    }
    assert f"index: {INDEX_ID} (status reingest_required)" in outcome.stderr
    assert "mode: same_index" in outcome.stderr
    assert "live documents to re-ingest: 4" in outcome.stderr
    assert "dispatcher's next wake" in outcome.stderr
    assert "re-ingest after" not in outcome.stdout
    assert not ops.database.script


def test_start_runs_the_real_run_function_in_same_index_mode(ops: OpsHarness) -> None:
    _script_plan(ops)
    database = ops.database
    database.answer(_index_row())  # the locked read of the logical index
    database.answer(_physical_row())  # the active physical index's settings
    database.answer(rowcount=1)  # the guarded status update
    database.answer(
        {"id": UUID(int=1), "current_version_id": UUID(int=2)},
        {"id": UUID(int=3), "current_version_id": UUID(int=4)},
    )
    outcome = _start(ops, "--yes")
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert not database.script
    sql = database.sql("reindex_start")
    assert "WITH (UPDLOCK, ROWLOCK)" in sql[0]
    assert sql[2].startswith("UPDATE app.search_indexes SET status=")
    runs = [
        statement.compile().params
        for statement, _ in database.executed
        if isinstance(statement, Insert) and statement.table.name == "ingestion_runs"
    ]
    assert runs[0]["kind"] == "index_reingest"
    assert runs[0]["requested_by"] == UUID(ops.operator_oid)
    assert runs[0]["traceparent"] is None
    report = outcome.json()
    assert report["mode"] == "same_index"
    assert report["itemCount"] == 2
    assert json.loads(database.audit_rows[0]["metadata_json"])["itemCount"] == 2


@pytest.mark.parametrize(
    ("index", "physical"),
    [
        (_index_row(status="failed", active=None), None),
        (_index_row(status="ready"), _physical_row(analyzer="en.microsoft")),
    ],
)
def test_start_shows_a_rebuild_with_the_service_capacity(
    ops: OpsHarness,
    monkeypatch: pytest.MonkeyPatch,
    index: dict[str, Any],
    physical: dict[str, Any] | None,
) -> None:
    result = ReingestResult(
        RUN_ID, ReingestMode.NEW_PHYSICAL_INDEX, 4, NEW_PHYSICAL_ID, IndexStatus.REBUILDING
    )
    monkeypatch.setattr(reindex, "create_reingest_run", _Recorder(result))
    _script_plan(ops, index=index, physical=physical, physical_count=14)
    outcome = _start(ops, "--yes")
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert "mode: new_physical_index" in outcome.stderr
    assert "physical indexes on the search service: 14 of 15" in outcome.stderr
    report = outcome.json()
    assert report["mode"] == "new_physical_index"
    assert report["physicalIndexId"] == str(NEW_PHYSICAL_ID)
    assert report["status"] == "rebuilding"


def test_a_missing_active_physical_row_counts_as_no_active_index(
    ops: OpsHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reindex, "create_reingest_run", _Recorder(_same_index()))
    ops.database.answer(_index_row())
    ops.database.answer()
    ops.database.answer({"count": 0})
    ops.database.answer({"count": 2})
    outcome = _start(ops, "--yes")
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert "mode: new_physical_index" in outcome.stderr


@pytest.mark.parametrize("row", [None, _index_row(status="deleted")])
def test_start_of_an_unknown_index_exits_4(ops: OpsHarness, row: dict[str, Any] | None) -> None:
    ops.database.answer(*([row] if row else []))
    outcome = _start(ops, "--yes")
    assert outcome.code == ExitCode.NOT_FOUND
    assert "index_not_found" in outcome.failure
    assert ops.database.audit_rows == []


@pytest.mark.parametrize("status", ["provisioning", "reingesting", "rebuilding", "deleting"])
def test_start_while_the_status_allows_no_re_ingest_exits_2(ops: OpsHarness, status: str) -> None:
    _script_plan(ops, index=_index_row(status=status))
    outcome = _start(ops, "--yes")
    assert outcome.code == ExitCode.USAGE
    assert "index_busy" in outcome.failure
    assert "Dry run" not in outcome.stderr
    assert ("reindex_start", "none") not in ops.database.units


def test_start_with_no_room_on_the_search_service_exits_2(ops: OpsHarness) -> None:
    _script_plan(ops, index=_index_row(active=None), physical_count=15)
    outcome = _start(ops, "--yes")
    assert outcome.code == ExitCode.USAGE
    assert "search_index_capacity" in outcome.failure
    assert "adminDeleteIndex" in outcome.failure
    assert "Dry run" not in outcome.stderr


def test_start_with_dimensions_the_catalogue_lacks_exits_2(ops: OpsHarness) -> None:
    _script_plan(ops, index=_index_row(active=None, dimensions=1536))
    outcome = _start(ops, "--yes")
    assert outcome.code == ExitCode.USAGE
    assert "unsupported_embedding_model" in outcome.failure


def test_start_of_a_model_missing_from_the_catalogue_exits_2(ops: OpsHarness) -> None:
    row = _index_row(active=None) | {"embedding_model": "text-embedding-3-small"}
    _script_plan(ops, index=row)
    outcome = _start(ops, "--yes")
    assert outcome.code == ExitCode.USAGE
    assert "unsupported_embedding_model" in outcome.failure


@pytest.mark.parametrize(
    ("code", "exit_code"),
    [
        ("index_not_found", ExitCode.NOT_FOUND),
        ("precondition_failed", ExitCode.CONFLICT),
        ("index_busy", ExitCode.USAGE),
        ("search_index_capacity", ExitCode.USAGE),
        ("unsupported_embedding_model", ExitCode.USAGE),
    ],
)
def test_a_refusal_in_the_transaction_maps_to_its_exit_code(
    ops: OpsHarness, monkeypatch: pytest.MonkeyPatch, code: Any, exit_code: ExitCode
) -> None:
    monkeypatch.setattr(
        reindex, "create_reingest_run", _Recorder(error=IngestionRefusedError(code))
    )
    _script_plan(ops)
    outcome = _start(ops, "--yes")
    assert outcome.code == exit_code
    assert ops.database.audit_rows == []
    if exit_code == ExitCode.CONFLICT:
        assert "changed by someone else" in outcome.failure
    else:
        assert code in outcome.failure


def test_a_concurrent_rebuild_exits_6(ops: OpsHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    error = SqlUniqueViolation(2601, "23000", "UX_physical_indexes_one_building")
    monkeypatch.setattr(reindex, "create_reingest_run", _Recorder(error=error))
    _script_plan(ops)
    assert _start(ops, "--yes").code == ExitCode.CONFLICT


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("DOCCHAT_SEARCH_MAX_INDEXES", "", "DOCCHAT_SEARCH_MAX_INDEXES is missing"),
        ("DOCCHAT_SEARCH_MAX_INDEXES", "fifteen", "isn't 2 to 200"),
        ("DOCCHAT_OPENAI_EMBEDDING_MODELS", "{}", "isn't an embedding catalogue"),
    ],
)
def test_malformed_ledger_values_exit_2_before_sign_in(
    ops: OpsHarness, name: str, value: str, message: str
) -> None:
    ops.write_azd("dev", **{name: value})
    outcome = _start(ops, "--yes")
    assert outcome.code == ExitCode.USAGE
    assert message in outcome.failure
    assert ops.credentials_made == 0
    assert ops.az.calls == []


def test_start_in_prod_needs_the_typed_environment_name(
    ops: OpsHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reindex, "create_reingest_run", _Recorder(_same_index()))
    _script_plan(ops)
    ops.answer("prod\n", terminal=True)
    outcome = _start(ops, "--yes", environment="prod")
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert "Type the environment name to continue: " in outcome.stderr


def test_a_declined_start_writes_nothing(ops: OpsHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder(_same_index())
    monkeypatch.setattr(reindex, "create_reingest_run", recorder)
    _script_plan(ops)
    ops.answer("no\n")
    outcome = _start(ops)
    assert outcome.code == ExitCode.USAGE
    assert recorder.calls == []
    assert ops.database.audit_rows == []


def test_the_load_deploy_identity_starts_a_re_ingest_in_load(
    ops: OpsHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reindex, "create_reingest_run", _Recorder(_same_index()))
    ops.az.on("account", "show", stdout="servicePrincipal\n")
    _script_plan(ops)
    assert _start(ops, "--yes", environment="load").code == ExitCode.SUCCESS


def test_an_unknown_index_id_exits_2_before_sign_in(ops: OpsHarness) -> None:
    outcome = ops.run("--env", "dev", "reindex", "start", "--index-id", "nope", *TICKET)
    assert outcome.code == ExitCode.USAGE
    assert ops.credentials_made == 0
