"""`purge_document_artifacts`, the step of a `cleanup_document` item (`14-ingestion.md` sections
6.12, 7.1 and 5.3 duty 6).

Created by a document delete and by sweep duty 6, the item removes, for its document and reading
SQL directly: the chunks of every retired chunk set that still holds them in an `active` or
`building` physical index (deleted by key, then `chunks_deleted_at`), and the source, the
converted PDF and the renditions of every superseded or failed version not yet purged (then
`storage_purged_at`). Chunk sets in `retiring` or `deleted` physical indexes go with the index.
After `activityTimeBudgetSeconds` of work the step returns `done: false` and is called once more
(at most 20 calls); a failure is the retryable `cleanup_failed`.

The input may name the document; the item's own row is the authority, because `ItemInput`
carries no document ID (section 5.1), so the fenced statement reads it.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from opentelemetry.trace import Span
from pydantic import Field
from sqlalchemy import Connection, select

from docchat_core.ingestion.models import ActivityResult, ItemEnvelope, ItemStage
from docchat_core.models import LowerCaseUuid
from docchat_core.sql.tables.ingestion import (
    chunk_sets,
    document_versions,
    ingestion_run_items,
    physical_indexes,
)
from docchat_core.telemetry import set_attributes
from docchat_ingestion.activities.documents import (
    DEADLINES,
    ActivityOutput,
    DocumentServices,
    RetiredSet,
    ServicesProvider,
    VersionStorage,
    blueprint,
    document_services,
    fence,
    purge_version_storage,
    remove_retired_set,
    run_document_activity,
)
from docchat_ingestion.activities.support import literal, literals

NAME: Final = "purge_document_artifacts"
DEADLINE_SECONDS: Final = DEADLINES[NAME]


class PurgeInput(ItemEnvelope):
    """The item envelope and, optionally, the document the item names."""

    document_id: LowerCaseUuid | None = None


class PurgeResult(ActivityResult):
    """`{done, deletedChunks, purgedVersions}` with the `ActivityResult` fields."""

    done: bool = False
    deleted_chunks: int = Field(default=0, ge=0)
    purged_versions: int = Field(default=0, ge=0)


def item_document(connection: Connection, item: PurgeInput) -> UUID:
    """The fenced lease update, then the item's document."""
    fence(connection, item, ItemStage.PURGE_DOCUMENT, DEADLINE_SECONDS)
    document_id: UUID | None = connection.execute(
        select(ingestion_run_items.c.document_id).where(ingestion_run_items.c.id == item.item_id)
    ).scalar_one()
    if document_id is None or (item.document_id is not None and item.document_id != document_id):
        raise ValueError("a document clean-up item names its document")
    return document_id


def leftover_sets(connection: Connection, document_id: UUID) -> list[RetiredSet]:
    rows = connection.execute(
        select(
            chunk_sets.c.id,
            chunk_sets.c.document_id,
            chunk_sets.c.chunk_count,
            physical_indexes.c.name,
        )
        .select_from(chunk_sets)
        .join(physical_indexes, physical_indexes.c.id == chunk_sets.c.physical_index_id)
        .where(
            chunk_sets.c.document_id == document_id,
            chunk_sets.c.state == literal("retired"),
            chunk_sets.c.chunks_deleted_at.is_(None),
            physical_indexes.c.state.in_(literals(("active", "building"))),
        )
    ).all()
    return [RetiredSet(row.id, row.document_id, row.chunk_count, row.name) for row in rows]


def unpurged_versions(connection: Connection, document_id: UUID) -> list[VersionStorage]:
    rows = connection.execute(
        select(
            document_versions.c.id,
            document_versions.c.search_index_id,
            document_versions.c.document_id,
            document_versions.c.version_number,
            document_versions.c.blob_path,
        ).where(
            document_versions.c.document_id == document_id,
            document_versions.c.state.in_(literals(("superseded", "failed"))),
            document_versions.c.storage_purged_at.is_(None),
        )
    ).all()
    return [
        VersionStorage(
            row.id, row.search_index_id, row.document_id, row.version_number, row.blob_path
        )
        for row in rows
    ]


async def purge(services: DocumentServices, item: PurgeInput, span: Span) -> PurgeResult:
    def read(connection: Connection) -> tuple[UUID, list[RetiredSet], list[VersionStorage]]:
        document_id = item_document(connection, item)
        return (
            document_id,
            leftover_sets(connection, document_id),
            unpurged_versions(connection, document_id),
        )

    document_id, sets, versions = await services.sql.run(
        read, name="purge_document_read", retry="transient"
    )
    set_attributes(span, **{"docchat.document_id": str(document_id)})
    budget_ends = services.clock.monotonic() + item.settings.activity_time_budget_seconds
    deleted = purged = 0
    for chunk_set in sets:
        deleted += await remove_retired_set(services, chunk_set)
        if services.clock.monotonic() >= budget_ends:
            return PurgeResult.success(done=False, deleted_chunks=deleted, purged_versions=purged)
    for version in versions:
        await purge_version_storage(services, version)
        purged += 1
        if services.clock.monotonic() >= budget_ends:
            return PurgeResult.success(done=False, deleted_chunks=deleted, purged_versions=purged)
    return PurgeResult.success(done=True, deleted_chunks=deleted, purged_versions=purged)


def _failure(code: str, retry_after: int | None) -> PurgeResult:
    return PurgeResult.failure(code, retry_after_seconds=retry_after)


async def run(payload: object, provider: ServicesProvider = document_services) -> ActivityOutput:
    return await run_document_activity(
        NAME,
        payload,
        PurgeInput,
        purge,
        stage=ItemStage.PURGE_DOCUMENT,
        provider=provider,
        failure=_failure,
    )


async def purge_document_artifacts(payload: object) -> ActivityOutput:
    return await run(payload)


bp: Final = blueprint(purge_document_artifacts)
