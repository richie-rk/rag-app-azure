"""The repoint on a scripted connection (`14-ingestion.md` section 8.9; the effect on SQL Server is
T-178's)."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

from docchat_ingestion.activities import repoint

INDEX = UUID("3f2c0d9e-8b7a-41c6-a5d4-e3f2c1b0a998")
OLD = UUID("1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d")
NEW = UUID("2b3c4d5e-6f7a-4b8c-9d0e-1f2a3b4c5d6e")
RUN = UUID("2d3e4f5a-6b7c-4d8e-9f0a-1b2c3d4e5f60")

REBUILDING = repoint.RebuildingIndex(INDEX, NEW, 2, RUN)


def test_rebuilding_indexes_with_a_provisioned_building_index(fakes: SimpleNamespace) -> None:
    connection = fakes.FakeSql(
        fakes.Rows([fakes.row(id=INDEX, building_id=NEW, generation=2, build_run_id=RUN)])
    ).connection

    assert repoint.rebuilding_indexes(connection) == [REBUILDING]
    sql = connection.executed[0].sql
    assert "status = N'rebuilding'" in sql
    assert "provisioned_at IS NOT NULL" in sql


def test_the_condition_counts_live_documents_the_building_index_lacks(
    fakes: SimpleNamespace,
) -> None:
    connection = fakes.FakeSql(fakes.Rows([4])).connection

    assert repoint.missing_documents(connection, INDEX, NEW) == 4
    sql = connection.executed[0].sql
    assert "current_version_id IS NOT NULL" in sql
    assert "served_chunk_sets.document_version_id = app.documents.current_version_id" in sql


def test_the_repoint_moves_the_pointer_once_and_queues_the_old_index_delete(
    fakes: SimpleNamespace,
) -> None:
    connection = fakes.FakeSql(
        fakes.Rows([OLD]),
        fakes.Rows([0]),
        *(fakes.Rows(rowcount=1) for _ in range(6)),
    ).connection

    done = repoint.repoint(connection, REBUILDING)

    assert done == repoint.Repointed(INDEX, OLD, NEW, 2)
    sql = [item.sql for item in connection.executed]
    assert "WITH (UPDLOCK, ROWLOCK)" in sql[0]
    assert "SET active_physical_index_id=" in sql[2]
    assert "active_physical_index_id = " in sql[2]
    # The old index retires before the new one activates (`UX_physical_indexes_one_active`).
    assert "SET state=N'retiring'" in sql[3]
    assert "dateadd(minute," in sql[3]
    assert connection.executed[3].params["dateadd_1"] == 10
    assert "SET state=N'active'" in sql[4]
    assert "SET state=N'cancelled'" in sql[5]
    assert connection.executed[5].params["outcome_code"] == "target_repointed"
    assert "SET physical_index_id=" in sql[6]
    (insert,) = connection.inserts()
    assert "INSERT INTO app.ingestion_run_items" in insert.sql
    assert "app.physical_indexes.delete_after" in insert.sql
    assert "N'delete_physical_index'" in insert.sql
    assert "SET build_run_id=" in sql[-1]


def test_a_condition_that_stopped_holding_commits_nothing(fakes: SimpleNamespace) -> None:
    connection = fakes.FakeSql(fakes.Rows([OLD]), fakes.Rows([1])).connection

    assert repoint.repoint(connection, REBUILDING) is None
    assert len(connection.executed) == 2


def test_a_logical_index_without_an_active_index_points_to_the_new_one(
    fakes: SimpleNamespace,
) -> None:
    connection = fakes.FakeSql(
        fakes.Rows([None]),
        fakes.Rows([0]),
        fakes.Rows(rowcount=1),
        fakes.Rows(rowcount=1),
        fakes.Rows(rowcount=1),
    ).connection

    done = repoint.repoint(connection, REBUILDING)

    assert done is not None
    assert done.old_physical_index_id is None
    assert "active_physical_index_id IS NULL" in connection.executed[2].sql
    assert connection.inserts() == []
