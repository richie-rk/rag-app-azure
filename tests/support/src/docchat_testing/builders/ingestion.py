"""Row builders of the eleven ingestion tables (`80-data-model.md` sections 2.9 to 2.19).

Each builder returns the values of one valid row for `insert(<table>)`: generated IDs, the
seeded `pagewise` method and `text-embedding-3-large` at 1,024 dimensions, canary file names and
titles, the foreign keys the caller passes, and every scan and rendition column of a version at
its initial value, as the insert trigger of `app.document_versions` requires. Columns with a
server default are left to the server unless a test overrides them; an override naming a column
the table lacks raises `KeyError`.
"""

from __future__ import annotations

from typing import Any, Final
from uuid import UUID

from docchat_core.sql.tables.ingestion import (
    chunk_sets,
    document_versions,
    documents,
    ingestion_dispatcher_state,
    ingestion_methods,
    ingestion_run_items,
    ingestion_runs,
    physical_indexes,
    scan_canaries,
    search_indexes,
    served_chunk_sets,
)
from docchat_testing.builders.canaries import canary
from docchat_testing.builders.rows import (
    build_row,
    compact_json,
    new_id,
    random_sha256,
    sha256_of,
)

METHOD_KEY: Final = "pagewise"
METHOD_VERSION: Final = 1
METHOD_PARAMETERS: Final = {"summarizeTables": True, "ocrLanguages": ["eng"]}
"""The seeded `pagewise` version 1 method and its default parameters (migration `0001`)."""
EMBEDDING_MODEL: Final = "text-embedding-3-large"
EMBEDDING_MODEL_VERSION: Final = "1"
EMBEDDING_DIMENSIONS: Final = 1024
EMBEDDING_ENCODING: Final = "cl100k_base"
EMBEDDING_MAX_INPUT_TOKENS: Final = 8192
QUERY_EMBEDDING_DEPLOYMENT: Final = "embed-query-te3-large-v1"
INGESTION_EMBEDDING_DEPLOYMENT: Final = "embed-ingest-te3-large-v1"
LANGUAGE_ANALYZER: Final = "standard.lucene"
CHUNK_INDEX_SCHEMA_VERSION: Final = 1
PDF: Final = "application/pdf"
DEFAULT_SIZE_BYTES: Final = 1024
INTERACTIVE_PRIORITY: Final = 0
"""The priority of an `upload` run and its items (`80-data-model.md` section 2.16)."""


def physical_index_name(search_index_id: UUID, generation: int) -> str:
    """`docchat-<32 hex of the logical index ID>-g<generation>` (`80-data-model.md` 6.2)."""
    return f"docchat-{search_index_id.hex}-g{generation}"


def source_path(search_index_id: UUID, document_id: UUID, version: int) -> str:
    """`{indexId}/{documentId}/{v}/source` in container `documents` (`80-data-model.md` 4.3)."""
    return f"{search_index_id}/{document_id}/{version}/source"


def ingestion_method_row(**overrides: Any) -> dict[str, Any]:
    """An active `app.ingestion_methods` row of a method the migration didn't seed."""
    return build_row(
        ingestion_methods,
        {
            "method_key": f"test_{new_id().hex[:12]}",
            "method_version": 1,
            "display_name": canary("title"),
            "description": canary("summary"),
            "parameter_schema": compact_json({"type": "object", "additionalProperties": False}),
            "default_parameters": compact_json({}),
            "supported_content_types": compact_json([PDF]),
        },
        overrides,
    )


def search_index_row(**overrides: Any) -> dict[str, Any]:
    """An `app.search_indexes` row with the seeded `pagewise` method, provisioning and without
    an active physical index."""
    actor = new_id()
    parameters = compact_json(METHOD_PARAMETERS)
    return build_row(
        search_indexes,
        {
            "id": new_id(),
            "display_name": canary("title"),
            "method_key": METHOD_KEY,
            "method_version": METHOD_VERSION,
            "method_parameters": parameters,
            "method_parameter_hash": sha256_of(parameters),
            "embedding_model": EMBEDDING_MODEL,
            "embedding_dimensions": EMBEDDING_DIMENSIONS,
            "created_by": actor,
            "updated_by": actor,
        },
        overrides,
    )


def physical_index_row(
    search_index_id: UUID, *, generation: int = 1, **overrides: Any
) -> dict[str, Any]:
    """A `building` `app.physical_indexes` row of the logical index, named as the service
    index of that generation."""
    return build_row(
        physical_indexes,
        {
            "id": new_id(),
            "search_index_id": search_index_id,
            "generation": generation,
            "name": physical_index_name(search_index_id, generation),
            "schema_version": CHUNK_INDEX_SCHEMA_VERSION,
            "embedding_model": EMBEDDING_MODEL,
            "embedding_model_version": EMBEDDING_MODEL_VERSION,
            "embedding_dimensions": EMBEDDING_DIMENSIONS,
            "embedding_encoding": EMBEDDING_ENCODING,
            "embedding_max_input_tokens": EMBEDDING_MAX_INPUT_TOKENS,
            "language_analyzer": LANGUAGE_ANALYZER,
            "query_embedding_deployment": QUERY_EMBEDDING_DEPLOYMENT,
            "ingestion_embedding_deployment": INGESTION_EMBEDDING_DEPLOYMENT,
        },
        overrides,
    )


def document_row(search_index_id: UUID, **overrides: Any) -> dict[str, Any]:
    """A live `app.documents` row with a canary PDF name and no current version yet."""
    actor = new_id()
    name = f"{canary('filename')}.pdf"
    return build_row(
        documents,
        {
            "id": new_id(),
            "search_index_id": search_index_id,
            "source_name": name,
            "normalized_name": name.casefold(),
            "created_by": actor,
            "updated_by": actor,
        },
        overrides,
    )


def document_version_row(
    document_id: UUID,
    search_index_id: UUID,
    uploaded_via_project_id: UUID,
    *,
    version_number: int = 1,
    **overrides: Any,
) -> dict[str, Any]:
    """An `uploading` `app.document_versions` row of a 1 KiB PDF, with every scan and rendition
    column at its initial value (no scan required)."""
    return build_row(
        document_versions,
        {
            "id": new_id(),
            "document_id": document_id,
            "search_index_id": search_index_id,
            "version_number": version_number,
            "sha256": random_sha256(),
            "size_bytes": DEFAULT_SIZE_BYTES,
            "content_type": PDF,
            "source_name": f"{canary('filename')}.pdf",
            "blob_path": source_path(search_index_id, document_id, version_number),
            "uploaded_by": new_id(),
            "uploaded_via_project_id": uploaded_via_project_id,
            "scan_required": False,
            "scan_status": "not_required",
        },
        overrides,
    )


def chunk_set_row(
    document_id: UUID, document_version_id: UUID, physical_index_id: UUID, **overrides: Any
) -> dict[str, Any]:
    """A `building` `app.chunk_sets` row of the seeded `pagewise` method."""
    parameters = compact_json(METHOD_PARAMETERS)
    return build_row(
        chunk_sets,
        {
            "id": new_id(),
            "document_id": document_id,
            "document_version_id": document_version_id,
            "physical_index_id": physical_index_id,
            "method_key": METHOD_KEY,
            "method_version": METHOD_VERSION,
            "parameter_hash": sha256_of(parameters),
            "method_parameters": parameters,
            "embedding_model": EMBEDDING_MODEL,
            "embedding_dimensions": EMBEDDING_DIMENSIONS,
        },
        overrides,
    )


def served_chunk_set_row(
    document_id: UUID,
    physical_index_id: UUID,
    chunk_set_id: UUID,
    document_version_id: UUID,
    **overrides: Any,
) -> dict[str, Any]:
    """The `app.served_chunk_sets` row that serves a chunk set."""
    return build_row(
        served_chunk_sets,
        {
            "document_id": document_id,
            "physical_index_id": physical_index_id,
            "chunk_set_id": chunk_set_id,
            "document_version_id": document_version_id,
        },
        overrides,
    )


def ingestion_run_row(search_index_id: UUID, **overrides: Any) -> dict[str, Any]:
    """A `queued` `upload` `app.ingestion_runs` row at the interactive priority 0 its kind takes."""
    return build_row(
        ingestion_runs,
        {
            "id": new_id(),
            "kind": "upload",
            "search_index_id": search_index_id,
            "priority": INTERACTIVE_PRIORITY,
            "requested_by": new_id(),
        },
        overrides,
    )


def ingestion_run_item_row(
    run_id: UUID,
    search_index_id: UUID,
    *,
    document_id: UUID | None = None,
    document_version_id: UUID | None = None,
    physical_index_id: UUID | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """A `queued` `app.ingestion_run_items` row.

    Its operation follows the targets given, so the row meets `CK_run_items_shape` without an
    override: `ingest` with a document, version and physical index; `cleanup_document` with a
    document alone; `create_physical_index` with a physical index alone; `purge_index_blobs`
    with no target. An `operation` override takes precedence.
    """
    if document_version_id is not None:
        operation = "ingest"
    elif document_id is not None:
        operation = "cleanup_document"
    elif physical_index_id is not None:
        operation = "create_physical_index"
    else:
        operation = "purge_index_blobs"
    return build_row(
        ingestion_run_items,
        {
            "id": new_id(),
            "run_id": run_id,
            "operation": operation,
            "search_index_id": search_index_id,
            "physical_index_id": physical_index_id,
            "document_id": document_id,
            "document_version_id": document_version_id,
            "priority": INTERACTIVE_PRIORITY,
        },
        overrides,
    )


def dispatcher_state_row(**overrides: Any) -> dict[str, Any]:
    """The one `app.ingestion_dispatcher_state` row (`id` 1), which the migration seeds; for a
    test that deletes it and needs it back with chosen values."""
    return build_row(ingestion_dispatcher_state, {"id": 1}, overrides)


def scan_canary_row(**overrides: Any) -> dict[str, Any]:
    """An `app.scan_canaries` row written now with no result yet."""
    canary_id = new_id()
    return build_row(
        scan_canaries,
        {"id": canary_id, "blob_path": f"_scanner-canary/{canary_id}.pdf"},
        overrides,
    )
