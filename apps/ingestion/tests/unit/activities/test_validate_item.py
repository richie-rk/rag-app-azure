"""Unit tests of `validate_item` (`14-ingestion.md` sections 6.2, 5.4 and 5.7): every decision in
order, the build with its chunk-set reuse or insert, the conversion plan, the lost claim and the
build-key race."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from docchat_core.ingestion.methods import PAGEWISE_V1, PagewiseParametersV1
from docchat_core.ingestion.models import ItemEnvelope
from docchat_core.ingestion.names import source_path
from docchat_core.sql.engine import SqlUnavailable, SqlUniqueViolation
from docchat_ingestion.activities import validate_item
from docchat_ingestion.activities.documents import LeaseLost
from docchat_ingestion.activities.validate_item import ValidateResult, conversion_plan, decide

COMMITTED = datetime(2026, 1, 1, 9, tzinfo=UTC)
PARAMETERS = PagewiseParametersV1()
HASH = PAGEWISE_V1.parameter_hash(PARAMETERS)
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def item_row(kit: SimpleNamespace, ids: Any, chunk_set: UUID | None = None) -> Any:
    return kit.Result(
        rows=[
            kit.Row(
                run_id=ids.run,
                search_index_id=ids.index,
                physical_index_id=ids.physical,
                document_id=ids.document,
                document_version_id=ids.version,
                chunk_set_id=chunk_set,
            )
        ]
    )


def version_row(
    kit: SimpleNamespace,
    *,
    state: str = "pending",
    content_type: str = "application/pdf",
    converter_version: str | None = None,
    converted: bytes | None = None,
) -> Any:
    return kit.Result(
        rows=[
            kit.Row(
                state=state,
                committed_at=COMMITTED,
                version_number=2,
                sha256=bytes.fromhex(kit.sha(kit.PDF_BYTES)),
                content_type=content_type,
                source_name="Annual report.pdf"
                if content_type == "application/pdf"
                else "Notes.docx",
                blob_path="",
                converter_version=converter_version,
                converted_sha256=converted,
            )
        ]
    )


def target_row(
    kit: SimpleNamespace,
    *,
    is_deleted: bool = False,
    active: UUID | None = None,
    state: str = "active",
) -> Any:
    return kit.Result(
        rows=[
            kit.Row(
                is_deleted=is_deleted,
                current_version_id=None,
                status="ready",
                active_physical_index_id=active,
                method_key="pagewise",
                method_version=1,
                method_parameters=json.dumps(PARAMETERS.dumped()),
                method_parameter_hash=HASH,
                state=state,
                name="docchat-0123456789abcdef0123456789abcdef-g1",
                schema_version=1,
                embedding_model="text-embedding-3-large",
                embedding_dimensions=1024,
                embedding_encoding="cl100k_base",
                embedding_max_input_tokens=8192,
                ingestion_embedding_deployment="embed-ingest-te3-large-v1",
            )
        ]
    )


def with_blob_path(kit: SimpleNamespace, ids: Any, result: Any, content_type: str) -> Any:
    row = result.rows[0]
    values = dict(zip(row._names, row, strict=True))
    values["blob_path"] = source_path(ids.index, ids.document, 2)
    values["content_type"] = content_type
    return kit.Result(rows=[kit.Row(**values)])


def start(
    kit: SimpleNamespace, ids: Any, *, version: Any = None, target: Any = None, **item: Any
) -> Any:
    version = version or version_row(kit)
    return [
        kit.fence_ok(),
        item_row(kit, ids, **item),
        with_blob_path(kit, ids, version, version.rows[0].content_type),
        target or target_row(kit, active=ids.physical),
    ]


def envelope_model(kit: SimpleNamespace, ids: Any) -> Any:
    return ItemEnvelope.model_validate(kit.envelope(ids), by_alias=True, by_name=False)


def run_decide(
    kit: SimpleNamespace, ids: Any, results: list[Any], new_id: UUID | None = None
) -> Any:
    connection = kit.ScriptedConnection(*results)
    decision = decide(
        connection,
        envelope_model(kit, ids),
        converter_version=kit.CONVERTER,
        new_id=lambda: new_id or uuid4(),
    )
    connection.finished()
    return decision, connection


def test_a_lost_claim_writes_nothing(kit: SimpleNamespace) -> None:
    ids = kit.Ids()
    connection = kit.ScriptedConnection(kit.Result(rowcount=0))
    with pytest.raises(LeaseLost):
        decide(connection, envelope_model(kit, ids), converter_version=kit.CONVERTER)
    (fenced,) = connection.executed
    assert "lease_expires_at=dateadd(second" in fenced.sql
    assert fenced.values["dateadd_1"] == 720
    assert fenced.values["claim_count_1"] == 1


def test_a_retired_target_without_an_active_index_skips(kit: SimpleNamespace) -> None:
    ids = kit.Ids()
    decision, _ = run_decide(kit, ids, start(kit, ids, target=target_row(kit, state="retiring")))
    assert decision.skip == "target_retired"


def test_t163_a_repointed_target_covered_elsewhere_skips(kit: SimpleNamespace) -> None:
    ids = kit.Ids()
    active = uuid4()
    results = [
        *start(kit, ids, target=target_row(kit, active=active, state="retiring")),
        kit.Result(rows=[kit.Row(one=1)]),
    ]
    decision, connection = run_decide(kit, ids, results)
    assert decision.skip == "target_repointed"
    assert connection.statements("INSERT INTO app.ingestion_run_items") == []
    assert connection.statements("UPDATE app.ingestion_run_items SET physical_index_id") == []


def test_t163_a_repointed_target_retargets_the_item_and_builds(kit: SimpleNamespace) -> None:
    ids = kit.Ids()
    active = uuid4()
    chunk_set = uuid4()
    results = [
        *start(kit, ids, target=target_row(kit, active=active, state="deleted")),
        kit.Result(rows=[]),
        kit.Result(rowcount=1),
        target_row(kit, active=active, state="active"),
        kit.Result(rows=[]),  # newer commit
        kit.Result(rows=[]),  # already served
        kit.Result(rows=[]),  # chunk set of the build key
        kit.Result(rowcount=1),  # version to ingesting
        kit.Result(rowcount=1),  # link
    ]
    decision, connection = run_decide(kit, ids, results, new_id=chunk_set)
    assert decision.plan.physical_index.id == active
    assert decision.plan.chunk_set_id == chunk_set
    (retargeted,) = connection.statements("UPDATE app.ingestion_run_items SET physical_index_id")
    assert retargeted.values["physical_index_id"] == active
    # In place: the claimed item moves to the active index, and no item is added for it.
    assert connection.statements("INSERT INTO app.ingestion_run_items") == []


def test_a_retarget_with_a_lost_claim_raises(kit: SimpleNamespace) -> None:
    ids = kit.Ids()
    results = [
        *start(kit, ids, target=target_row(kit, active=uuid4(), state="retiring")),
        kit.Result(rows=[]),
        kit.Result(rowcount=0),
    ]
    with pytest.raises(LeaseLost):
        run_decide(kit, ids, results)


@pytest.mark.parametrize(
    ("version_state", "deleted", "code"),
    [
        ("pending", True, "document_deleted"),
        ("superseded", False, "superseded_by_newer"),
        ("failed", False, "version_not_ready"),
        ("uploading", False, "version_not_ready"),
    ],
)
def test_skips_in_order(kit: SimpleNamespace, version_state: str, deleted: bool, code: str) -> None:
    ids = kit.Ids()
    results = start(
        kit,
        ids,
        version=version_row(kit, state=version_state),
        target=target_row(kit, active=ids.physical, is_deleted=deleted),
    )
    decision, _ = run_decide(kit, ids, results)
    assert decision.skip == code


def test_a_newer_commit_supersedes_a_pending_version(kit: SimpleNamespace) -> None:
    ids = kit.Ids()
    results = [*start(kit, ids), kit.Result(rows=[kit.Row(one=1)]), kit.Result(rowcount=1)]
    decision, connection = run_decide(kit, ids, results)
    assert decision.skip == "superseded_by_newer"
    (superseded,) = connection.statements("UPDATE app.document_versions SET state=N'superseded'")
    assert "state = N'pending'" in superseded.sql


def test_an_already_served_build_skips(kit: SimpleNamespace) -> None:
    ids = kit.Ids()
    results = [
        *start(kit, ids, version=version_row(kit, state="ingested")),
        kit.Result(rows=[kit.Row(id=uuid4())]),
    ]
    decision, _ = run_decide(kit, ids, results)
    assert decision.skip == "already_served"


def test_a_build_inserts_its_chunk_set_and_links_it(kit: SimpleNamespace) -> None:
    ids = kit.Ids()
    chunk_set = uuid4()
    results = [
        *start(kit, ids),
        kit.Result(rows=[]),  # newer commit
        kit.Result(rows=[]),  # already served
        kit.Result(rows=[]),  # build key
        kit.Result(rowcount=1),
        kit.Result(rowcount=1),
    ]
    decision, connection = run_decide(kit, ids, results, new_id=chunk_set)
    plan = decision.plan
    assert plan.chunk_set_id == chunk_set
    assert plan.conversion is None
    assert plan.title == "Annual report"
    assert plan.work_prefix == f"{ids.run}/{ids.document}/2/"
    assert plan.method.key == "pagewise"
    assert plan.method.parameter_hash == HASH.hex()
    assert plan.physical_index.is_active
    (inserted,) = connection.statements("INSERT INTO app.chunk_sets")
    assert inserted.values["build_item_id"] == ids.item
    (lock,) = [
        entry for entry in connection.executed if "chunk_sets WITH (UPDLOCK, ROWLOCK)" in entry.sql
    ]
    assert "N'building', N'ready', N'served'" in lock.sql


def test_a_retry_reuses_the_build_and_retires_another_claims_set(kit: SimpleNamespace) -> None:
    ids = kit.Ids()
    existing, earlier = uuid4(), uuid4()
    results = [
        *start(kit, ids, version=version_row(kit, state="ingesting"), chunk_set=earlier),
        kit.Result(rows=[]),  # already served
        kit.Result(rows=[kit.Row(id=existing, state="building")]),
        kit.Result(rowcount=1),  # retire the earlier claim's set
        kit.Result(rowcount=0),  # version already ingesting
        kit.Result(rowcount=1),
    ]
    decision, connection = run_decide(kit, ids, results)
    assert decision.plan.chunk_set_id == existing
    assert not connection.statements("INSERT INTO app.chunk_sets")
    (retired,) = connection.statements("UPDATE app.chunk_sets SET state=N'retired'")
    assert retired.values["id_1"] == earlier


def test_a_build_whose_link_finds_no_claim_raises(kit: SimpleNamespace) -> None:
    ids = kit.Ids()
    results = [
        *start(kit, ids, version=version_row(kit, state="ingested")),
        kit.Result(rows=[]),
        kit.Result(rows=[kit.Row(id=uuid4(), state="ready")]),
        kit.Result(rowcount=0),
        kit.Result(rowcount=0),
    ]
    with pytest.raises(LeaseLost):
        run_decide(kit, ids, results)


def test_an_office_version_converts_under_another_converter(kit: SimpleNamespace) -> None:
    ids = kit.Ids()
    results = [
        *start(
            kit,
            ids,
            version=version_row(
                kit,
                content_type=DOCX,
                converter_version="lo-old/aaaaaaaaaaaa",
                converted=b"\x01" * 32,
            ),
        ),
        kit.Result(rows=[]),
        kit.Result(rows=[]),
        kit.Result(rows=[]),
        kit.Result(rowcount=1),
        kit.Result(rowcount=1),
    ]
    decision, _ = run_decide(kit, ids, results)
    conversion = decision.plan.conversion
    assert conversion.convert
    assert conversion.converted_sha256 is None
    assert conversion.converted_blob_path == f"{ids.index}/{ids.document}/2/converted.pdf"
    assert decision.plan.title == "Notes"


def _conversion(kit: SimpleNamespace, ids: Any, **kwargs: Any) -> Any:
    item = validate_item.ItemRow(ids.run, ids.index, ids.physical, ids.document, ids.version, None)
    version_values = dict(
        version_row(kit, content_type=DOCX, **kwargs.pop("version", {})).rows[0]._asdict()
    )
    version = validate_item.VersionRow(**version_values)
    target_values = dict(target_row(kit, **kwargs.pop("target", {})).rows[0]._asdict())
    target = validate_item.TargetRow(**target_values)
    return conversion_plan(item, version, target, ids.physical, kit.CONVERTER)


def test_the_conversion_plan(kit: SimpleNamespace) -> None:
    ids = kit.Ids()
    stored = b"\x02" * 32
    same = _conversion(kit, ids, version={"converter_version": kit.CONVERTER, "converted": stored})
    assert not same.convert
    assert same.converted_sha256 == stored.hex()
    rebuild = _conversion(
        kit,
        ids,
        version={"converter_version": "lo-older", "converted": stored},
        target={"state": "building", "active": uuid4()},
    )
    assert not rebuild.convert
    in_place = _conversion(kit, ids, version={"converter_version": "lo-older", "converted": stored})
    assert in_place.convert
    never = _conversion(kit, ids, version={"converter_version": None, "converted": None})
    assert never.convert


def test_validate_result_carries_a_plan_or_a_skip(kit: SimpleNamespace) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids)
    built = ValidateResult.build(plan)
    assert built.plan_fields()["action"] == "build"
    assert "ok" not in built.plan_fields()
    skipped = ValidateResult.skip("already_served")
    assert skipped.model_dump(mode="json")["skipCode"] == "already_served"
    with pytest.raises(ValueError, match="skip names"):
        ValidateResult.success(action="skip")
    with pytest.raises(ValueError, match="builds or skips"):
        ValidateResult.success()


async def test_the_activity_returns_the_plan(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    world.sql.script(
        *start(kit, ids),
        kit.Result(rows=[]),
        kit.Result(rows=[]),
        kit.Result(rows=[]),
        kit.Result(rowcount=1),
        kit.Result(rowcount=1),
    )
    output = await validate_item.run(kit.envelope(ids), world.provider)
    assert output["ok"] is True
    assert output["action"] == "build"
    assert output["physicalIndex"]["dimensions"] == 1024
    assert world.sql.units == [("validate_item", "transient")]


async def test_a_build_key_race_runs_the_transaction_once_more(
    kit: SimpleNamespace, world: Any
) -> None:
    ids = kit.Ids()
    world.sql.failures.append(SqlUniqueViolation(2601, "23000", "UX_chunk_sets_build_key"))
    world.sql.script(*start(kit, ids), kit.Result(rows=[]), kit.Result(rows=[kit.Row(id=1)]))
    output = await validate_item.run(kit.envelope(ids), world.provider)
    assert output["skipCode"] == "already_served"
    assert len(world.sql.units) == 2


async def test_a_second_race_is_internal_error(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    world.sql.failures.extend(
        [
            SqlUniqueViolation(2601, "23000", "UX_chunk_sets_build_key"),
            SqlUniqueViolation(2601, "23000", "UX_chunk_sets_build_key"),
        ]
    )
    output = await validate_item.run(kit.envelope(ids), world.provider)
    assert output["errorCode"] == "internal_error"
    assert output["retryable"] is True


async def test_sql_out_of_reach_is_database_unavailable(kit: SimpleNamespace, world: Any) -> None:
    world.sql.failures.append(SqlUnavailable("exhausted"))
    output = await validate_item.run(kit.envelope(kit.Ids()), world.provider)
    assert output == {
        **output,
        "ok": False,
        "errorCode": "database_unavailable",
        "retryable": True,
    }


async def test_a_lost_claim_is_lease_lost(kit: SimpleNamespace, world: Any) -> None:
    world.sql.script(kit.Result(rowcount=0))
    output = await validate_item.run(kit.envelope(kit.Ids()), world.provider)
    assert output["errorCode"] == "lease_lost"
    assert output["retryable"] is False


async def test_a_malformed_input_is_internal_error(world: Any) -> None:
    output = await validate_item.run({"itemId": "not a uuid"}, world.provider)
    assert output["errorCode"] == "internal_error"
