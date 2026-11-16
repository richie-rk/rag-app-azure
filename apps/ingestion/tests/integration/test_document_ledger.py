"""The document path's SQL against the SQL Server container (T-164, T-168, T-169, T-170, T-172,
T-173 and the ledger halves of T-166, T-171, T-174 and T-615; `14-ingestion.md` sections 5.7,
6.2, 6.3, 6.5, 6.7, 6.8, 6.9 and 6.11).

Each unit of work runs as `docchat_ingestion`, the role that runs it in production, on a
synchronous connection inside one transaction, as the data-access layer runs it; rows are arranged
and read back as `sa`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, Table, insert, select, update

from docchat_core.ingestion.methods import PAGEWISE_V1, PagewiseParametersV1, compact_json
from docchat_core.ingestion.models import ItemEnvelope, ItemSettings
from docchat_core.ingestion.runs import create_document_delete_run
from docchat_core.sql.tables.access import projects
from docchat_core.sql.tables.ingestion import (
    chunk_sets,
    document_versions,
    documents,
    ingestion_run_items,
    ingestion_runs,
    physical_indexes,
    search_indexes,
    served_chunk_sets,
)
from docchat_ingestion.activities import (
    chunk_document,
    claim_ingestion_items,
    extract_document,
    finalisation,
    flip_chunk_set,
    index_chunks,
    record_item_outcome,
    render_pages,
    sweep_duties,
    validate_item,
)
from docchat_ingestion.activities.documents import BuildPlan, PlanInput
from docchat_ingestion.outcomes import ItemOutcome
from docchat_ingestion.workfiles import RenditionMarker
from docchat_testing.builders import project_row
from docchat_testing.builders.ingestion import (
    document_row,
    document_version_row,
    ingestion_run_item_row,
    ingestion_run_row,
    physical_index_row,
    search_index_row,
)
from docchat_testing.builders.rows import sql_now
from docchat_testing.fixtures.sql import SqlTestDatabase

pytestmark = pytest.mark.integration

CONVERTER = "lo-4:7.4.7-1+deb12u14/0123456789ab"
PARAMETERS = PagewiseParametersV1()
SETTINGS = ItemSettings(
    activity_time_budget_seconds=1200,
    max_pages_per_document=2000,
    pagewise_pages_per_extract=40,
    max_parse_seconds_per_document=7200,
    embedding_batch_max_inputs=64,
    embedding_batch_max_tokens=60_000,
    embedding_concurrency=2,
    index_batch_max_documents=100,
    index_batch_max_bytes=8_000_000,
    table_summary_concurrency=4,
    table_summary_max_input_tokens=6000,
    table_summary_max_output_tokens=300,
)


@dataclass(frozen=True)
class Engines:
    admin: Engine
    ingestion: Engine
    api: Engine


@pytest.fixture
def engines(sql_database: SqlTestDatabase) -> Iterator[Engines]:
    created = Engines(
        admin=sql_database.engine(),
        ingestion=sql_database.engine("docchat_ingestion"),
        api=sql_database.engine("docchat_api"),
    )
    yield created
    created.admin.dispose()
    created.ingestion.dispose()
    created.api.dispose()


def put(engine: Engine, *rows: tuple[Table, dict[str, Any]]) -> None:
    with engine.begin() as connection:
        for table, row in rows:
            connection.execute(insert(table), row)


def unit(engine: Engine, work: Any) -> Any:
    with engine.begin() as connection:
        return work(connection)


def one(engine: Engine, statement: Any) -> Any:
    with engine.connect() as connection:
        return connection.execute(statement).one()


@dataclass(frozen=True)
class Index:
    project_id: UUID
    index_id: UUID
    active_id: UUID


def ready_index(engines: Engines) -> Index:
    project = project_row()
    index = search_index_row(
        status="ready",
        method_parameters=compact_json(PARAMETERS.dumped()),
        method_parameter_hash=PAGEWISE_V1.parameter_hash(PARAMETERS),
    )
    physical = physical_index_row(index["id"], state="active", provisioned_at=sql_now())
    put(engines.admin, (projects, project), (search_indexes, index), (physical_indexes, physical))
    with engines.admin.begin() as connection:
        connection.execute(
            update(search_indexes)
            .where(search_indexes.c.id == index["id"])
            .values(active_physical_index_id=physical["id"])
        )
    return Index(project["id"], index["id"], physical["id"])


def new_document(engines: Engines, index: Index) -> UUID:
    document = document_row(index.index_id)
    put(engines.admin, (documents, document))
    document_id: UUID = document["id"]
    return document_id


def pending_version(
    engines: Engines, index: Index, document_id: UUID, number: int, minutes_ago: int = 0
) -> UUID:
    version = document_version_row(
        document_id,
        index.index_id,
        index.project_id,
        version_number=number,
        state="pending",
        committed_at=sql_now() - timedelta(minutes=minutes_ago),
    )
    put(engines.admin, (document_versions, version))
    version_id: UUID = version["id"]
    return version_id


def running_item(
    engines: Engines,
    index: Index,
    document_id: UUID,
    version_id: UUID,
    *,
    physical_id: UUID | None = None,
    run_id: UUID | None = None,
) -> ItemEnvelope:
    if run_id is None:
        run = ingestion_run_row(index.index_id, state="running")
        put(engines.admin, (ingestion_runs, run))
        run_id = run["id"]
    item = ingestion_run_item_row(
        run_id,
        index.index_id,
        document_id=document_id,
        document_version_id=version_id,
        physical_index_id=physical_id or index.active_id,
        state="running",
        claim_count=1,
        lease_expires_at=sql_now() + timedelta(minutes=30),
        started_at=sql_now(),
    )
    put(engines.admin, (ingestion_run_items, item))
    return ItemEnvelope(item_id=item["id"], claim_number=1, run_id=run_id, settings=SETTINGS)


def finish(
    engines: Engines, envelope: ItemEnvelope, state: str = "succeeded", code: str | None = None
) -> None:
    """The item's outcome, recorded as its sub-orchestration ends it; only then may another item
    of the document run in that physical index (`UX_run_items_running_target`,
    `80-data-model.md` section 2.17; `14-ingestion.md` section 5.6 item 3)."""
    outcome = ItemOutcome(
        item_id=envelope.item_id,
        claim_number=envelope.claim_number,
        state=state,  # type: ignore[arg-type]
        code=code,
    )
    assert unit(engines.ingestion, lambda c: record_item_outcome.record(c, outcome)) is not None


def validate(engines: Engines, envelope: ItemEnvelope) -> validate_item.Decision:
    decision: validate_item.Decision = unit(
        engines.ingestion,
        lambda connection: validate_item.decide(connection, envelope, converter_version=CONVERTER),
    )
    return decision


def built(engines: Engines, envelope: ItemEnvelope, plan: BuildPlan, chunks: int = 3) -> PlanInput:
    """Extract's, Chunk's and Index's ledger writes for a build of `chunks` chunks."""
    sha = "c" * 64
    unit(engines.ingestion, lambda c: extract_document.record_pdf_sha(c, plan.chunk_set_id, sha))
    unit(
        engines.ingestion,
        lambda c: extract_document.record_page_count(c, plan.version_id, 2, converted=False),
    )
    unit(
        engines.ingestion, lambda c: chunk_document.record_chunk_count(c, plan.chunk_set_id, chunks)
    )
    assert unit(engines.ingestion, lambda c: index_chunks.mark_ready(c, plan.chunk_set_id))
    return PlanInput(**envelope.model_dump(), plan=plan)


def flip(engines: Engines, item: PlanInput) -> flip_chunk_set.Flip:
    flipped: flip_chunk_set.Flip = unit(
        engines.ingestion, lambda connection: flip_chunk_set.decide(connection, item)
    )
    return flipped


def test_t173_only_the_newest_pending_version_is_built(engines: Engines) -> None:
    index = ready_index(engines)
    document_id = new_document(engines, index)
    older = pending_version(engines, index, document_id, 1, minutes_ago=5)
    newer = pending_version(engines, index, document_id, 2)

    older_item = running_item(engines, index, document_id, older)
    skipped = validate(engines, older_item)
    finish(engines, older_item, "skipped", "superseded_by_newer")
    decision = validate(engines, running_item(engines, index, document_id, newer))

    assert skipped.skip == "superseded_by_newer"
    assert (
        one(
            engines.admin, select(document_versions.c.state).where(document_versions.c.id == older)
        ).state
        == "superseded"
    )
    assert decision.plan is not None
    assert (
        one(
            engines.admin, select(document_versions.c.state).where(document_versions.c.id == newer)
        ).state
        == "ingesting"
    )


def test_t166_t171_a_retry_reuses_the_build_and_a_second_flip_changes_nothing(
    engines: Engines,
) -> None:
    index = ready_index(engines)
    document_id = new_document(engines, index)
    version = pending_version(engines, index, document_id, 1)
    envelope = running_item(engines, index, document_id, version)

    first = validate(engines, envelope).plan
    second = validate(engines, envelope).plan
    assert first is not None
    assert second is not None
    assert first.chunk_set_id == second.chunk_set_id
    item = built(engines, envelope, first)
    flipped = flip(engines, item)
    again = flip(engines, item)

    assert flipped.outcome == "flipped"
    assert (again.outcome, again.retired) == ("flipped", ())
    served = one(
        engines.admin,
        select(served_chunk_sets.c.chunk_set_id).where(
            served_chunk_sets.c.document_id == document_id
        ),
    )
    assert served.chunk_set_id == first.chunk_set_id
    current = one(
        engines.admin, select(documents.c.current_version_id).where(documents.c.id == document_id)
    )
    assert current.current_version_id == version
    finish(engines, envelope)
    assert (
        validate(engines, running_item(engines, index, document_id, version)).skip
        == "already_served"
    )


def test_t169_a_delete_during_embed_is_never_served(engines: Engines) -> None:
    index = ready_index(engines)
    document_id = new_document(engines, index)
    version = pending_version(engines, index, document_id, 1)
    envelope = running_item(engines, index, document_id, version)
    plan = validate(engines, envelope).plan
    assert plan is not None
    item = built(engines, envelope, plan)
    with engines.admin.begin() as connection:
        connection.execute(
            update(documents)
            .where(documents.c.id == document_id)
            .values(is_deleted=True, deleted_at=sql_now())
        )

    flipped = flip(engines, item)

    assert (flipped.outcome, flipped.code) == ("skipped", "document_deleted")
    with engines.admin.connect() as connection:
        assert (
            connection.execute(
                select(served_chunk_sets).where(served_chunk_sets.c.document_id == document_id)
            ).first()
            is None
        )
    state = one(
        engines.admin, select(chunk_sets.c.state).where(chunk_sets.c.id == plan.chunk_set_id)
    )
    assert state.state == "retired"


def test_t168_the_newest_of_two_concurrent_versions_is_served(engines: Engines) -> None:
    index = ready_index(engines)
    document_id = new_document(engines, index)
    older = pending_version(engines, index, document_id, 1, minutes_ago=1)
    older_item = running_item(engines, index, document_id, older)
    older_plan = validate(engines, older_item).plan
    assert older_plan is not None
    older_built = built(engines, older_item, older_plan)
    # The second upload commits while the first builds; its item waits for the document's
    # running one (`14-ingestion.md` section 5.6 item 3).
    newer = pending_version(engines, index, document_id, 2)

    late = flip(engines, older_built)
    finish(engines, older_item, "skipped", "superseded_by_newer")
    newer_item = running_item(engines, index, document_id, newer)
    newer_plan = validate(engines, newer_item).plan
    assert newer_plan is not None
    assert flip(engines, built(engines, newer_item, newer_plan)).outcome == "flipped"

    assert (late.outcome, late.code) == ("skipped", "superseded_by_newer")
    assert [row.chunk_set_id for row in served_rows(engines, document_id)] == [
        newer_plan.chunk_set_id
    ]
    current = one(
        engines.admin, select(documents.c.current_version_id).where(documents.c.id == document_id)
    )
    assert current.current_version_id == newer


def test_t170_an_update_during_a_rebuild_queues_the_building_index(engines: Engines) -> None:
    index = ready_index(engines)
    rebuild = ingestion_run_row(index.index_id, kind="index_rebuild", priority=1, state="running")
    building = physical_index_row(
        index.index_id,
        generation=2,
        state="building",
        provisioned_at=sql_now(),
        build_run_id=rebuild["id"],
    )
    put(engines.admin, (ingestion_runs, rebuild), (physical_indexes, building))
    document_id = new_document(engines, index)
    first = pending_version(engines, index, document_id, 1, minutes_ago=5)
    first_item = running_item(engines, index, document_id, first)
    first_plan = validate(engines, first_item).plan
    assert first_plan is not None
    first_flip = flip(engines, built(engines, first_item, first_plan))
    assert (first_flip.outcome, first_flip.building_item_queued) == ("flipped", True)
    finish(engines, first_item)

    # The Flip queued the version for the building index in the rebuild run; the claim takes it.
    rebuild_item = claim_one(engines)
    claimed = one(
        engines.admin,
        select(ingestion_run_items.c.run_id, ingestion_run_items.c.physical_index_id).where(
            ingestion_run_items.c.id == rebuild_item.item_id
        ),
    )
    assert (claimed.run_id, claimed.physical_index_id) == (rebuild["id"], building["id"])
    rebuild_plan = validate(engines, rebuild_item).plan
    assert rebuild_plan is not None
    assert flip(engines, built(engines, rebuild_item, rebuild_plan)).outcome == "flipped"
    finish(engines, rebuild_item)
    second = pending_version(engines, index, document_id, 2)
    second_item = running_item(engines, index, document_id, second)
    second_plan = validate(engines, second_item).plan
    assert second_plan is not None
    flipped = flip(engines, built(engines, second_item, second_plan))

    assert flipped.building_item_queued
    assert flipped.superseded == first
    queued = one(
        engines.admin,
        select(ingestion_run_items.c.run_id, ingestion_run_items.c.state).where(
            ingestion_run_items.c.document_version_id == second,
            ingestion_run_items.c.physical_index_id == building["id"],
        ),
    )
    assert (queued.run_id, queued.state) == (rebuild["id"], "queued")


def test_a_failed_item_fails_its_version_only_in_the_live_index(engines: Engines) -> None:
    index = ready_index(engines)
    document_id = new_document(engines, index)
    version = pending_version(engines, index, document_id, 1)
    envelope = running_item(engines, index, document_id, version)
    plan = validate(engines, envelope).plan
    assert plan is not None
    outcome = ItemOutcome(
        item_id=envelope.item_id,
        claim_number=1,
        state="failed",
        code="embedding_failed",
        stage="embed",  # type: ignore[arg-type]
    )

    recorded = unit(engines.ingestion, lambda c: record_item_outcome.record(c, outcome))
    lost = unit(engines.ingestion, lambda c: record_item_outcome.record(c, outcome))

    assert recorded is not None
    assert lost is None
    row = one(engines.admin, select(document_versions).where(document_versions.c.id == version))
    assert (row.state, row.failure_code) == ("failed", "embedding_failed")
    item = one(
        engines.admin,
        select(ingestion_run_items).where(ingestion_run_items.c.id == envelope.item_id),
    )
    assert (item.state, item.outcome_code, item.stage) == ("failed", "embedding_failed", "embed")
    assert (
        one(
            engines.admin, select(chunk_sets.c.state).where(chunk_sets.c.id == plan.chunk_set_id)
        ).state
        == "retired"
    )


def test_t615_render_records_the_converted_pdf(engines: Engines) -> None:
    index = ready_index(engines)
    document_id = new_document(engines, index)
    version = document_version_row(
        document_id,
        index.index_id,
        index.project_id,
        state="ingesting",
        committed_at=sql_now(),
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        source_name="notes.docx",
        converted_sha256=b"\x01" * 32,
        converter_version="lo-older",
    )
    put(engines.admin, (document_versions, version))
    pdf = "d" * 64
    target = render_pages.RenditionTarget(
        index_id=index.index_id,
        document_id=document_id,
        version_number=1,
        version_id=version["id"],
        content_type=version["content_type"],
        pdf_sha256=pdf,
        page_count=2,
    )
    marker = RenditionMarker(
        page_count=2, pdf_sha256=pdf, rendition_rules_version=1, document_pdf="built"
    )

    unit(engines.ingestion, lambda c: render_pages.clear_stale_converted(c, version["id"], pdf))
    cleared = one(
        engines.admin,
        select(document_versions.c.converted_sha256).where(document_versions.c.id == version["id"]),
    )
    unit(engines.ingestion, lambda c: render_pages.record_completion(c, target, marker, CONVERTER))

    assert cleared.converted_sha256 is None
    row = one(
        engines.admin, select(document_versions).where(document_versions.c.id == version["id"])
    )
    assert bytes(row.converted_sha256).hex() == pdf
    assert (row.converter_version, row.document_pdf_status) == (CONVERTER, "built")
    assert json.loads(marker.encode())["renditionRulesVersion"] == 1


# --- T-164: the Flip against a delete, a newer commit and a repoint -------------------------------


def served_rows(engines: Engines, document_id: UUID) -> list[Any]:
    with engines.admin.connect() as connection:
        return list(
            connection.execute(
                select(served_chunk_sets).where(served_chunk_sets.c.document_id == document_id)
            ).all()
        )


def delete_document(engines: Engines, index: Index, document_id: UUID) -> None:
    """`adminDeleteDocument`'s transaction, as the API role runs it, on the row version read
    just before."""
    row_version = one(
        engines.admin, select(documents.c.row_version).where(documents.c.id == document_id)
    ).row_version
    unit(
        engines.api,
        lambda connection: create_document_delete_run(
            connection,
            index_id=index.index_id,
            document_id=document_id,
            row_version=row_version,
            requested_by=uuid4(),
            trace=None,
        ),
    )


def chunk_set_state(engines: Engines, chunk_set_id: UUID) -> str:
    state: str = one(
        engines.admin, select(chunk_sets.c.state).where(chunk_sets.c.id == chunk_set_id)
    ).state
    return state


def built_item(engines: Engines, index: Index, number: int = 1) -> tuple[UUID, UUID, PlanInput]:
    document_id = new_document(engines, index)
    version = pending_version(engines, index, document_id, number)
    envelope = running_item(engines, index, document_id, version)
    plan = validate(engines, envelope).plan
    assert plan is not None
    return document_id, version, built(engines, envelope, plan)


def test_t164_a_delete_committed_before_the_flip_leaves_no_served_row(engines: Engines) -> None:
    index = ready_index(engines)
    document_id, _, item = built_item(engines, index)

    delete_document(engines, index, document_id)
    flipped = flip(engines, item)

    assert (flipped.outcome, flipped.code) == ("skipped", "document_deleted")
    assert served_rows(engines, document_id) == []
    assert one(engines.admin, select(documents).where(documents.c.id == document_id)).is_deleted
    assert chunk_set_state(engines, item.plan.chunk_set_id) == "retired"


def test_t164_a_flip_committed_before_the_delete_leaves_no_served_row(engines: Engines) -> None:
    index = ready_index(engines)
    document_id, _, item = built_item(engines, index)

    assert flip(engines, item).outcome == "flipped"
    assert len(served_rows(engines, document_id)) == 1
    delete_document(engines, index, document_id)

    assert served_rows(engines, document_id) == []
    row = one(engines.admin, select(documents).where(documents.c.id == document_id))
    assert row.is_deleted
    assert row.current_version_id is None
    assert chunk_set_state(engines, item.plan.chunk_set_id) == "retired"


def test_t164_a_flip_of_an_older_version_after_a_newer_commit_supersedes_it(
    engines: Engines,
) -> None:
    index = ready_index(engines)
    document_id = new_document(engines, index)
    older = pending_version(engines, index, document_id, 1, minutes_ago=5)
    envelope = running_item(engines, index, document_id, older)
    plan = validate(engines, envelope).plan
    assert plan is not None
    item = built(engines, envelope, plan)
    pending_version(engines, index, document_id, 2)

    flipped = flip(engines, item)

    assert (flipped.outcome, flipped.code) == ("skipped", "superseded_by_newer")
    assert (
        one(
            engines.admin, select(document_versions.c.state).where(document_versions.c.id == older)
        ).state
        == "superseded"
    )
    assert chunk_set_state(engines, plan.chunk_set_id) == "retired"
    assert served_rows(engines, document_id) == []


def test_t164_a_flip_whose_target_was_repointed_requeues_the_item(engines: Engines) -> None:
    index = ready_index(engines)
    document_id, _, item = built_item(engines, index)
    successor = physical_index_row(
        index.index_id, generation=2, state="active", provisioned_at=sql_now()
    )
    # The repoint's end state: the old index retiring, then its successor active
    # (`UX_physical_indexes_one_active` allows one active index at a time).
    with engines.admin.begin() as connection:
        connection.execute(
            update(physical_indexes)
            .where(physical_indexes.c.id == index.active_id)
            .values(state="retiring", delete_after=sql_now() + timedelta(minutes=10))
        )
        connection.execute(insert(physical_indexes), successor)
        connection.execute(
            update(search_indexes)
            .where(search_indexes.c.id == index.index_id)
            .values(active_physical_index_id=successor["id"])
        )

    flipped = flip(engines, item)

    assert flipped.outcome == "requeued"
    requeued = one(
        engines.admin,
        select(ingestion_run_items).where(ingestion_run_items.c.id == item.item_id),
    )
    assert (requeued.state, requeued.physical_index_id) == ("queued", successor["id"])
    assert requeued.claim_count == 1
    assert chunk_set_state(engines, item.plan.chunk_set_id) == "retired"
    assert served_rows(engines, document_id) == []


# --- T-172: a dispatcher crash while an item runs ------------------------------------------------


def claim_one(engines: Engines) -> ItemEnvelope:
    claimed, _ = unit(engines.ingestion, lambda c: claim_ingestion_items.claim(c, 1))
    (item,) = claimed
    return ItemEnvelope(
        item_id=item.item_id, claim_number=item.claim_number, run_id=item.run_id, settings=SETTINGS
    )


def test_t172_a_crash_with_a_running_item_requeues_it_once_and_serves_one_chunk_set(
    engines: Engines,
) -> None:
    index = ready_index(engines)
    document_id = new_document(engines, index)
    version = pending_version(engines, index, document_id, 1)
    run = ingestion_run_row(index.index_id)
    queued = ingestion_run_item_row(
        run["id"],
        index.index_id,
        document_id=document_id,
        document_version_id=version,
        physical_index_id=index.active_id,
    )
    put(engines.admin, (ingestion_runs, run), (ingestion_run_items, queued))

    # The first claim validates and starts its build; then the dispatcher and its
    # sub-orchestration are gone, and nothing records an outcome.
    first = claim_one(engines)
    first_plan = validate(engines, first).plan
    assert first_plan is not None
    with engines.admin.begin() as connection:
        connection.execute(
            update(ingestion_run_items)
            .where(ingestion_run_items.c.id == first.item_id)
            .values(lease_expires_at=sql_now() - timedelta(seconds=1))
        )
    leases = unit(engines.ingestion, lambda c: sweep_duties.expire_leases(c, 3))
    again = unit(engines.ingestion, lambda c: sweep_duties.expire_leases(c, 3))

    assert leases.requeued == (first.item_id,)
    assert again.requeued == ()
    second = claim_one(engines)
    assert second.claim_number == 2
    plan = validate(engines, second).plan
    assert plan is not None
    item = built(engines, second, plan)
    assert flip(engines, item).outcome == "flipped"
    outcome = ItemOutcome(item_id=second.item_id, claim_number=2, state="succeeded")
    assert unit(engines.ingestion, lambda c: record_item_outcome.record(c, outcome)) is not None
    late = ItemOutcome(
        item_id=first.item_id,
        claim_number=1,
        state="failed",
        code="embedding_failed",
        stage="embed",  # type: ignore[arg-type]
    )
    assert unit(engines.ingestion, lambda c: record_item_outcome.record(c, late)) is None

    (served,) = served_rows(engines, document_id)
    assert served.chunk_set_id == plan.chunk_set_id
    final = one(
        engines.admin,
        select(ingestion_run_items).where(ingestion_run_items.c.id == first.item_id),
    )
    assert (final.state, final.claim_count) == ("succeeded", 2)
    assert (
        one(
            engines.admin,
            select(document_versions.c.state).where(document_versions.c.id == version),
        ).state
        == "ingested"
    )


# --- T-110: each state moves once along its transitions -------------------------------------------


def state_of(engines: Engines, table: Table, row_id: UUID) -> str:
    state: str = one(engines.admin, select(table.c.state).where(table.c.id == row_id)).state
    return state


def test_t110_run_items_move_once_along_their_transitions(engines: Engines) -> None:
    index = ready_index(engines)
    document_id = new_document(engines, index)
    version = pending_version(engines, index, document_id, 1)
    run = ingestion_run_row(index.index_id)
    queued = ingestion_run_item_row(
        run["id"],
        index.index_id,
        document_id=document_id,
        document_version_id=version,
        physical_index_id=index.active_id,
    )
    put(engines.admin, (ingestion_runs, run), (ingestion_run_items, queued))

    claimed = claim_one(engines)
    assert unit(engines.ingestion, lambda c: claim_ingestion_items.claim(c, 1))[0] == []
    assert state_of(engines, ingestion_run_items, claimed.item_id) == "running"
    done = ItemOutcome(item_id=claimed.item_id, claim_number=1, state="succeeded")
    assert unit(engines.ingestion, lambda c: record_item_outcome.record(c, done)) is not None
    assert unit(engines.ingestion, lambda c: record_item_outcome.record(c, done)) is None
    assert unit(engines.ingestion, lambda c: sweep_duties.expire_leases(c, 3)).requeued == ()

    row = one(
        engines.admin,
        select(ingestion_run_items).where(ingestion_run_items.c.id == claimed.item_id),
    )
    assert (row.state, row.claim_count) == ("succeeded", 1)
    assert row.completed_at is not None


def test_t110_a_run_is_finalised_once(engines: Engines) -> None:
    index = ready_index(engines)
    run = ingestion_run_row(index.index_id, state="running")
    put(engines.admin, (ingestion_runs, run))

    (finalisable,) = [
        candidate
        for candidate in unit(engines.ingestion, finalisation.finalisable_runs)
        if candidate.id == run["id"]
    ]
    first = unit(engines.ingestion, lambda c: finalisation.finalise(c, finalisable))
    ended = one(engines.admin, select(ingestion_runs).where(ingestion_runs.c.id == run["id"]))
    second = unit(engines.ingestion, lambda c: finalisation.finalise(c, finalisable))

    assert first is not None
    assert first.state.value == "completed"
    assert second is None
    after = one(engines.admin, select(ingestion_runs).where(ingestion_runs.c.id == run["id"]))
    assert (after.state, after.completed_at) == (ended.state, ended.completed_at)


def test_t110_a_chunk_set_and_its_version_move_once_along_their_transitions(
    engines: Engines,
) -> None:
    index = ready_index(engines)
    document_id = new_document(engines, index)
    version = pending_version(engines, index, document_id, 1)
    envelope = running_item(engines, index, document_id, version)
    plan = validate(engines, envelope).plan
    assert plan is not None
    assert state_of(engines, document_versions, version) == "ingesting"
    assert state_of(engines, chunk_sets, plan.chunk_set_id) == "building"

    item = built(engines, envelope, plan)
    assert state_of(engines, chunk_sets, plan.chunk_set_id) == "ready"
    assert flip(engines, item).outcome == "flipped"
    assert state_of(engines, chunk_sets, plan.chunk_set_id) == "served"
    assert state_of(engines, document_versions, version) == "ingested"
    # `ready` is the only way into `served`: a served set doesn't go back to `ready`.
    assert unit(engines.ingestion, lambda c: index_chunks.mark_ready(c, plan.chunk_set_id)) is False
    assert state_of(engines, chunk_sets, plan.chunk_set_id) == "served"

    finish(engines, envelope)
    newer = pending_version(engines, index, document_id, 2)
    newer_item = running_item(engines, index, document_id, newer)
    newer_plan = validate(engines, newer_item).plan
    assert newer_plan is not None
    assert flip(engines, built(engines, newer_item, newer_plan)).outcome == "flipped"
    finish(engines, newer_item)

    assert state_of(engines, chunk_sets, plan.chunk_set_id) == "retired"
    assert state_of(engines, document_versions, version) == "superseded"
    # Terminal: a retired set is never made ready again, and a superseded version isn't failed,
    # not even by a later item of that version that fails.
    assert unit(engines.ingestion, lambda c: index_chunks.mark_ready(c, plan.chunk_set_id)) is False
    stale = running_item(engines, index, document_id, version)
    failed = ItemOutcome(
        item_id=stale.item_id,
        claim_number=1,
        state="failed",
        code="embedding_failed",
        stage="embed",  # type: ignore[arg-type]
    )
    assert unit(engines.ingestion, lambda c: record_item_outcome.record(c, failed)) is not None
    assert state_of(engines, document_versions, version) == "superseded"
    assert state_of(engines, chunk_sets, plan.chunk_set_id) == "retired"
