"""`purge_index_blobs` (`14-ingestion.md` sections 8.12 item 3 and 7.2; ADR 0052).

The last item of a logical index's deletion. The claim starts it only while no item of its logical
index runs, so every upload a Render or `rerender` item makes has ended before it lists. It lists
`documents/{indexId}/` and `renditions/{indexId}/` and deletes in calls of at most 256 blobs (an
absent blob counts as deleted), returning `done: false` once `activityTimeBudgetSeconds` of work is
spent, so the orchestrator calls it once more; when both prefixes are empty it records every
version of the index purged and returns `done: true`. Deleted blobs stay soft-deleted for 14 days.
"""

from __future__ import annotations

from typing import Any, Final
from uuid import UUID

import azure.durable_functions as df
from azure.core.exceptions import AzureError
from opentelemetry.trace import Span
from sqlalchemy import Connection, select, update

from docchat_core.ingestion.models import ActivityResult, ItemEnvelope, ItemStage
from docchat_core.ingestion.names import index_prefix
from docchat_core.sql.tables.ingestion import document_versions, ingestion_run_items
from docchat_core.sql.tables.schema import SYSUTCDATETIME
from docchat_ingestion.activities.runtime import (
    ContainerPort,
    IngestionProcess,
    ProcessProvider,
    runtime,
)
from docchat_ingestion.activities.support import fence_lease, run_item_activity

NAME: Final = "purge_index_blobs"
DEADLINE_SECONDS: Final = 1560
DELETE_BATCH: Final = 256
DELETED_STATUSES: Final = frozenset({200, 202, 404})
"""A batch sub-response that counts as deleted, an absent blob included."""


class PurgeIndexBlobsResult(ActivityResult):
    """`{done, deletedBlobs}` with the `ActivityResult` fields."""

    done: bool = False
    deleted_blobs: int = 0


class BlobDeleteRefusedError(Exception):
    """A blob of a batch delete was refused with a status other than an absent blob's."""

    def __init__(self) -> None:
        super().__init__("blob delete refused")


def claim_index(connection: Connection, envelope: ItemEnvelope) -> UUID | None:
    """The fenced lease update, then the item's logical index; `None` for a lost claim."""
    if not fence_lease(
        connection, envelope.item_id, envelope.claim_number, ItemStage.PURGE_BLOBS, DEADLINE_SECONDS
    ):
        return None
    index_id: UUID = connection.execute(
        select(ingestion_run_items.c.search_index_id).where(
            ingestion_run_items.c.id == envelope.item_id
        )
    ).scalar_one()
    return index_id


def mark_purged(connection: Connection, index_id: UUID) -> None:
    connection.execute(
        update(document_versions)
        .where(
            document_versions.c.search_index_id == index_id,
            document_versions.c.storage_purged_at.is_(None),
        )
        .values(storage_purged_at=SYSUTCDATETIME)
    )


async def _next_batch(container: ContainerPort, prefix: str) -> list[str]:
    names: list[str] = []
    async for blob in container.list_blobs(name_starts_with=prefix):
        names.append(blob.name)
        if len(names) >= DELETE_BATCH:
            break
    return names


async def _delete(container: ContainerPort, names: list[str]) -> None:
    responses = await container.delete_blobs(*names, raise_on_any_failure=False)
    async for response in responses:
        status = getattr(response, "status_code", None)
        if status not in DELETED_STATUSES:
            raise BlobDeleteRefusedError


async def purge(process: IngestionProcess, envelope: ItemEnvelope) -> ActivityResult:
    index_id = await process.sql.run(
        lambda connection: claim_index(connection, envelope),
        name="purge_blobs_claim",
        retry="transient",
    )
    if index_id is None:
        return PurgeIndexBlobsResult.failure("lease_lost")
    prefix = index_prefix(index_id)
    budget_ends = process.clock.monotonic() + envelope.settings.activity_time_budget_seconds
    deleted = 0
    try:
        for container in (process.documents, process.renditions):
            while True:
                names = await _next_batch(container, prefix)
                if not names:
                    break
                await _delete(container, names)
                deleted += len(names)
                if process.clock.monotonic() >= budget_ends:
                    return PurgeIndexBlobsResult.success(done=False, deleted_blobs=deleted)
    except (AzureError, BlobDeleteRefusedError):
        return PurgeIndexBlobsResult.failure("storage_unavailable")
    await process.sql.run(
        lambda connection: mark_purged(connection, index_id),
        name="purge_blobs_mark",
        retry="transient",
    )
    return PurgeIndexBlobsResult.success(done=True, deleted_blobs=deleted)


async def run(payload: object, provider: ProcessProvider = runtime) -> dict[str, Any]:
    async def body(envelope: ItemEnvelope, span: Span) -> ActivityResult:
        del span  # The span carries no identifier of a logical index.
        return await purge(await provider(), envelope)

    return await run_item_activity(NAME, DEADLINE_SECONDS, payload, body)


async def purge_index_blobs(payload: object) -> dict[str, Any]:
    return await run(payload)


bp: Final = df.Blueprint()
bp.activity_trigger(input_name="payload")(purge_index_blobs)
