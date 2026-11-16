"""`create_search_index` (`14-ingestion.md` sections 8.12 item 1, 8.2, 8.3 and 8.13; ADRs 0044 and
0045).

Builds the chunk index of the physical index row (its name, dimensions and analyzer, never a
setting's current value) and creates it. An existing index of that name counts as created only
when its fields, types, dimensions and analyzers equal the built definition; otherwise the item
fails with `index_schema_conflict`. Then one transaction marks the physical index provisioned and,
while the logical index has no active physical index, makes this one active and the logical index
`ready`, holding the logical index row's update lock.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

import azure.durable_functions as df
from azure.core.exceptions import (
    HttpResponseError,
    ResourceExistsError,
    ServiceRequestError,
    ServiceResponseError,
)
from opentelemetry.trace import Span
from sqlalchemy import Connection, select, update

from docchat_core.ingestion.models import ActivityResult, ItemEnvelope, ItemStage
from docchat_core.search.schema import build_chunk_index
from docchat_core.sql.tables.ingestion import ingestion_run_items, physical_indexes, search_indexes
from docchat_core.sql.tables.schema import SYSUTCDATETIME
from docchat_core.telemetry import set_attributes
from docchat_ingestion.activities.runtime import IngestionProcess, ProcessProvider, runtime
from docchat_ingestion.activities.support import (
    fence_lease,
    http_status,
    literal,
    retry_after_seconds,
    run_item_activity,
    service_unavailable,
)

NAME: Final = "create_search_index"
DEADLINE_SECONDS: Final = 300
FORBIDDEN: Final = 403
"""How the service refuses an index beyond its quota."""


class CreateSearchIndexResult(ActivityResult):
    """`{activated}` with the `ActivityResult` fields."""

    activated: bool = False


@dataclass(frozen=True, slots=True)
class TargetIndex:
    search_index_id: UUID
    physical_index_id: UUID
    name: str
    dimensions: int
    analyzer: str


def claim_target(connection: Connection, envelope: ItemEnvelope) -> TargetIndex | None:
    """The fenced lease update, then the item's physical index row; `None` for a lost claim."""
    if not fence_lease(
        connection,
        envelope.item_id,
        envelope.claim_number,
        ItemStage.CREATE_INDEX,
        DEADLINE_SECONDS,
    ):
        return None
    row = connection.execute(
        select(
            ingestion_run_items.c.search_index_id,
            physical_indexes.c.id,
            physical_indexes.c.name,
            physical_indexes.c.embedding_dimensions,
            physical_indexes.c.language_analyzer,
        )
        .select_from(
            ingestion_run_items.join(
                physical_indexes, physical_indexes.c.id == ingestion_run_items.c.physical_index_id
            )
        )
        .where(ingestion_run_items.c.id == envelope.item_id)
    ).one()
    return TargetIndex(
        search_index_id=row.search_index_id,
        physical_index_id=row.id,
        name=row.name,
        dimensions=row.embedding_dimensions,
        analyzer=row.language_analyzer,
    )


def _shape(fields: Iterable[Any] | None) -> list[tuple[Any, ...]]:
    """What the comparison looks at, sub-fields included: name, type, dimensions, analyzers."""
    shape = [
        (
            item.name,
            str(item.type),
            getattr(item, "vector_search_dimensions", None),
            getattr(item, "analyzer_name", None),
            getattr(item, "search_analyzer_name", None),
            getattr(item, "index_analyzer_name", None),
            tuple(_shape(getattr(item, "fields", None))),
        )
        for item in fields or ()
    ]
    return sorted(shape, key=lambda entry: str(entry[0]))


def same_schema(existing: Any, built: Any) -> bool:
    """Whether an existing index has the built definition's fields, types, dimensions and
    analyzers."""
    return _shape(getattr(existing, "fields", None)) == _shape(getattr(built, "fields", None))


def activate(connection: Connection, target: TargetIndex) -> bool:
    """Provisioned; and, while the logical index has no active physical index, active with the
    logical index `ready`."""
    connection.execute(
        update(physical_indexes)
        .where(
            physical_indexes.c.id == target.physical_index_id,
            physical_indexes.c.provisioned_at.is_(None),
        )
        .values(provisioned_at=SYSUTCDATETIME)
    )
    active = connection.execute(
        select(search_indexes.c.active_physical_index_id)
        .where(search_indexes.c.id == target.search_index_id)
        .with_hint(search_indexes, "WITH (UPDLOCK, ROWLOCK)", "mssql")
    ).scalar_one()
    if active is not None:
        return False
    pointed = connection.execute(
        update(search_indexes)
        .where(
            search_indexes.c.id == target.search_index_id,
            search_indexes.c.active_physical_index_id.is_(None),
        )
        .values(
            active_physical_index_id=target.physical_index_id,
            status=literal("ready"),
            status_changed_at=SYSUTCDATETIME,
        )
    ).rowcount
    connection.execute(
        update(physical_indexes)
        .where(
            physical_indexes.c.id == target.physical_index_id,
            physical_indexes.c.state == literal("building"),
        )
        .values(state=literal("active"), activated_at=SYSUTCDATETIME, build_run_id=None)
    )
    return bool(pointed == 1)


def is_capacity_error(error: HttpResponseError) -> bool:
    """The service's index-count quota refusal."""
    if http_status(error) == FORBIDDEN:
        return True
    message = getattr(error, "message", None)
    return isinstance(message, str) and "quota" in message.casefold()


def creation_failure(error: Exception) -> CreateSearchIndexResult:
    """A failed `create_index` or `get_index` as its item code: throttled or unavailable is
    retryable `search_unavailable`, the quota is `search_index_capacity`, any other refusal
    `index_create_failed`."""
    if isinstance(error, ServiceRequestError | ServiceResponseError):
        return CreateSearchIndexResult.failure("search_unavailable")
    if service_unavailable(error):
        return CreateSearchIndexResult.failure(
            "search_unavailable", retry_after_seconds=retry_after_seconds(error)
        )
    if isinstance(error, HttpResponseError) and is_capacity_error(error):
        return CreateSearchIndexResult.failure("search_index_capacity")
    return CreateSearchIndexResult.failure("index_create_failed")


async def create(process: IngestionProcess, envelope: ItemEnvelope, span: Span) -> ActivityResult:
    target = await process.sql.run(
        lambda connection: claim_target(connection, envelope),
        name="create_index_claim",
        retry="transient",
    )
    if target is None:
        return CreateSearchIndexResult.failure("lease_lost")
    set_attributes(span, **{"docchat.physical_index_id": str(target.physical_index_id)})
    index = build_chunk_index(target.name, target.dimensions, target.analyzer)
    try:
        try:
            await process.search_indexes.create_index(index)
        except ResourceExistsError:
            existing = await process.search_indexes.get_index(target.name)
            if not same_schema(existing, index):
                return CreateSearchIndexResult.failure("index_schema_conflict")
    except (ServiceRequestError, ServiceResponseError, HttpResponseError) as error:
        return creation_failure(error)
    activated = await process.sql.run(
        lambda connection: activate(connection, target),
        name="create_index_activate",
        retry="transient",
    )
    return CreateSearchIndexResult.success(activated=activated)


async def run(payload: object, provider: ProcessProvider = runtime) -> dict[str, Any]:
    async def body(envelope: ItemEnvelope, span: Span) -> ActivityResult:
        return await create(await provider(), envelope, span)

    return await run_item_activity(NAME, DEADLINE_SECONDS, payload, body)


async def create_search_index(payload: object) -> dict[str, Any]:
    return await run(payload)


bp: Final = df.Blueprint()
bp.activity_trigger(input_name="payload")(create_search_index)
