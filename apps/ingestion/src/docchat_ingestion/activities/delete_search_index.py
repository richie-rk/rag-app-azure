"""`delete_search_index` (`14-ingestion.md` sections 8.12 item 2, 8.10 and 7.2; ADRs 0045 and
0052).

Deletes the Azure index (an absent one counts as deleted), then, in one transaction, the physical
index's served rows (kept until now, so readers holding a cached index record still filter
correctly), its chunk sets marked retired with their chunks deleted, and the physical index
`deleted`. An `index_delete` run deletes `active` and `building` indexes too.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

import azure.durable_functions as df
from azure.core.exceptions import (
    HttpResponseError,
    ResourceNotFoundError,
    ServiceRequestError,
    ServiceResponseError,
)
from opentelemetry.trace import Span
from sqlalchemy import Connection, delete, func, select, update

from docchat_core.ingestion.models import ActivityResult, ItemEnvelope, ItemStage
from docchat_core.sql.tables.ingestion import (
    chunk_sets,
    ingestion_run_items,
    physical_indexes,
    served_chunk_sets,
)
from docchat_core.sql.tables.schema import SYSUTCDATETIME
from docchat_core.telemetry import emit_event, set_attributes
from docchat_ingestion.activities.runtime import IngestionProcess, ProcessProvider, runtime
from docchat_ingestion.activities.support import (
    fence_lease,
    literal,
    literals,
    retry_after_seconds,
    run_item_activity,
    service_unavailable,
)

NAME: Final = "delete_search_index"
DEADLINE_SECONDS: Final = 300


class DeleteSearchIndexResult(ActivityResult):
    """`{}` with the `ActivityResult` fields."""


@dataclass(frozen=True, slots=True)
class DoomedIndex:
    physical_index_id: UUID
    name: str


def claim_target(connection: Connection, envelope: ItemEnvelope) -> DoomedIndex | None:
    """The fenced lease update, then the item's physical index; `None` for a lost claim."""
    if not fence_lease(
        connection,
        envelope.item_id,
        envelope.claim_number,
        ItemStage.DELETE_INDEX,
        DEADLINE_SECONDS,
    ):
        return None
    row = connection.execute(
        select(physical_indexes.c.id, physical_indexes.c.name)
        .select_from(
            ingestion_run_items.join(
                physical_indexes, physical_indexes.c.id == ingestion_run_items.c.physical_index_id
            )
        )
        .where(ingestion_run_items.c.id == envelope.item_id)
    ).one()
    return DoomedIndex(row.id, row.name)


def mark_deleted(connection: Connection, physical_index_id: UUID) -> bool:
    connection.execute(
        delete(served_chunk_sets).where(served_chunk_sets.c.physical_index_id == physical_index_id)
    )
    connection.execute(
        update(chunk_sets)
        .where(chunk_sets.c.physical_index_id == physical_index_id)
        .values(
            state=literal("retired"),
            retired_at=func.coalesce(chunk_sets.c.retired_at, SYSUTCDATETIME),
            chunks_deleted_at=func.coalesce(chunk_sets.c.chunks_deleted_at, SYSUTCDATETIME),
        )
    )
    changed = connection.execute(
        update(physical_indexes)
        .where(
            physical_indexes.c.id == physical_index_id,
            physical_indexes.c.state.in_(literals(("retiring", "active", "building"))),
        )
        .values(state=literal("deleted"), deleted_at=SYSUTCDATETIME)
    ).rowcount
    return bool(changed == 1)


async def remove(process: IngestionProcess, envelope: ItemEnvelope, span: Span) -> ActivityResult:
    target = await process.sql.run(
        lambda connection: claim_target(connection, envelope),
        name="delete_index_claim",
        retry="transient",
    )
    if target is None:
        return DeleteSearchIndexResult.failure("lease_lost")
    set_attributes(span, **{"docchat.physical_index_id": str(target.physical_index_id)})
    try:
        await process.search_indexes.delete_index(target.name)
    except ResourceNotFoundError:
        pass
    except (ServiceRequestError, ServiceResponseError):
        return DeleteSearchIndexResult.failure("search_unavailable")
    except HttpResponseError as error:
        if service_unavailable(error):
            return DeleteSearchIndexResult.failure(
                "search_unavailable", retry_after_seconds=retry_after_seconds(error)
            )
        return DeleteSearchIndexResult.failure("index_delete_failed")
    deleted = await process.sql.run(
        lambda connection: mark_deleted(connection, target.physical_index_id),
        name="delete_index_rows",
        retry="transient",
    )
    if deleted:
        emit_event("physical_index_deleted", physicalIndexId=str(target.physical_index_id))
    return DeleteSearchIndexResult.success()


async def run(payload: object, provider: ProcessProvider = runtime) -> dict[str, Any]:
    async def body(envelope: ItemEnvelope, span: Span) -> ActivityResult:
        return await remove(await provider(), envelope, span)

    return await run_item_activity(NAME, DEADLINE_SECONDS, payload, body)


async def delete_search_index(payload: object) -> dict[str, Any]:
    return await run(payload)


bp: Final = df.Blueprint()
bp.activity_trigger(input_name="payload")(delete_search_index)
