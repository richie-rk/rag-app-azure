"""`clean_up_item`, the Clean up step (`14-ingestion.md` sections 6.10 and 13.4).

After a Flip (mode `flipped`) it deletes the chunks of the sets the Flip retired, the stored files
of the version the Flip superseded and the item's work files; after a failure (mode `discard`)
the chunks of the item's own build and the work files. Every step is idempotent: chunks are
deleted by their computable keys (a missing key counts as deleted) and only from a set that is
`retired`, so a retried clean-up can never remove chunks a later build serves; an absent blob
counts as deleted. A failure is the retryable `cleanup_failed`; the orchestrator makes 3
attempts after a Flip (then the warning `cleanup_incomplete`) and 2 in discard mode, and sweep
duty 6 finishes what is left. Readers never see a difference, because the served rows changed in
the Flip.
"""

from __future__ import annotations

from typing import Final, Literal
from uuid import UUID

from opentelemetry.trace import Span
from pydantic import Field, model_validator
from sqlalchemy import Connection

from docchat_core.ingestion.models import ActivityResult, ItemStage
from docchat_ingestion.activities.documents import (
    CLEANUP_FAILED,
    DEADLINES,
    ActivityOutput,
    DocumentServices,
    PlanInput,
    RetiredSet,
    ServicesProvider,
    VersionStorage,
    blueprint,
    delete_prefix,
    document_services,
    fence,
    item_ids,
    purge_version_storage,
    remove_retired_set,
    retired_sets_of,
    run_document_activity,
    version_storage,
)
from docchat_ingestion.activities.flip_chunk_set import FlipResult

NAME: Final = "clean_up_item"
DEADLINE_SECONDS: Final = DEADLINES[NAME]

type CleanUpMode = Literal["flipped", "discard"]


class CleanUpInput(PlanInput):
    """The item envelope, the plan, the Flip's output (null in discard mode) and the mode."""

    flip: FlipResult | None = None
    mode: CleanUpMode

    @model_validator(mode="after")
    def _flip_after_a_flip(self) -> CleanUpInput:
        if (self.mode == "flipped") != (self.flip is not None):
            raise ValueError("a clean-up after a Flip carries the Flip's output, and only then")
        return self


class CleanUpResult(ActivityResult):
    """`{deletedChunks, purgedVersion}` with the `ActivityResult` fields."""

    deleted_chunks: int = Field(default=0, ge=0)
    purged_version: bool = False


async def clean_up(services: DocumentServices, item: CleanUpInput, span: Span) -> CleanUpResult:
    del span
    plan = item.plan
    sets: list[UUID]
    if item.flip is not None:
        sets = list(item.flip.retired_chunk_set_ids)
        superseded = item.flip.superseded_version_id
    else:
        sets, superseded = [plan.chunk_set_id], None

    def read(connection: Connection) -> tuple[list[RetiredSet], VersionStorage | None]:
        fence(connection, item, ItemStage.CLEAN_UP, DEADLINE_SECONDS)
        version = None if superseded is None else version_storage(connection, superseded)
        return list(retired_sets_of(connection, sets)), version

    retired, version = await services.sql.run(read, name="clean_up_read", retry="transient")
    deleted = 0
    for chunk_set in retired:
        deleted += await remove_retired_set(services, chunk_set)
    purged = False
    if version is not None:
        await purge_version_storage(services, version)
        purged = True
    await delete_prefix(services.work, plan.work_prefix, code=CLEANUP_FAILED)
    return CleanUpResult.success(deleted_chunks=deleted, purged_version=purged)


def _failure(code: str, retry_after: int | None) -> CleanUpResult:
    return CleanUpResult.failure(code, retry_after_seconds=retry_after)


async def run(payload: object, provider: ServicesProvider = document_services) -> ActivityOutput:
    return await run_document_activity(
        NAME,
        payload,
        CleanUpInput,
        clean_up,
        stage=ItemStage.CLEAN_UP,
        provider=provider,
        ids=item_ids,
        failure=_failure,
    )


async def clean_up_item(payload: object) -> ActivityOutput:
    return await run(payload)


bp: Final = blueprint(clean_up_item)
