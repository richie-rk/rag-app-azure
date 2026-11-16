"""`reindex start` (`86-operations.md` section 2.8.6; `14-ingestion.md` sections 8.5 to 8.7 and
19.1; runbook R05).

The automation path of a re-index: the checks and the transaction of `adminStartReingest`, through
the same function the API calls, `docchat_core.ingestion.runs.create_reingest_run`, run by a config
operator under `docchat_config_operator`. The operator types no `If-Match`, so the command reads the
logical index's row version with its status, predicts the mode and refuses what the transaction
would refuse before the confirmation; the transaction is conditioned on that row version, so a
change made meanwhile exits 6. Operators hold no queue-send right: the run carries no trace context
and starts at the dispatcher's next wake.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from uuid import UUID

from sqlalchemy import func, select

from docchat_core.audit import write_audit_event
from docchat_core.audit.models import IngestionReindexStartedMetadata
from docchat_core.ingestion.models import (
    SETTINGS_CHANGE_STATUSES,
    IndexStatus,
    IngestionRefusedError,
    PhysicalIndexState,
    ReingestMode,
)
from docchat_core.ingestion.runs import (
    PhysicalIndexSettings,
    create_reingest_run,
    has_capacity,
    serves_settings,
)
from docchat_core.sql.tables.ingestion import documents, physical_indexes, search_indexes
from docchat_core.sql.tables.schema import BIT_FALSE, nvarchar_literal
from docchat_ops.commands.ingestion import (
    CHANGED,
    NEXT_WAKE,
    RERUN,
    apply_change,
    embedding_catalog,
    identifier,
    max_indexes,
    operator_session,
)
from docchat_ops.framework.audit import operator_event
from docchat_ops.framework.exitcodes import Conflict, NotFound, OpsError, UsageError

if TYPE_CHECKING:
    import argparse

    from sqlalchemy import Connection

    from docchat_core.ingestion.models import RefusalCode
    from docchat_core.ingestion.runs import ReingestResult
    from docchat_core.ingestion.settings import EmbeddingModelCatalog
    from docchat_ops.cli import Run
    from docchat_ops.framework.audit import OpsDatabase

READ_UNIT: Final = "reindex_read"
START_UNIT: Final = "reindex_start"

NO_ROOM_STEP: Final = (
    "delete unused indexes (adminDeleteIndex) or raise search.sku through R01 (R05 step 1)"
)


def _refusal(code: RefusalCode) -> OpsError:
    """The exit of a refusal of `create_reingest_run` (`14-ingestion.md` section 19.1)."""
    if code == "index_not_found":
        return NotFound("no live logical index with this ID", "check the index ID", code=code)
    if code == "precondition_failed":
        return Conflict(CHANGED, RERUN)
    if code == "search_index_capacity":
        return UsageError(
            "the search service has no room for another physical index", NO_ROOM_STEP, code=code
        )
    if code == "unsupported_embedding_model":
        return UsageError(
            "DOCCHAT_OPENAI_EMBEDDING_MODELS has no entry with the index's embedding model and "
            "dimensions",
            "run azd env refresh, or change the index's embedding model in the admin screen",
            code=code,
        )
    # `index_busy`, the one other code the function raises.
    return UsageError(
        "the index's status doesn't allow a re-ingest; nothing was written",
        "wait until the index is ready, reingest_required or failed",
        code=code,
    )


def start_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--index-id", type=identifier, required=True, help="the logical index's ID")


@dataclass(frozen=True)
class _Plan:
    """What the confirmation shows and the transaction is conditioned on."""

    status: IndexStatus
    row_version: bytes
    embedding_model: str
    embedding_dimensions: int
    mode: ReingestMode
    live_documents: int
    physical_indexes: int


def _plan(connection: Connection, index_id: UUID) -> _Plan | None:
    indexes = search_indexes.c
    row = connection.execute(
        select(
            indexes.status,
            indexes.row_version,
            indexes.embedding_model,
            indexes.embedding_dimensions,
            indexes.language_analyzer,
            indexes.active_physical_index_id,
        ).where(indexes.id == index_id)
    ).first()
    if row is None or row.status == IndexStatus.DELETED:
        return None
    active: PhysicalIndexSettings | None = None
    if row.active_physical_index_id is not None:
        pi = physical_indexes.c
        found = connection.execute(
            select(
                pi.embedding_model, pi.embedding_dimensions, pi.language_analyzer, pi.schema_version
            ).where(pi.id == row.active_physical_index_id)
        ).first()
        if found is not None:
            active = PhysicalIndexSettings(
                embedding_model=found.embedding_model,
                embedding_dimensions=int(found.embedding_dimensions),
                language_analyzer=found.language_analyzer,
                schema_version=int(found.schema_version),
            )
    same_index = serves_settings(
        active,
        embedding_model=row.embedding_model,
        embedding_dimensions=int(row.embedding_dimensions),
        language_analyzer=row.language_analyzer,
    )
    live = connection.execute(
        select(func.count())
        .select_from(documents)
        .where(
            documents.c.search_index_id == index_id,
            documents.c.is_deleted == BIT_FALSE,
            documents.c.current_version_id.is_not(None),
        )
    ).scalar_one()
    physical = connection.execute(
        select(func.count())
        .select_from(physical_indexes)
        .where(physical_indexes.c.state != nvarchar_literal(PhysicalIndexState.DELETED))
    ).scalar_one()
    return _Plan(
        status=IndexStatus(row.status),
        row_version=bytes(row.row_version),
        embedding_model=str(row.embedding_model),
        embedding_dimensions=int(row.embedding_dimensions),
        mode=ReingestMode.SAME_INDEX if same_index else ReingestMode.NEW_PHYSICAL_INDEX,
        live_documents=int(live),
        physical_indexes=int(physical),
    )


async def _read_plan(database: OpsDatabase, index_id: UUID) -> _Plan:
    def read(connection: Connection) -> _Plan | None:
        return _plan(connection, index_id)

    plan = await database.run(read, name=READ_UNIT, retry="transient")
    if plan is None:
        raise _refusal("index_not_found")
    return plan


def _check(plan: _Plan, limit: int, catalog: EmbeddingModelCatalog) -> None:
    """Refuse before the confirmation what the transaction would refuse (exit 2)."""
    if plan.status not in SETTINGS_CHANGE_STATUSES:
        raise _refusal("index_busy")
    if plan.mode is ReingestMode.SAME_INDEX:
        return
    if not has_capacity(plan.physical_indexes, limit, keep_free=False):
        raise _refusal("search_index_capacity")
    entry = catalog.find(plan.embedding_model)
    if entry is None or plan.embedding_dimensions not in entry.dimensions:
        raise _refusal("unsupported_embedding_model")


def _summary(index_id: UUID, plan: _Plan, limit: int) -> list[str]:
    lines = [
        f"index: {index_id} (status {plan.status})",
        f"mode: {plan.mode}",
        f"live documents to re-ingest: {plan.live_documents}",
    ]
    if plan.mode is ReingestMode.NEW_PHYSICAL_INDEX:
        lines.append(
            f"physical indexes on the search service: {plan.physical_indexes} of {limit}, "
            "and one more for the rebuild"
        )
    return lines


async def start(run: Run, args: argparse.Namespace) -> None:
    index_id: UUID = args.index_id
    environment = run.azd()
    limit = max_indexes(environment)
    catalog = embedding_catalog(environment)
    async with operator_session(run, environment) as session:
        plan = await _read_plan(session.database, index_id)
        _check(plan, limit, catalog)
        await run.confirm(_summary(index_id, plan, limit))
        actor = UUID(session.actor)
        ticket = run.ticket or ""

        def work(connection: Connection) -> ReingestResult:
            result = create_reingest_run(
                connection,
                index_id=index_id,
                row_version=plan.row_version,
                requested_by=actor,
                # A run without a request stores no trace context (section 4.9 item 5).
                trace=None,
                max_indexes=limit,
                catalog=catalog,
            )
            write_audit_event(
                connection,
                operator_event(
                    action="ingestion.reindex_started",
                    actor_object_id=session.actor,
                    target_type="search_index",
                    target_id=str(index_id),
                    metadata=IngestionReindexStartedMetadata(
                        run_id=result.run_id,
                        mode=result.mode.value,
                        item_count=result.item_count,
                        ticket=ticket,
                    ),
                ),
            )
            return result

        try:
            result = await apply_change(session.database, work, START_UNIT)
        except IngestionRefusedError as refusal:
            raise _refusal(refusal.code) from None
    run.progress(NEXT_WAKE)
    run.report(
        "started",
        {
            "indexId": str(index_id),
            "runId": str(result.run_id),
            "mode": result.mode.value,
            "itemCount": result.item_count,
            "physicalIndexId": str(result.physical_index_id),
            "status": result.status.value,
        },
    )
