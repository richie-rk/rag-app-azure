"""The served-chunk-set filter of the chat pipeline (`12-chat-pipeline.md` section 6.5;
`82-security.md` SEC-16).

After each search pass, one statement returns, for the distinct document IDs among the results (at
most 50, bound as an expanding parameter list), the chunk set each document serves in the
searched physical index, with the document's `source_name`; a deleted document returns no row.
It seeks `PK_served_chunk_sets` (`document_id`, `physical_index_id`) and `PK_documents`, and runs
with bound parameters only.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from sqlalchemy import Select, bindparam, select

from docchat_core.sql.engine import SqlEngine, request_path
from docchat_core.sql.tables.ingestion import documents, served_chunk_sets
from docchat_core.sql.tables.schema import BIT_FALSE

SERVED_CHUNK_SETS: Final = "served_chunk_sets"
"""The statement set's name: the unit's span `docchat.sql.served_chunk_sets` and its metrics."""

_EXAMPLE_PHYSICAL_INDEX: Final = UUID("9c4d5e6f-7a8b-4c9d-8e1f-2a3b4c5d6e63")
_EXAMPLE_DOCUMENTS: Final = (
    UUID("a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c64"),
    UUID("b2c3d4e5-f6a7-4b8c-9d0e-1f2a3b4c5d65"),
)


def _statement() -> Select[Any]:
    s = served_chunk_sets.alias("s")
    d = documents.alias("d")
    return (
        select(s.c.document_id, s.c.chunk_set_id, s.c.document_version_id, d.c.source_name)
        .select_from(s.join(d, d.c.id == s.c.document_id))
        .where(
            s.c.physical_index_id == bindparam("physical_index_id"),
            s.c.document_id.in_(bindparam("document_ids", expanding=True)),
            d.c.is_deleted == BIT_FALSE,
        )
    )


SERVED_CHUNK_SETS_QUERY: Final = _statement()


@dataclass(frozen=True, slots=True)
class ServedChunkSet:
    """The chunk set one document serves in the physical index, and the document's name."""

    document_id: UUID
    chunk_set_id: UUID
    document_version_id: UUID
    source_name: str


@request_path(
    example_params={
        "physical_index_id": _EXAMPLE_PHYSICAL_INDEX,
        "document_ids": _EXAMPLE_DOCUMENTS,
    }
)
async def read_served_chunk_sets(
    engine: SqlEngine, *, physical_index_id: UUID, document_ids: Sequence[UUID]
) -> dict[UUID, ServedChunkSet]:
    """The served chunk set of each of `document_ids` in the physical index, by document; a
    document that serves none there, or is deleted, is absent. No statement runs for no IDs."""
    if not document_ids:
        return {}
    rows = await engine.fetch_all(
        SERVED_CHUNK_SETS_QUERY,
        {"physical_index_id": physical_index_id, "document_ids": list(dict.fromkeys(document_ids))},
        name=SERVED_CHUNK_SETS,
    )
    return {
        row.document_id: ServedChunkSet(
            document_id=row.document_id,
            chunk_set_id=row.chunk_set_id,
            document_version_id=row.document_version_id,
            source_name=row.source_name,
        )
        for row in rows
    }
