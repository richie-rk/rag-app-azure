"""The sweep duties' units of work on a scripted connection (`14-ingestion.md` section 5.3 duties
1, 2, 4, 5, 6, 8, 10 and 12). Their effect on a real SQL Server is T-149, T-161 and T-623's."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from docchat_core.ingestion.models import ItemOperation
from docchat_ingestion.activities import sweep_duties as duties

INDEX = UUID("3f2c0d9e-8b7a-41c6-a5d4-e3f2c1b0a998")
OTHER_INDEX = UUID("4a3d1e0f-9c8b-42d7-b6e5-f4a3d2c1b0a9")
ACTIVE = UUID("1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d")
BUILDING = UUID("2b3c4d5e-6f7a-4b8c-9d0e-1f2a3b4c5d6e")
DOCUMENT = UUID("6c2d3e4f-5061-4b72-9c83-a4b5c6d7e8f9")
VERSION = UUID("5b1c2d3e-4f50-4a61-8b72-93a4b5c6d7e8")
ITEM = UUID("7d3e4f50-6172-4c83-ad94-b5c6d7e8f9a0")
RUN = UUID("2d3e4f5a-6b7c-4d8e-9f0a-1b2c3d4e5f60")
CHUNK_SET = UUID("8e4f5061-7283-4d94-bea5-c6d7e8f9a0b1")


def _item_rows(executed: Any) -> list[dict[str, Any]]:
    return [row for item in executed for row in (item.parameters or []) if "operation" in row]


def test_heartbeat_returns_the_sweep_time_as_utc(fakes: SimpleNamespace) -> None:
    swept = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    stored = swept.replace(tzinfo=None)
    connection = fakes.FakeSql(fakes.Rows([fakes.row(last_sweep_at=stored)])).connection

    assert duties.heartbeat(connection, 7) == swept
    (statement,) = connection.executed
    assert "OUTPUT inserted.last_sweep_at" in statement.sql
    assert "running_items=(SELECT count(*)" in statement.sql
    assert statement.params["generation"] == 7


def test_heartbeat_without_the_dispatcher_row_raises(fakes: SimpleNamespace) -> None:
    connection = fakes.FakeSql(fakes.Rows([])).connection

    try:
        duties.heartbeat(connection, 0)
    except LookupError:
        pass
    else:
        raise AssertionError("expected LookupError")


def _expired_row(fakes: SimpleNamespace, state: str, **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "id": ITEM,
        "state": state,
        "run_id": RUN,
        "operation": "ingest",
        "stage": "embed",
        "search_index_id": INDEX,
    }
    values.update(overrides)
    return fakes.row(**values)


def test_expired_leases_requeue_with_claims_left(fakes: SimpleNamespace) -> None:
    connection = fakes.FakeSql(fakes.Rows([_expired_row(fakes, "queued")])).connection

    expired = duties.expire_leases(connection, 3)

    assert expired.requeued == (ITEM,)
    assert expired.failed == ()
    assert expired.run_ids == (RUN,)
    (statement,) = connection.executed
    assert "CASE WHEN (app.ingestion_run_items.claim_count <" in statement.sql
    assert "lease_expires_at < SYSUTCDATETIME()" in statement.sql
    assert "OUTPUT inserted.id, inserted.state" in statement.sql


def test_the_last_expiry_fails_the_item_with_the_failure_side_effects(
    fakes: SimpleNamespace,
) -> None:
    connection = fakes.FakeSql(
        fakes.Rows([_expired_row(fakes, "failed")]),
        fakes.Rows(rowcount=1),
        fakes.Rows(rowcount=1),
    ).connection

    expired = duties.expire_leases(connection, 3)

    (failed,) = expired.failed
    assert (failed.id, failed.operation, failed.stage) == (ITEM, ItemOperation.INGEST, "embed")
    # The shared side effects of `docchat_core.ingestion.runs`, keyed by the item.
    _, retire, fail_version = connection.executed
    assert "UPDATE app.chunk_sets SET state=N'retired'" in retire.sql
    assert ITEM in retire.params.values()
    assert "UPDATE app.document_versions SET state=N'failed'" in fail_version.sql
    assert fail_version.params["failure_code"] == "processing_interrupted"


def test_stale_uploads_and_their_guarded_removal(fakes: SimpleNamespace) -> None:
    reserved = datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None)
    connection = fakes.FakeSql(
        fakes.Rows(
            [fakes.row(id=VERSION, document_id=DOCUMENT, blob_path="p", reserved_at=reserved)]
        ),
        fakes.Rows(rowcount=1),
        fakes.Rows(rowcount=1),
    ).connection

    (upload,) = duties.stale_uploads(connection, 24)
    assert duties.remove_stale_upload(connection, upload) is True

    select, delete_version, delete_document = connection.executed
    assert "dateadd(hour," in select.sql
    assert select.params["dateadd_1"] == -24
    assert "reserved_at = " in delete_version.sql
    assert delete_version.params["reserved_at_1"] == reserved
    assert "current_version_id IS NULL" in delete_document.sql
    assert "NOT (EXISTS (SELECT" in delete_document.sql


def test_orphaned_versions_get_one_maintenance_run_per_index(fakes: SimpleNamespace) -> None:
    other_version = UUID("9f506172-8394-4ea5-8fb6-d7e8f9a0b1c2")
    connection = fakes.FakeSql(
        fakes.Rows(
            [
                fakes.row(id=VERSION, document_id=DOCUMENT, search_index_id=INDEX),
                fakes.row(id=other_version, document_id=DOCUMENT, search_index_id=OTHER_INDEX),
            ]
        ),
        fakes.Rows(
            [
                fakes.row(id=INDEX, active_physical_index_id=ACTIVE),
                fakes.row(id=OTHER_INDEX, active_physical_index_id=None),
            ]
        ),
        fakes.Rows([fakes.row(search_index_id=OTHER_INDEX, id=BUILDING)]),
    ).connection

    runs = duties.queue_orphaned_versions(connection, 15)

    assert len(runs) == 2
    select = connection.executed[0]
    assert "scan_status IN (N'not_required', N'awaiting', N'clean')" in select.sql
    items = _item_rows(connection.executed)
    assert {(row["physical_index_id"], row["document_version_id"]) for row in items} == {
        (ACTIVE, VERSION),
        (BUILDING, other_version),
    }
    assert all(row["operation"] == "ingest" and row["priority"] == 0 for row in items)


def test_an_orphan_of_an_index_without_a_physical_index_is_left(fakes: SimpleNamespace) -> None:
    connection = fakes.FakeSql(
        fakes.Rows([fakes.row(id=VERSION, document_id=DOCUMENT, search_index_id=INDEX)]),
        fakes.Rows([fakes.row(id=INDEX, active_physical_index_id=None)]),
        fakes.Rows([]),
    ).connection

    assert duties.queue_orphaned_versions(connection, 15) == []


def test_nothing_orphaned_reads_no_target(fakes: SimpleNamespace) -> None:
    connection = fakes.FakeSql(fakes.Rows([])).connection

    assert duties.queue_orphaned_versions(connection, 15) == []
    assert len(connection.executed) == 1


def test_leftover_cleanup_items_per_document(fakes: SimpleNamespace) -> None:
    connection = fakes.FakeSql(
        fakes.Rows([fakes.row(document_id=DOCUMENT, search_index_id=INDEX)])
    ).connection

    (run,) = duties.queue_leftover_cleanup(connection, 15)

    select = connection.executed[0]
    assert "UNION" in select.sql
    assert "status NOT IN (N'deleting', N'deleted')" in select.sql
    (item,) = _item_rows(connection.executed)
    assert (item["operation"], item["document_id"]) == ("cleanup_document", DOCUMENT)
    assert run is not None


def test_due_deletions_get_delete_items(fakes: SimpleNamespace) -> None:
    connection = fakes.FakeSql(fakes.Rows([fakes.row(id=ACTIVE, search_index_id=INDEX)])).connection

    duties.queue_due_deletions(connection)

    assert "state = N'retiring'" in connection.executed[0].sql
    assert connection.executed[0].params["dateadd_1"] == -15
    (item,) = _item_rows(connection.executed)
    assert (item["operation"], item["physical_index_id"]) == ("delete_physical_index", ACTIVE)


def test_counts_per_priority_with_the_oldest_age(fakes: SimpleNamespace) -> None:
    connection = fakes.FakeSql(
        fakes.Rows(
            [fakes.row(priority=0, items=2, age=30), fakes.row(priority=1, items=5, age=900)]
        ),
        fakes.Rows([1]),
    ).connection

    counted = duties.counts(connection)

    assert counted.queued == {"interactive": 2, "bulk": 5}
    assert counted.running == 1
    assert counted.oldest_queued_age_seconds == 900
    assert counted.has_outstanding_work is True
    assert "datediff(second," in connection.executed[0].sql


def test_counts_with_nothing_queued_look_for_other_outstanding_work(fakes: SimpleNamespace) -> None:
    connection = fakes.FakeSql(fakes.Rows([]), fakes.Rows([0]), fakes.Rows([1])).connection

    counted = duties.counts(connection)

    assert counted.oldest_queued_age_seconds is None
    assert counted.has_outstanding_work is True
    assert "state IN (N'building', N'retiring')" in connection.executed[2].sql


def test_counts_of_an_idle_ledger(fakes: SimpleNamespace) -> None:
    connection = fakes.FakeSql(fakes.Rows([]), fakes.Rows([0]), fakes.Rows([0])).connection

    assert duties.counts(connection).has_outstanding_work is False


def test_stale_renditions_queue_bulk_rerender_items(fakes: SimpleNamespace) -> None:
    connection = fakes.FakeSql(
        fakes.Rows([3]),
        fakes.Rows(
            [
                fakes.row(
                    id=CHUNK_SET,
                    document_id=DOCUMENT,
                    document_version_id=VERSION,
                    physical_index_id=ACTIVE,
                    search_index_id=INDEX,
                )
            ]
        ),
    ).connection

    (run,) = duties.queue_stale_renditions(connection, 2)

    select = connection.executed[1]
    assert "row_number() OVER (PARTITION BY app.chunk_sets.document_id" in select.sql
    assert (
        "rendition_rules_version IS NULL OR app.chunk_sets.rendition_rules_version <" in select.sql
    )
    assert (
        "outcome_code NOT IN (N'storage_unavailable', N'activity_timeout', "
        "N'processing_interrupted')" in select.sql
    )
    assert select.params["param_1"] == 7
    (item,) = _item_rows(connection.executed)
    assert (item["operation"], item["chunk_set_id"], item["priority"]) == ("rerender", CHUNK_SET, 1)
    assert run is not None


def test_stale_renditions_stop_at_ten_outstanding(fakes: SimpleNamespace) -> None:
    connection = fakes.FakeSql(fakes.Rows([10])).connection

    assert duties.queue_stale_renditions(connection, 2) == []
    assert len(connection.executed) == 1
