"""The eleven ingestion tables and their grants (`80-data-model.md` sections 2.9 to 2.20 and
2.22; ADR 0034).

The two circular foreign keys, `FK_search_indexes_active_physical` and
`FK_documents_current_version`, are created after both of their tables exist (`use_alter`).
`document_versions` has an `INSERT` trigger, so its table is declared with
`implicit_returning=False`: SQL Server refuses an `OUTPUT` clause without `INTO` on such a table.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import (
    BINARY,
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Table,
    UniqueConstraint,
    and_,
    literal_column,
)
from sqlalchemy.dialects.mssql import (
    BIT,
    NVARCHAR,
    ROWVERSION,
    TINYINT,
    UNIQUEIDENTIFIER,
    VARCHAR,
)

from docchat_core.sql.tables.schema import (
    BIT_FALSE,
    BIT_TRUE,
    GrantMatrix,
    actor,
    in_list,
    metadata,
    nvarchar_literal,
    rights,
    utc_time,
)

SEARCH_INDEX_STATUSES: Final = (
    "provisioning",
    "ready",
    "reingest_required",
    "reingesting",
    "rebuilding",
    "failed",
    "deleting",
    "deleted",
)
PHYSICAL_INDEX_STATES: Final = ("building", "active", "retiring", "deleted")
DOCUMENT_VERSION_STATES: Final = (
    "uploading",
    "pending",
    "superseded",
    "ingesting",
    "ingested",
    "failed",
)
ACTIVE_VERSION_STATES: Final = ("pending", "ingesting", "ingested")
"""The states the two SHA-256 indexes cover; a failed, superseded or uploading version never
blocks the same bytes."""
SCAN_STATUSES: Final = ("not_required", "awaiting", "clean", "malicious", "failed", "unavailable")
DOCUMENT_PDF_STATUSES: Final = ("built", "too_large")
CONTENT_TYPES: Final = (
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
)
CHUNK_SET_STATES: Final = ("building", "ready", "served", "retired")
LIVE_CHUNK_SET_STATES: Final = ("building", "ready", "served")
RUN_KINDS: Final = (
    "upload",
    "document_reingest",
    "document_delete",
    "index_create",
    "index_reingest",
    "index_rebuild",
    "index_delete",
    "maintenance",
)
RUN_STATES: Final = ("queued", "running", "completed", "completed_with_errors", "failed")
OPEN_RUN_STATES: Final = ("queued", "running")
ITEM_OPERATIONS: Final = (
    "ingest",
    "cleanup_document",
    "rerender",
    "create_physical_index",
    "delete_physical_index",
    "purge_index_blobs",
)
ITEM_STATES: Final = ("queued", "running", "succeeded", "skipped", "failed", "cancelled")
ITEM_STAGES: Final = (
    "validate",
    "extract",
    "enrich",
    "chunk",
    "embed",
    "index",
    "render",
    "flip",
    "clean_up",
    "purge_document",
    "create_index",
    "delete_index",
    "purge_blobs",
)
CANARY_RESULTS: Final = ("clean", "malicious", "failed", "unavailable")
EMBEDDING_DIMENSIONS_CHECK: Final = "IN (256, 1024, 3072)"

ingestion_methods: Final = Table(
    "ingestion_methods",
    metadata,
    Column("method_key", NVARCHAR(64), nullable=False),
    Column("method_version", Integer, nullable=False),
    Column("display_name", NVARCHAR(100), nullable=False),
    Column("description", NVARCHAR(500), nullable=False),
    Column("parameter_schema", NVARCHAR(None), nullable=False),
    Column("default_parameters", NVARCHAR(None), nullable=False),
    Column("supported_content_types", NVARCHAR(400), nullable=False),
    Column("is_active", BIT, nullable=False, server_default=BIT_TRUE),
    utc_time("created_at", nullable=False, defaulted=True),
    PrimaryKeyConstraint(
        "method_key", "method_version", name="PK_ingestion_methods", mssql_clustered=True
    ),
    CheckConstraint(
        "method_key COLLATE Latin1_General_BIN2 NOT LIKE N'%[^a-z0-9_]%'",
        name="CK_ingestion_methods_key",
    ),
    CheckConstraint("method_version >= 1", name="CK_ingestion_methods_version"),
    CheckConstraint("ISJSON(parameter_schema) = 1", name="CK_ingestion_methods_parameter_schema"),
    CheckConstraint(
        "ISJSON(default_parameters) = 1", name="CK_ingestion_methods_default_parameters"
    ),
    CheckConstraint(
        "ISJSON(supported_content_types) = 1", name="CK_ingestion_methods_content_types"
    ),
)

search_indexes: Final = Table(
    "search_indexes",
    metadata,
    Column("id", UNIQUEIDENTIFIER, nullable=False),
    Column("display_name", NVARCHAR(80), nullable=False),
    Column("description", NVARCHAR(1200), nullable=True),
    Column("method_key", NVARCHAR(64), nullable=False),
    Column("method_version", Integer, nullable=False),
    Column("method_parameters", NVARCHAR(None), nullable=False),
    Column("method_parameter_hash", BINARY(32), nullable=False),
    Column("embedding_model", NVARCHAR(64), nullable=False),
    Column("embedding_dimensions", Integer, nullable=False),
    Column(
        "language_analyzer",
        NVARCHAR(32),
        nullable=False,
        server_default=nvarchar_literal("standard.lucene"),
    ),
    Column("active_physical_index_id", UNIQUEIDENTIFIER, nullable=True),
    Column("status", NVARCHAR(32), nullable=False, server_default=nvarchar_literal("provisioning")),
    utc_time("status_changed_at", nullable=False, defaulted=True),
    utc_time("created_at", nullable=False, defaulted=True),
    utc_time("updated_at", nullable=False, defaulted=True),
    actor("created_by", nullable=False),
    actor("updated_by", nullable=False),
    Column("row_version", ROWVERSION, nullable=False),
    PrimaryKeyConstraint("id", name="PK_search_indexes", mssql_clustered=True),
    ForeignKeyConstraint(
        ["method_key", "method_version"],
        ["app.ingestion_methods.method_key", "app.ingestion_methods.method_version"],
        name="FK_search_indexes_methods",
    ),
    ForeignKeyConstraint(
        ["active_physical_index_id"],
        ["app.physical_indexes.id"],
        name="FK_search_indexes_active_physical",
        use_alter=True,
    ),
    CheckConstraint(
        "LEN(LTRIM(display_name)) BETWEEN 1 AND 80", name="CK_search_indexes_display_name"
    ),
    CheckConstraint("ISJSON(method_parameters) = 1", name="CK_search_indexes_method_parameters"),
    CheckConstraint(
        f"embedding_dimensions {EMBEDDING_DIMENSIONS_CHECK}", name="CK_search_indexes_dimensions"
    ),
    CheckConstraint(in_list("status", SEARCH_INDEX_STATUSES), name="CK_search_indexes_status"),
    Index("IX_search_indexes_status", "status"),
)
Index(
    "UX_search_indexes_display_name",
    search_indexes.c.display_name,
    unique=True,
    mssql_where=search_indexes.c.status != nvarchar_literal("deleted"),
)

ingestion_runs: Final = Table(
    "ingestion_runs",
    metadata,
    Column("id", UNIQUEIDENTIFIER, nullable=False),
    Column("kind", NVARCHAR(32), nullable=False),
    Column("search_index_id", UNIQUEIDENTIFIER, nullable=False),
    Column("priority", TINYINT, nullable=False),
    Column("state", NVARCHAR(32), nullable=False, server_default=nvarchar_literal("queued")),
    actor("requested_by", nullable=True),
    Column("traceparent", VARCHAR(55), nullable=True),
    Column("tracestate", VARCHAR(512), nullable=True),
    Column("total_items", Integer, nullable=True),
    Column("succeeded_items", Integer, nullable=True),
    Column("skipped_items", Integer, nullable=True),
    Column("failed_items", Integer, nullable=True),
    Column("cancelled_items", Integer, nullable=True),
    utc_time("created_at", nullable=False, defaulted=True),
    utc_time("started_at", nullable=True),
    utc_time("completed_at", nullable=True),
    PrimaryKeyConstraint("id", name="PK_ingestion_runs", mssql_clustered=True),
    ForeignKeyConstraint(
        ["search_index_id"], ["app.search_indexes.id"], name="FK_ingestion_runs_search_indexes"
    ),
    CheckConstraint(in_list("kind", RUN_KINDS), name="CK_ingestion_runs_kind"),
    CheckConstraint("priority IN (0, 1)", name="CK_ingestion_runs_priority"),
    CheckConstraint(in_list("state", RUN_STATES), name="CK_ingestion_runs_state"),
    CheckConstraint(
        "traceparent IS NULL OR LEN(traceparent) = 55", name="CK_ingestion_runs_traceparent"
    ),
)
Index(
    "IX_ingestion_runs_created",
    ingestion_runs.c.created_at.desc(),
    ingestion_runs.c.id.desc(),
    mssql_include=[
        "kind",
        "search_index_id",
        "priority",
        "state",
        "requested_by",
        "started_at",
        "completed_at",
    ],
)
Index(
    "IX_ingestion_runs_index_created",
    ingestion_runs.c.search_index_id,
    ingestion_runs.c.created_at.desc(),
    ingestion_runs.c.id.desc(),
    mssql_include=["kind", "priority", "state", "requested_by", "started_at", "completed_at"],
)
Index(
    "IX_ingestion_runs_open",
    ingestion_runs.c.state,
    mssql_where=ingestion_runs.c.state.in_([nvarchar_literal(s) for s in OPEN_RUN_STATES]),
)

physical_indexes: Final = Table(
    "physical_indexes",
    metadata,
    Column("id", UNIQUEIDENTIFIER, nullable=False),
    Column("search_index_id", UNIQUEIDENTIFIER, nullable=False),
    Column("generation", Integer, nullable=False),
    Column("name", NVARCHAR(128), nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("embedding_model", NVARCHAR(64), nullable=False),
    Column("embedding_model_version", NVARCHAR(32), nullable=False),
    Column("embedding_dimensions", Integer, nullable=False),
    Column("embedding_encoding", NVARCHAR(32), nullable=False),
    Column("embedding_max_input_tokens", Integer, nullable=False),
    Column("language_analyzer", NVARCHAR(32), nullable=False),
    Column("query_embedding_deployment", NVARCHAR(64), nullable=False),
    Column("ingestion_embedding_deployment", NVARCHAR(64), nullable=False),
    Column("state", NVARCHAR(32), nullable=False, server_default=nvarchar_literal("building")),
    Column("build_run_id", UNIQUEIDENTIFIER, nullable=True),
    utc_time("provisioned_at", nullable=True),
    utc_time("created_at", nullable=False, defaulted=True),
    utc_time("activated_at", nullable=True),
    utc_time("retiring_at", nullable=True),
    utc_time("delete_after", nullable=True),
    utc_time("deleted_at", nullable=True),
    PrimaryKeyConstraint("id", name="PK_physical_indexes", mssql_clustered=True),
    ForeignKeyConstraint(
        ["search_index_id"],
        ["app.search_indexes.id"],
        name="FK_physical_indexes_search_indexes",
    ),
    ForeignKeyConstraint(
        ["build_run_id"], ["app.ingestion_runs.id"], name="FK_physical_indexes_build_run"
    ),
    CheckConstraint("generation >= 1", name="CK_physical_indexes_generation"),
    CheckConstraint("schema_version >= 1", name="CK_physical_indexes_schema_version"),
    CheckConstraint(
        f"embedding_dimensions {EMBEDDING_DIMENSIONS_CHECK}",
        name="CK_physical_indexes_dimensions",
    ),
    CheckConstraint(in_list("state", PHYSICAL_INDEX_STATES), name="CK_physical_indexes_state"),
    Index("UX_physical_indexes_name", "name", unique=True),
    Index("UX_physical_indexes_generation", "search_index_id", "generation", unique=True),
)
Index(
    "UX_physical_indexes_one_active",
    physical_indexes.c.search_index_id,
    unique=True,
    mssql_where=physical_indexes.c.state == nvarchar_literal("active"),
)
Index(
    "UX_physical_indexes_one_building",
    physical_indexes.c.search_index_id,
    unique=True,
    mssql_where=physical_indexes.c.state == nvarchar_literal("building"),
)
Index(
    "IX_physical_indexes_retiring",
    physical_indexes.c.state,
    physical_indexes.c.delete_after,
    mssql_where=physical_indexes.c.state == nvarchar_literal("retiring"),
)

documents: Final = Table(
    "documents",
    metadata,
    Column("id", UNIQUEIDENTIFIER, nullable=False),
    Column("search_index_id", UNIQUEIDENTIFIER, nullable=False),
    Column("source_name", NVARCHAR(255), nullable=False),
    Column("normalized_name", NVARCHAR(255), nullable=False),
    Column("current_version_id", UNIQUEIDENTIFIER, nullable=True),
    Column("next_version_number", Integer, nullable=False, server_default=literal_column("1")),
    Column("is_deleted", BIT, nullable=False, server_default=BIT_FALSE),
    utc_time("deleted_at", nullable=True),
    actor("deleted_by", nullable=True),
    utc_time("created_at", nullable=False, defaulted=True),
    utc_time("updated_at", nullable=False, defaulted=True),
    actor("created_by", nullable=False),
    actor("updated_by", nullable=False),
    Column("row_version", ROWVERSION, nullable=False),
    PrimaryKeyConstraint("id", name="PK_documents", mssql_clustered=True),
    ForeignKeyConstraint(
        ["search_index_id"], ["app.search_indexes.id"], name="FK_documents_search_indexes"
    ),
    ForeignKeyConstraint(
        ["current_version_id"],
        ["app.document_versions.id"],
        name="FK_documents_current_version",
        use_alter=True,
    ),
    UniqueConstraint("search_index_id", "normalized_name", name="UQ_documents_index_name"),
    UniqueConstraint("id", "search_index_id", name="UQ_documents_id_index"),
    CheckConstraint("next_version_number >= 1", name="CK_documents_next_version_number"),
    CheckConstraint("is_deleted = 0 OR deleted_at IS NOT NULL", name="CK_documents_deleted_at"),
    Index(
        "IX_documents_index_live",
        "search_index_id",
        "is_deleted",
        mssql_include=["current_version_id", "normalized_name"],
    ),
)

document_versions: Final = Table(
    "document_versions",
    metadata,
    Column("id", UNIQUEIDENTIFIER, nullable=False),
    Column("document_id", UNIQUEIDENTIFIER, nullable=False),
    Column("search_index_id", UNIQUEIDENTIFIER, nullable=False),
    Column("version_number", Integer, nullable=False),
    Column("sha256", BINARY(32), nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("content_type", NVARCHAR(100), nullable=False),
    Column("source_name", NVARCHAR(255), nullable=False),
    Column("blob_path", NVARCHAR(200), nullable=False),
    utc_time("blob_last_modified", nullable=True),
    Column("page_count", Integer, nullable=True),
    actor("uploaded_by", nullable=False),
    Column("uploaded_via_project_id", UNIQUEIDENTIFIER, nullable=False),
    Column("state", NVARCHAR(32), nullable=False, server_default=nvarchar_literal("uploading")),
    Column("scan_required", BIT, nullable=False, server_default=BIT_FALSE),
    Column(
        "scan_status",
        NVARCHAR(32),
        nullable=False,
        server_default=nvarchar_literal("not_required"),
    ),
    Column("scan_raw_result", NVARCHAR(256), nullable=True),
    utc_time("scan_time", nullable=True),
    Column("converter_version", NVARCHAR(64), nullable=True),
    Column("converted_sha256", BINARY(32), nullable=True),
    Column("document_pdf_status", NVARCHAR(16), nullable=True),
    Column("failure_code", NVARCHAR(64), nullable=True),
    utc_time("reserved_at", nullable=False, defaulted=True),
    utc_time("committed_at", nullable=True),
    utc_time("ingest_started_at", nullable=True),
    utc_time("ingested_at", nullable=True),
    utc_time("superseded_at", nullable=True),
    utc_time("failed_at", nullable=True),
    utc_time("storage_purged_at", nullable=True),
    utc_time("created_at", nullable=False, defaulted=True),
    PrimaryKeyConstraint("id", name="PK_document_versions", mssql_clustered=True),
    ForeignKeyConstraint(
        ["document_id", "search_index_id"],
        ["app.documents.id", "app.documents.search_index_id"],
        name="FK_document_versions_documents",
    ),
    ForeignKeyConstraint(
        ["search_index_id"],
        ["app.search_indexes.id"],
        name="FK_document_versions_search_indexes",
    ),
    ForeignKeyConstraint(
        ["uploaded_via_project_id"], ["app.projects.id"], name="FK_document_versions_projects"
    ),
    UniqueConstraint("document_id", "version_number", name="UQ_document_versions_number"),
    CheckConstraint("version_number >= 1", name="CK_document_versions_version_number"),
    CheckConstraint("size_bytes > 0", name="CK_document_versions_size"),
    CheckConstraint(
        in_list("content_type", CONTENT_TYPES), name="CK_document_versions_content_type"
    ),
    CheckConstraint(in_list("state", DOCUMENT_VERSION_STATES), name="CK_document_versions_state"),
    CheckConstraint(in_list("scan_status", SCAN_STATUSES), name="CK_document_versions_scan_status"),
    CheckConstraint(
        "converted_sha256 IS NULL OR content_type <> N'application/pdf'",
        name="CK_document_versions_converted",
    ),
    CheckConstraint(
        "document_pdf_status IS NULL OR " + in_list("document_pdf_status", DOCUMENT_PDF_STATUSES),
        name="CK_document_versions_document_pdf",
    ),
    CheckConstraint(
        "state = N'uploading' OR committed_at IS NOT NULL OR state = N'failed'",
        name="CK_document_versions_committed",
    ),
    Index(
        "IX_document_versions_document_state",
        "document_id",
        "state",
        mssql_include=["committed_at", "version_number", "sha256"],
    ),
    implicit_returning=False,
)
_active_versions = document_versions.c.state.in_(
    [nvarchar_literal(state) for state in ACTIVE_VERSION_STATES]
)
Index(
    "UX_document_versions_document_sha",
    document_versions.c.document_id,
    document_versions.c.sha256,
    unique=True,
    mssql_where=_active_versions,
)
Index(
    "UX_document_versions_index_sha",
    document_versions.c.search_index_id,
    document_versions.c.sha256,
    unique=True,
    mssql_where=_active_versions,
)
Index(
    "UX_document_versions_one_ingested",
    document_versions.c.document_id,
    unique=True,
    mssql_where=document_versions.c.state == nvarchar_literal("ingested"),
)
Index(
    "IX_document_versions_pending",
    document_versions.c.state,
    document_versions.c.committed_at,
    mssql_include=["document_id", "search_index_id", "scan_required", "scan_status"],
    mssql_where=document_versions.c.state == nvarchar_literal("pending"),
)
Index(
    "IX_document_versions_uploading",
    document_versions.c.reserved_at,
    mssql_where=document_versions.c.state == nvarchar_literal("uploading"),
)
Index(
    "IX_document_versions_unpurged",
    document_versions.c.state,
    document_versions.c.superseded_at,
    mssql_include=["document_id", "failed_at"],
    mssql_where=document_versions.c.storage_purged_at.is_(None),
)

chunk_sets: Final = Table(
    "chunk_sets",
    metadata,
    Column("id", UNIQUEIDENTIFIER, nullable=False),
    Column("document_id", UNIQUEIDENTIFIER, nullable=False),
    Column("document_version_id", UNIQUEIDENTIFIER, nullable=False),
    Column("physical_index_id", UNIQUEIDENTIFIER, nullable=False),
    Column("method_key", NVARCHAR(64), nullable=False),
    Column("method_version", Integer, nullable=False),
    Column("parameter_hash", BINARY(32), nullable=False),
    Column("method_parameters", NVARCHAR(None), nullable=False),
    Column("embedding_model", NVARCHAR(64), nullable=False),
    Column("embedding_dimensions", Integer, nullable=False),
    Column("chunk_count", Integer, nullable=True),
    Column("parse_seconds_used", Integer, nullable=False, server_default=literal_column("0")),
    Column("pdf_sha256", BINARY(32), nullable=True),
    Column("state", NVARCHAR(32), nullable=False, server_default=nvarchar_literal("building")),
    Column("rendition_rules_version", Integer, nullable=True),
    Column("build_item_id", UNIQUEIDENTIFIER, nullable=True),
    utc_time("created_at", nullable=False, defaulted=True),
    utc_time("ready_at", nullable=True),
    utc_time("served_at", nullable=True),
    utc_time("retired_at", nullable=True),
    utc_time("chunks_deleted_at", nullable=True),
    PrimaryKeyConstraint("id", name="PK_chunk_sets", mssql_clustered=True),
    ForeignKeyConstraint(["document_id"], ["app.documents.id"], name="FK_chunk_sets_documents"),
    ForeignKeyConstraint(
        ["document_version_id"],
        ["app.document_versions.id"],
        name="FK_chunk_sets_document_versions",
    ),
    ForeignKeyConstraint(
        ["physical_index_id"],
        ["app.physical_indexes.id"],
        name="FK_chunk_sets_physical_indexes",
    ),
    ForeignKeyConstraint(
        ["method_key", "method_version"],
        ["app.ingestion_methods.method_key", "app.ingestion_methods.method_version"],
        name="FK_chunk_sets_methods",
    ),
    CheckConstraint("ISJSON(method_parameters) = 1", name="CK_chunk_sets_method_parameters"),
    CheckConstraint(
        "chunk_count IS NULL OR chunk_count BETWEEN 1 AND 100000", name="CK_chunk_sets_chunk_count"
    ),
    CheckConstraint("parse_seconds_used >= 0", name="CK_chunk_sets_parse_seconds"),
    CheckConstraint(in_list("state", CHUNK_SET_STATES), name="CK_chunk_sets_state"),
    Index("IX_chunk_sets_document", "document_id", "physical_index_id", "state"),
)
Index(
    "UX_chunk_sets_build_key",
    chunk_sets.c.document_version_id,
    chunk_sets.c.physical_index_id,
    chunk_sets.c.method_key,
    chunk_sets.c.method_version,
    chunk_sets.c.parameter_hash,
    unique=True,
    mssql_where=chunk_sets.c.state.in_([nvarchar_literal(s) for s in LIVE_CHUNK_SET_STATES]),
)
Index(
    "IX_chunk_sets_cleanup",
    chunk_sets.c.retired_at,
    mssql_include=["document_id", "physical_index_id", "chunk_count"],
    mssql_where=and_(
        chunk_sets.c.state == nvarchar_literal("retired"), chunk_sets.c.chunks_deleted_at.is_(None)
    ),
)
Index(
    "IX_chunk_sets_served_rules",
    chunk_sets.c.rendition_rules_version,
    chunk_sets.c.served_at,
    mssql_include=["document_id", "document_version_id", "physical_index_id"],
    mssql_where=chunk_sets.c.state == nvarchar_literal("served"),
)

served_chunk_sets: Final = Table(
    "served_chunk_sets",
    metadata,
    Column("document_id", UNIQUEIDENTIFIER, nullable=False),
    Column("physical_index_id", UNIQUEIDENTIFIER, nullable=False),
    Column("chunk_set_id", UNIQUEIDENTIFIER, nullable=False),
    Column("document_version_id", UNIQUEIDENTIFIER, nullable=False),
    utc_time("served_at", nullable=False, defaulted=True),
    PrimaryKeyConstraint(
        "document_id", "physical_index_id", name="PK_served_chunk_sets", mssql_clustered=True
    ),
    ForeignKeyConstraint(
        ["document_id"], ["app.documents.id"], name="FK_served_chunk_sets_documents"
    ),
    ForeignKeyConstraint(
        ["physical_index_id"],
        ["app.physical_indexes.id"],
        name="FK_served_chunk_sets_physical_indexes",
    ),
    ForeignKeyConstraint(
        ["chunk_set_id"], ["app.chunk_sets.id"], name="FK_served_chunk_sets_chunk_sets"
    ),
    ForeignKeyConstraint(
        ["document_version_id"],
        ["app.document_versions.id"],
        name="FK_served_chunk_sets_document_versions",
    ),
    UniqueConstraint("chunk_set_id", name="UQ_served_chunk_sets_chunk_set"),
    Index(
        "IX_served_chunk_sets_physical",
        "physical_index_id",
        "document_id",
        mssql_include=["document_version_id", "chunk_set_id"],
    ),
)

ingestion_run_items: Final = Table(
    "ingestion_run_items",
    metadata,
    Column("id", UNIQUEIDENTIFIER, nullable=False),
    Column("run_id", UNIQUEIDENTIFIER, nullable=False),
    Column("queue_seq", BigInteger, Identity(start=1, increment=1), nullable=False),
    Column("operation", NVARCHAR(32), nullable=False),
    Column("search_index_id", UNIQUEIDENTIFIER, nullable=False),
    Column("physical_index_id", UNIQUEIDENTIFIER, nullable=True),
    Column("document_id", UNIQUEIDENTIFIER, nullable=True),
    Column("document_version_id", UNIQUEIDENTIFIER, nullable=True),
    Column("priority", TINYINT, nullable=False),
    Column("state", NVARCHAR(32), nullable=False, server_default=nvarchar_literal("queued")),
    Column("stage", NVARCHAR(32), nullable=True),
    Column("claim_count", Integer, nullable=False, server_default=literal_column("0")),
    Column("instance_id", NVARCHAR(100), nullable=True),
    utc_time("lease_expires_at", nullable=True),
    utc_time("not_before", nullable=True),
    Column("chunk_set_id", UNIQUEIDENTIFIER, nullable=True),
    Column("outcome_code", NVARCHAR(64), nullable=True),
    Column("outcome_message", NVARCHAR(400), nullable=True),
    Column("warnings", NVARCHAR(4000), nullable=True),
    Column("embedding_tokens", Integer, nullable=True),
    Column("summary_input_tokens", Integer, nullable=True),
    Column("summary_output_tokens", Integer, nullable=True),
    Column("docintel_pages", Integer, nullable=True),
    utc_time("created_at", nullable=False, defaulted=True),
    utc_time("started_at", nullable=True),
    utc_time("completed_at", nullable=True),
    PrimaryKeyConstraint("id", name="PK_ingestion_run_items", mssql_clustered=True),
    ForeignKeyConstraint(["run_id"], ["app.ingestion_runs.id"], name="FK_run_items_runs"),
    ForeignKeyConstraint(
        ["search_index_id"], ["app.search_indexes.id"], name="FK_run_items_search_indexes"
    ),
    ForeignKeyConstraint(
        ["physical_index_id"], ["app.physical_indexes.id"], name="FK_run_items_physical_indexes"
    ),
    ForeignKeyConstraint(["document_id"], ["app.documents.id"], name="FK_run_items_documents"),
    ForeignKeyConstraint(
        ["document_version_id"],
        ["app.document_versions.id"],
        name="FK_run_items_document_versions",
    ),
    ForeignKeyConstraint(["chunk_set_id"], ["app.chunk_sets.id"], name="FK_run_items_chunk_sets"),
    UniqueConstraint("queue_seq", name="UQ_run_items_queue_seq"),
    CheckConstraint(in_list("operation", ITEM_OPERATIONS), name="CK_run_items_operation"),
    CheckConstraint(in_list("state", ITEM_STATES), name="CK_run_items_state"),
    CheckConstraint("stage IS NULL OR " + in_list("stage", ITEM_STAGES), name="CK_run_items_stage"),
    CheckConstraint("warnings IS NULL OR ISJSON(warnings) = 1", name="CK_run_items_warnings"),
    CheckConstraint(
        "(operation = N'ingest' AND document_id IS NOT NULL AND document_version_id IS NOT NULL "
        "AND physical_index_id IS NOT NULL) OR "
        "(operation = N'cleanup_document' AND document_id IS NOT NULL "
        "AND document_version_id IS NULL) OR "
        "(operation = N'rerender' AND document_id IS NOT NULL AND document_version_id IS NOT NULL "
        "AND physical_index_id IS NOT NULL AND chunk_set_id IS NOT NULL) OR "
        "(operation IN (N'create_physical_index', N'delete_physical_index') "
        "AND physical_index_id IS NOT NULL AND document_id IS NULL) OR "
        "(operation = N'purge_index_blobs' AND document_id IS NULL AND physical_index_id IS NULL)",
        name="CK_run_items_shape",
    ),
    Index("IX_run_items_run", "run_id", "state"),
)
Index(
    "UX_run_items_run_version",
    ingestion_run_items.c.run_id,
    ingestion_run_items.c.document_version_id,
    unique=True,
    mssql_where=ingestion_run_items.c.document_version_id.is_not(None),
)
Index(
    "UX_run_items_running_target",
    ingestion_run_items.c.document_id,
    ingestion_run_items.c.physical_index_id,
    unique=True,
    mssql_where=ingestion_run_items.c.state == nvarchar_literal("running"),
)
Index(
    "IX_run_items_claim",
    ingestion_run_items.c.priority,
    ingestion_run_items.c.queue_seq,
    mssql_include=[
        "document_id",
        "document_version_id",
        "physical_index_id",
        "operation",
        "not_before",
        "created_at",
    ],
    mssql_where=ingestion_run_items.c.state == nvarchar_literal("queued"),
)
Index(
    "IX_run_items_running",
    ingestion_run_items.c.lease_expires_at,
    mssql_include=["document_id", "physical_index_id"],
    mssql_where=ingestion_run_items.c.state == nvarchar_literal("running"),
)
Index(
    "IX_run_items_document",
    ingestion_run_items.c.document_id,
    ingestion_run_items.c.created_at.desc(),
)

ingestion_dispatcher_state: Final = Table(
    "ingestion_dispatcher_state",
    metadata,
    # Not an identity: the single row is id 1, which the first migration inserts.
    Column("id", TINYINT, autoincrement=False, nullable=False),
    Column("generation", Integer, nullable=False, server_default=literal_column("0")),
    utc_time("last_sweep_at", nullable=False, defaulted=True),
    utc_time("last_claim_at", nullable=True),
    Column("running_items", Integer, nullable=False, server_default=literal_column("0")),
    utc_time("updated_at", nullable=False, defaulted=True),
    PrimaryKeyConstraint("id", name="PK_ingestion_dispatcher_state", mssql_clustered=True),
    CheckConstraint("id = 1", name="CK_ingestion_dispatcher_state_id"),
)

scan_canaries: Final = Table(
    "scan_canaries",
    metadata,
    Column("id", UNIQUEIDENTIFIER, nullable=False),
    Column("blob_path", NVARCHAR(256), nullable=False),
    utc_time("written_at", nullable=False, defaulted=True),
    Column("result", NVARCHAR(32), nullable=True),
    utc_time("scanned_at", nullable=True),
    Column("minutes_to_result", Integer, nullable=True),
    PrimaryKeyConstraint("id", name="PK_scan_canaries", mssql_clustered=True),
    CheckConstraint(
        "result IS NULL OR " + in_list("result", CANARY_RESULTS), name="CK_scan_canaries_result"
    ),
    CheckConstraint(
        "minutes_to_result IS NULL OR minutes_to_result >= 0", name="CK_scan_canaries_minutes"
    ),
    Index("IX_scan_canaries_written_at", "written_at", mssql_include=["result"]),
)

INGESTION_TABLES: Final = (
    ingestion_methods,
    search_indexes,
    physical_indexes,
    documents,
    document_versions,
    chunk_sets,
    served_chunk_sets,
    ingestion_runs,
    ingestion_run_items,
    ingestion_dispatcher_state,
    scan_canaries,
)

API_DOCUMENT_VERSION_COLUMNS: Final = (
    "state",
    "reserved_at",
    "uploaded_by",
    "uploaded_via_project_id",
    "committed_at",
    "blob_last_modified",
    "failure_code",
    "failed_at",
    "superseded_at",
)
"""Footnote 1: the `docchat_api` column grant on `app.document_versions`."""

OPERATOR_DOCUMENT_VERSION_COLUMNS: Final = ("state", "failure_code", "failed_at")
"""Footnote 2: the `docchat_config_operator` column grant on `app.document_versions`."""

INGESTION_GRANTS: Final[GrantMatrix] = {
    "ingestion_methods": {
        "docchat_api": rights("S"),
        "docchat_ingestion": rights("S"),
        "docchat_config_operator": rights("S"),
    },
    "search_indexes": {
        "docchat_api": rights("SIU"),
        "docchat_ingestion": rights("SU"),
        "docchat_config_operator": rights("SU"),
        "docchat_user_data_operator": rights("S"),
    },
    "physical_indexes": {
        "docchat_api": rights("SIU"),
        "docchat_ingestion": rights("SU"),
        "docchat_config_operator": rights("SIU"),
    },
    "documents": {
        "docchat_api": rights("SIU"),
        "docchat_ingestion": rights("SUD"),
        "docchat_config_operator": rights("S"),
        "docchat_user_data_operator": rights("S"),
    },
    "document_versions": {
        "docchat_api": rights("SI", update_columns=API_DOCUMENT_VERSION_COLUMNS),
        "docchat_ingestion": rights("SUD"),
        "docchat_config_operator": rights("S", update_columns=OPERATOR_DOCUMENT_VERSION_COLUMNS),
        "docchat_user_data_operator": rights("S"),
    },
    "chunk_sets": {
        "docchat_api": rights("SU"),
        "docchat_ingestion": rights("SIU"),
        "docchat_config_operator": rights("SU"),
    },
    "served_chunk_sets": {
        "docchat_api": rights("SD"),
        "docchat_ingestion": rights("SIUD"),
        "docchat_config_operator": rights("S"),
    },
    "ingestion_runs": {
        "docchat_api": rights("SI"),
        "docchat_ingestion": rights("SIUD"),
        "docchat_config_operator": rights("SIU"),
        "docchat_user_data_operator": rights("S"),
    },
    "ingestion_run_items": {
        "docchat_api": rights("SIU"),
        "docchat_ingestion": rights("SIUD"),
        "docchat_config_operator": rights("SIU"),
    },
    "ingestion_dispatcher_state": {
        "docchat_ingestion": rights("SU"),
        "docchat_config_operator": rights("S"),
    },
    "scan_canaries": {
        "docchat_api": rights("S"),
        "docchat_ingestion": rights("SIUD"),
        "docchat_config_operator": rights("S"),
    },
}
"""The ingestion area's rows of the grant matrix, the operator roles' rights included."""
