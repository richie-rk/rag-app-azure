"""Unit tests of `flip_chunk_set` (`14-ingestion.md` section 6.9; ADRs 0040 and 0041): every
branch of the transaction in order, on a scripted connection."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from docchat_ingestion.activities import flip_chunk_set
from docchat_ingestion.activities.documents import LeaseLost, PlanInput
from docchat_ingestion.activities.flip_chunk_set import FlipInvariantError, decide

COMMITTED = datetime(2026, 1, 1, tzinfo=UTC)


def plan_input(kit: SimpleNamespace, ids: Any) -> PlanInput:
    return PlanInput.model_validate(
        kit.plan_payload(ids, kit.make_plan(ids)), by_alias=True, by_name=False
    )


def opening(kit: SimpleNamespace, active: Any, *, target_state: str = "active") -> list[Any]:
    return [
        kit.Result(rows=[(active,)]),  # the logical index, locked
        kit.fence_ok(),
        kit.Result(rows=[]),  # no served row yet
        kit.Result(rows=[(target_state,)]),  # the target, locked
    ]


def version(kit: SimpleNamespace) -> Any:
    return kit.Result(rows=[kit.Row(committed_at=COMMITTED, version_number=2)])


def run(kit: SimpleNamespace, ids: Any, results: list[Any]) -> tuple[Any, Any]:
    connection = kit.ScriptedConnection(*results)
    flipped = decide(connection, plan_input(kit, ids))
    connection.finished()
    return flipped, connection


def test_a_lost_claim(kit: SimpleNamespace) -> None:
    ids = kit.Ids()
    connection = kit.ScriptedConnection(kit.Result(rows=[(ids.physical,)]), kit.Result(rowcount=0))
    with pytest.raises(LeaseLost):
        decide(connection, plan_input(kit, ids))
    locked, fenced = connection.executed
    assert "search_indexes WITH (UPDLOCK, ROWLOCK)" in locked.sql
    assert fenced.values["dateadd_1"] == 720


def test_t165_an_earlier_commit_is_flipped_with_empty_lists(kit: SimpleNamespace) -> None:
    ids = kit.Ids()
    results = [kit.Result(rows=[(ids.physical,)]), kit.fence_ok(), kit.Result(rows=[(1,)])]
    flipped, connection = run(kit, ids, results)
    assert flipped.outcome == "flipped"
    assert flipped.retired == ()
    result = flipped.result().model_dump(mode="json")
    assert result["retiredChunkSetIds"] == []
    assert result["supersededVersionId"] is None
    assert result["buildingItemQueued"] is False
    # The lock, the lease fence and the served-row check: the fence is the claim's own lease
    # write, and no statement after it changes a row.
    locked, fenced, served = connection.executed
    assert locked.sql.startswith("SELECT")
    assert fenced.sql.startswith("UPDATE app.ingestion_run_items SET stage")
    assert served.sql.startswith("SELECT")
    assert "served_chunk_sets" in served.sql


def test_a_repointed_target_requeues_the_item(kit: SimpleNamespace) -> None:
    ids = kit.Ids()
    active = uuid4()
    results = [
        *opening(kit, active, target_state="retiring"),
        kit.Result(rowcount=1),  # build retired
        kit.Result(rows=[]),  # not covered
        kit.Result(rowcount=1),  # requeued
    ]
    flipped, connection = run(kit, ids, results)
    assert flipped.outcome == "requeued"
    assert flipped.retired == (ids.chunk_set,)
    (requeued,) = connection.statements("UPDATE app.ingestion_run_items SET physical_index_id")
    assert "state=N'queued'" in requeued.sql
    assert "not_before=SYSUTCDATETIME()" in requeued.sql
    assert requeued.values["physical_index_id"] == active


def test_a_requeue_that_finds_no_claim_breaks_the_invariant(kit: SimpleNamespace) -> None:
    ids = kit.Ids()
    results = [
        *opening(kit, uuid4(), target_state="retiring"),
        kit.Result(rowcount=1),
        kit.Result(rows=[]),
        kit.Result(rowcount=0),
    ]
    with pytest.raises(FlipInvariantError):
        run(kit, ids, results)


@pytest.mark.parametrize(
    ("covered", "code"), [(True, "target_repointed"), (False, "target_retired")]
)
def test_a_gone_target_skips(kit: SimpleNamespace, covered: bool, code: str) -> None:
    ids = kit.Ids()
    active = uuid4() if covered else None
    results = [*opening(kit, active, target_state="deleted"), kit.Result(rowcount=1)]
    if covered:
        results.append(kit.Result(rows=[(1,)]))
    flipped, _ = run(kit, ids, results)
    assert flipped.outcome == "skipped"
    assert flipped.code == code
    assert flipped.retired == (ids.chunk_set,)


def serving(kit: SimpleNamespace, retired: list[Any], *, pointed: int = 1) -> list[Any]:
    return [
        kit.Result(rowcount=1),  # ingested
        kit.Result(rows=[(chunk_set,) for chunk_set in retired]),  # retired served sets
        kit.Result(rowcount=pointed),  # served row updated
        kit.Result(rowcount=1),  # build served
    ]


def test_a_new_version_becomes_current(kit: SimpleNamespace) -> None:
    ids = kit.Ids()
    previous, old_set = uuid4(), uuid4()
    results = [
        *opening(kit, ids.physical),
        version(kit),
        kit.Result(rows=[(previous,)]),  # guard (4a), OUTPUT deleted.current_version_id
        kit.Result(rowcount=1),  # previous superseded
        *serving(kit, [old_set]),
        kit.Result(rows=[]),  # no building index
    ]
    flipped, connection = run(kit, ids, results)
    assert flipped.outcome == "flipped"
    assert flipped.superseded == previous
    assert flipped.retired == (old_set,)
    assert not flipped.building_item_queued
    (guard,) = connection.statements("UPDATE app.documents SET current_version_id")
    assert "OUTPUT deleted.current_version_id" in guard.sql
    assert "is_deleted = 0" in guard.sql
    assert "NOT (EXISTS" in guard.sql


def test_a_rebuild_queues_the_version_for_the_building_index(kit: SimpleNamespace) -> None:
    ids = kit.Ids()
    building, build_run = uuid4(), uuid4()
    results = [
        *opening(kit, ids.physical),
        version(kit),
        kit.Result(rows=[(None,)]),  # first version: nothing current before
        *serving(kit, [], pointed=0),
        kit.Result(rows=[kit.Row(id=building, build_run_id=build_run)]),
        kit.Result(rowcount=1),
    ]
    flipped, connection = run(kit, ids, results)
    assert flipped.building_item_queued
    assert flipped.superseded is None
    (inserted_row,) = connection.statements("INSERT INTO app.served_chunk_sets")
    assert inserted_row.values["chunk_set_id"] == ids.chunk_set
    (queued,) = connection.statements("INSERT INTO app.ingestion_run_items")
    assert "newid()" in queued.sql.lower()
    assert build_run in queued.values.values()
    assert building in queued.values.values()


def test_a_building_target_only_touches_the_current_version(kit: SimpleNamespace) -> None:
    ids = kit.Ids()
    active = uuid4()
    results = [
        *opening(kit, active, target_state="building"),
        version(kit),
        kit.Result(rowcount=1),  # guard (4b)
        *serving(kit, []),
    ]
    flipped, connection = run(kit, ids, results)
    assert flipped.outcome == "flipped"
    assert flipped.superseded is None
    assert not connection.statements("UPDATE app.documents SET current_version_id")


def test_a_reingest_in_place_of_the_current_version(kit: SimpleNamespace) -> None:
    ids = kit.Ids()
    results = [
        *opening(kit, ids.physical),
        version(kit),
        kit.Result(rows=[]),  # (4a) changes nothing: already current
        kit.Result(rowcount=1),  # (4b)
        *serving(kit, [uuid4()]),
    ]
    flipped, connection = run(kit, ids, results)
    assert flipped.outcome == "flipped"
    assert flipped.superseded is None
    assert not connection.statements("INSERT INTO app.ingestion_run_items")


@pytest.mark.parametrize(
    ("deleted", "code"), [(True, "document_deleted"), (False, "superseded_by_newer")]
)
def test_zero_rows_from_the_guard_skip(kit: SimpleNamespace, deleted: bool, code: str) -> None:
    ids = kit.Ids()
    results = [
        *opening(kit, ids.physical),
        version(kit),
        kit.Result(rows=[]),
        kit.Result(rowcount=0),
        kit.Result(rowcount=1),  # build retired
        kit.Result(rowcount=1),  # version superseded
        kit.Result(rows=[(deleted,)]),
    ]
    flipped, connection = run(kit, ids, results)
    assert flipped.outcome == "skipped"
    assert flipped.code == code
    assert flipped.retired == (ids.chunk_set,)
    (superseded,) = connection.statements("UPDATE app.document_versions SET state=N'superseded'")
    assert "state = N'ingesting'" in superseded.sql


def test_a_version_that_cannot_be_ingested_breaks_the_invariant(kit: SimpleNamespace) -> None:
    ids = kit.Ids()
    results = [
        *opening(kit, ids.physical),
        version(kit),
        kit.Result(rows=[]),
        kit.Result(rowcount=1),
        kit.Result(rowcount=0),
    ]
    with pytest.raises(FlipInvariantError):
        run(kit, ids, results)


def test_a_build_that_isnt_ready_breaks_the_invariant(kit: SimpleNamespace) -> None:
    ids = kit.Ids()
    results = [
        *opening(kit, ids.physical),
        version(kit),
        kit.Result(rows=[]),
        kit.Result(rowcount=1),
        kit.Result(rowcount=1),
        kit.Result(rows=[]),
        kit.Result(rowcount=1),
        kit.Result(rowcount=0),
    ]
    with pytest.raises(FlipInvariantError):
        run(kit, ids, results)


async def test_the_activity(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    world.sql.script(kit.Result(rows=[(ids.physical,)]), kit.fence_ok(), kit.Result(rows=[(1,)]))
    output = await flip_chunk_set.run(kit.plan_payload(ids, kit.make_plan(ids)), world.provider)
    assert output == {
        "ok": True,
        "errorCode": None,
        "retryable": False,
        "retryAfterSeconds": None,
        "outcome": "flipped",
        "code": None,
        "retiredChunkSetIds": [],
        "supersededVersionId": None,
        "buildingItemQueued": False,
    }
    assert world.sql.units == [("flip_chunk_set", "transient")]


async def test_a_broken_invariant_is_internal_error(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    world.sql.failures.append(FlipInvariantError())
    output = await flip_chunk_set.run(kit.plan_payload(ids, kit.make_plan(ids)), world.provider)
    assert output["errorCode"] == "internal_error"
    assert output["retiredChunkSetIds"] == []
