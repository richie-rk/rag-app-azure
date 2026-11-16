"""Initial schema: the first version's tables, roles, grants and seed rows.

Creates, in the order of `80-data-model.md` section 2.24, the five database roles; the access
tables `users`, `projects`, `project_members`, `ops_config`, `audit_events`; the eleven ingestion
tables with the trigger `app.trg_document_versions_scan_insert` and the two circular foreign keys
added once both of their tables exist; the access tables that reference ingestion tables,
`project_indexes` and `user_index_overrides`; the grant matrix of section 2.22, the access area's
then the ingestion area's; and the seed rows: the dispatcher heartbeat, the two ingestion methods
and the 73 ops-config rows.

The revision holds its own copy of every name, type and value, so a later change to
`docchat_core.sql.tables` can never rewrite it; `alembic check` proves the two agree.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

import json
from typing import Final

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mssql

type SeedValue = bool | int | float | str | dict[str, object]

# The dialect's type classes have untyped constructors; one instance of each serves every column.
DATETIME2_3: Final = mssql.DATETIME2(precision=3)  # type: ignore[no-untyped-call]
ROWVERSION: Final = mssql.ROWVERSION()  # type: ignore[no-untyped-call]

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None

ROLES: Final = (
    "docchat_api",
    "docchat_ingestion",
    "docchat_config_operator",
    "docchat_conversation_reader",
    "docchat_user_data_operator",
)

SCAN_INSERT_TRIGGER: Final = """CREATE TRIGGER app.trg_document_versions_scan_insert ON app.document_versions AFTER INSERT AS
BEGIN
    SET NOCOUNT ON;
    IF EXISTS (SELECT 1 FROM inserted
               WHERE NOT ((scan_required = 1 AND scan_status = N'awaiting')
                          OR (scan_required = 0 AND scan_status = N'not_required'))
                  OR scan_time IS NOT NULL
                  OR scan_raw_result IS NOT NULL
                  OR converter_version IS NOT NULL
                  OR converted_sha256 IS NOT NULL
                  OR document_pdf_status IS NOT NULL)
        THROW 51010, N'document_versions scan and rendition columns must start at their initial values', 1;
END"""  # noqa: E501 - the statement of the data model, verbatim
"""The statement of `80-data-model.md` section 2.13, verbatim."""

ACCESS_GRANTS: Final = (
    "GRANT SELECT, INSERT, UPDATE ON OBJECT::app.users TO docchat_api",
    "GRANT SELECT ON OBJECT::app.users TO docchat_config_operator",
    "GRANT SELECT, DELETE ON OBJECT::app.users TO docchat_user_data_operator",
    "GRANT SELECT, INSERT, UPDATE ON OBJECT::app.projects TO docchat_api",
    "GRANT SELECT ON OBJECT::app.projects TO docchat_config_operator",
    "GRANT SELECT ON OBJECT::app.projects TO docchat_user_data_operator",
    "GRANT SELECT, INSERT, DELETE ON OBJECT::app.project_members TO docchat_api",
    "GRANT SELECT, DELETE ON OBJECT::app.project_members TO docchat_user_data_operator",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON OBJECT::app.project_indexes TO docchat_api",
    "GRANT SELECT ON OBJECT::app.project_indexes TO docchat_config_operator",
    "GRANT SELECT ON OBJECT::app.project_indexes TO docchat_user_data_operator",
    "GRANT SELECT ON OBJECT::app.user_index_overrides TO docchat_api",
    (
        "GRANT SELECT, INSERT, UPDATE, DELETE ON "
        "OBJECT::app.user_index_overrides TO docchat_config_operator"
    ),
    "GRANT SELECT, DELETE ON OBJECT::app.user_index_overrides TO docchat_user_data_operator",
    "GRANT SELECT ON OBJECT::app.ops_config TO docchat_api",
    "GRANT SELECT ON OBJECT::app.ops_config TO docchat_ingestion",
    "GRANT SELECT, UPDATE ON OBJECT::app.ops_config TO docchat_config_operator",
    "GRANT SELECT ON OBJECT::app.ops_config TO docchat_user_data_operator",
    "GRANT INSERT ON OBJECT::app.audit_events TO docchat_api",
    "GRANT INSERT ON OBJECT::app.audit_events TO docchat_ingestion",
    "GRANT INSERT ON OBJECT::app.audit_events TO docchat_config_operator",
    "GRANT INSERT ON OBJECT::app.audit_events TO docchat_conversation_reader",
    "GRANT SELECT, INSERT ON OBJECT::app.audit_events TO docchat_user_data_operator",
)

INGESTION_GRANTS: Final = (
    "GRANT SELECT ON OBJECT::app.ingestion_methods TO docchat_api",
    "GRANT SELECT ON OBJECT::app.ingestion_methods TO docchat_ingestion",
    "GRANT SELECT ON OBJECT::app.ingestion_methods TO docchat_config_operator",
    "GRANT SELECT, INSERT, UPDATE ON OBJECT::app.search_indexes TO docchat_api",
    "GRANT SELECT, UPDATE ON OBJECT::app.search_indexes TO docchat_ingestion",
    "GRANT SELECT, UPDATE ON OBJECT::app.search_indexes TO docchat_config_operator",
    "GRANT SELECT ON OBJECT::app.search_indexes TO docchat_user_data_operator",
    "GRANT SELECT, INSERT, UPDATE ON OBJECT::app.physical_indexes TO docchat_api",
    "GRANT SELECT, UPDATE ON OBJECT::app.physical_indexes TO docchat_ingestion",
    "GRANT SELECT, INSERT, UPDATE ON OBJECT::app.physical_indexes TO docchat_config_operator",
    "GRANT SELECT, INSERT, UPDATE ON OBJECT::app.documents TO docchat_api",
    "GRANT SELECT, UPDATE, DELETE ON OBJECT::app.documents TO docchat_ingestion",
    "GRANT SELECT ON OBJECT::app.documents TO docchat_config_operator",
    "GRANT SELECT ON OBJECT::app.documents TO docchat_user_data_operator",
    "GRANT SELECT, INSERT ON OBJECT::app.document_versions TO docchat_api",
    (
        "GRANT UPDATE ON OBJECT::app.document_versions (state, reserved_at, "
        "uploaded_by, uploaded_via_project_id, committed_at, blob_last_modified, "
        "failure_code, failed_at, superseded_at) TO docchat_api"
    ),
    "GRANT SELECT, UPDATE, DELETE ON OBJECT::app.document_versions TO docchat_ingestion",
    "GRANT SELECT ON OBJECT::app.document_versions TO docchat_config_operator",
    (
        "GRANT UPDATE ON OBJECT::app.document_versions (state, failure_code, "
        "failed_at) TO docchat_config_operator"
    ),
    "GRANT SELECT ON OBJECT::app.document_versions TO docchat_user_data_operator",
    "GRANT SELECT, UPDATE ON OBJECT::app.chunk_sets TO docchat_api",
    "GRANT SELECT, INSERT, UPDATE ON OBJECT::app.chunk_sets TO docchat_ingestion",
    "GRANT SELECT, UPDATE ON OBJECT::app.chunk_sets TO docchat_config_operator",
    "GRANT SELECT, DELETE ON OBJECT::app.served_chunk_sets TO docchat_api",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON OBJECT::app.served_chunk_sets TO docchat_ingestion",
    "GRANT SELECT ON OBJECT::app.served_chunk_sets TO docchat_config_operator",
    "GRANT SELECT, INSERT ON OBJECT::app.ingestion_runs TO docchat_api",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON OBJECT::app.ingestion_runs TO docchat_ingestion",
    "GRANT SELECT, INSERT, UPDATE ON OBJECT::app.ingestion_runs TO docchat_config_operator",
    "GRANT SELECT ON OBJECT::app.ingestion_runs TO docchat_user_data_operator",
    "GRANT SELECT, INSERT, UPDATE ON OBJECT::app.ingestion_run_items TO docchat_api",
    (
        "GRANT SELECT, INSERT, UPDATE, DELETE ON OBJECT::app.ingestion_run_items "
        "TO docchat_ingestion"
    ),
    "GRANT SELECT, INSERT, UPDATE ON OBJECT::app.ingestion_run_items TO docchat_config_operator",
    "GRANT SELECT, UPDATE ON OBJECT::app.ingestion_dispatcher_state TO docchat_ingestion",
    "GRANT SELECT ON OBJECT::app.ingestion_dispatcher_state TO docchat_config_operator",
    "GRANT SELECT ON OBJECT::app.scan_canaries TO docchat_api",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON OBJECT::app.scan_canaries TO docchat_ingestion",
    "GRANT SELECT ON OBJECT::app.scan_canaries TO docchat_config_operator",
)

CONTENT_TYPES: Final = [
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
]

PAGEWISE_V1_SCHEMA: Final = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summarizeTables", "ocrLanguages"],
    "properties": {
        "summarizeTables": {
            "type": "boolean",
            "title": "Summarise tables",
            "description": (
                "Add a short written summary before each table so it can be found by meaning."
            ),
            "default": True,
        },
        "ocrLanguages": {
            "type": "array",
            "title": "Languages in scanned pages",
            "description": "Used to read text from scanned pages. Choose up to three.",
            "minItems": 1,
            "maxItems": 3,
            "uniqueItems": True,
            "default": ["eng"],
            "items": {
                "type": "string",
                "oneOf": [
                    {"const": "eng", "title": "English"},
                    {"const": "fra", "title": "French"},
                    {"const": "deu", "title": "German"},
                    {"const": "spa", "title": "Spanish"},
                    {"const": "ita", "title": "Italian"},
                    {"const": "nld", "title": "Dutch"},
                    {"const": "por", "title": "Portuguese"},
                ],
            },
        },
    },
}

RECURSIVE_V1_SCHEMA: Final = {
    "type": "object",
    "additionalProperties": False,
    "required": ["targetTokens", "overlapPercent", "tableMaxTokens"],
    "properties": {
        "targetTokens": {
            "type": "integer",
            "title": "Passage size (tokens)",
            "minimum": 128,
            "maximum": 2048,
            "default": 512,
        },
        "overlapPercent": {
            "type": "integer",
            "title": "Overlap between passages (%)",
            "description": (
                "Share of each passage repeated at the start of the next one in the same section."
            ),
            "minimum": 0,
            "maximum": 50,
            "multipleOf": 5,
            "default": 25,
        },
        "tableMaxTokens": {
            "type": "integer",
            "title": "Largest table kept whole (tokens)",
            "description": "Longer tables are split between rows, repeating the header rows.",
            "minimum": 256,
            "maximum": 6000,
            "default": 1600,
        },
    },
}

INGESTION_METHODS: Final = (
    {
        "method_key": "pagewise",
        "method_version": 1,
        "display_name": "Page by page",
        "description": (
            "One passage per page, with tables summarised and kept as structured "
            "tables. "
            "Uses the built-in parser."
        ),
        "parameter_schema": PAGEWISE_V1_SCHEMA,
        "default_parameters": {"summarizeTables": True, "ocrLanguages": ["eng"]},
    },
    {
        "method_key": "recursive",
        "method_version": 1,
        "display_name": "By headings and paragraphs",
        "description": (
            "Splits by headings, then paragraphs and sentences, into passages of a "
            "target size. "
            "Uses Azure AI Document Intelligence."
        ),
        "parameter_schema": RECURSIVE_V1_SCHEMA,
        "default_parameters": {"targetTokens": 512, "overlapPercent": 25, "tableMaxTokens": 1600},
    },
)

API_OPS_CONFIG: Final[tuple[tuple[str, SeedValue], ...]] = (("api.write_requests_per_minute", 120),)

CHAT_PIPELINE_OPS_CONFIG: Final[tuple[tuple[str, SeedValue], ...]] = (
    ("chat.context_token_budget", 40000),
    ("chat.answer_max_completion_tokens", 12000),
    ("chat.small_call_max_completion_tokens", 800),
    ("chat.rewrite_history_turns", 4),
    ("chat.rewrite_timeout_seconds", 8),
    ("chat.max_turn_seconds", 180),
    ("retrieval.candidate_count", 50),
    ("retrieval.keep_count", 8),
    ("retrieval.reranker_threshold", 2.0),
    ("retrieval.min_confident_results", 1),
    ("retrieval.semantic_max_wait_ms", 2000),
    ("retrieval.broaden_timeout_seconds", 8),
    ("followups.enabled", True),
    ("followups.timeout_seconds", 5),
    ("followups.max_count", 3),
    ("followups.source_count", 3),
    ("followups.source_excerpt_tokens", 300),
    ("ratelimit.chat_requests_per_minute", 3),
    ("ratelimit.max_concurrent_streams", 2),
    ("ratelimit.citation_downloads_per_minute", 30),
    ("ratelimit.max_concurrent_downloads", 2),
    ("quota.tokens_per_day", 2000000),
    ("quota.tokens_per_month", 32000000),
    ("pricing.currency", "USD"),
    ("pricing.model_rates", {}),
)

CONVERSATIONS_OPS_CONFIG: Final[tuple[tuple[str, SeedValue], ...]] = (
    ("conversations.max_turns_per_session", 500),
    ("summary.trigger_tokens", 3000),
    ("summary.keep_recent_tokens", 1200),
    ("summary.max_input_tokens", 12000),
    ("summary.max_summary_tokens", 800),
    ("summary.max_completion_tokens", 2000),
    ("summary.reasoning_effort", "low"),
    ("summary.timeout_seconds", 30),
    ("summary.lease_seconds", 120),
    ("summary.max_backoff_minutes", 60),
    ("memory.enabled", True),
    ("memory.max_facts", 50),
    ("memory.max_tokens", 1000),
    ("memory.prompt_max_tokens", 1300),
    ("memory.max_operations_per_turn", 3),
    ("memory.max_completion_tokens", 1200),
    ("memory.reasoning_effort", "low"),
    ("memory.timeout_seconds", 20),
    ("memory.lease_seconds", 60),
    ("memory.max_attempts", 3),
    ("memory.retry_window_hours", 24),
    ("feedback.comments_enabled", True),
)

INGESTION_OPS_CONFIG: Final[tuple[tuple[str, SeedValue], ...]] = (
    ("ingestion.max_parallel_items", 1),
    ("ingestion.active_wake_seconds", 60),
    ("ingestion.idle_wake_minutes", 60),
    ("ingestion.scan_wait_minutes", 240),
    ("ingestion.stale_upload_hours", 24),
    ("ingestion.orphan_pending_minutes", 15),
    ("ingestion.cleanup_retry_minutes", 15),
    ("ingestion.max_item_claims", 3),
    ("ingestion.retry_max_attempts", 7),
    ("ingestion.retry_first_delay_seconds", 15),
    ("ingestion.retry_max_delay_seconds", 480),
    ("ingestion.activity_time_budget_seconds", 1200),
    ("ingestion.max_pages_per_document", 2000),
    ("ingestion.pagewise_pages_per_extract", 40),
    ("ingestion.max_parse_seconds_per_document", 7200),
    ("ingestion.embedding_batch_max_inputs", 64),
    ("ingestion.embedding_batch_max_tokens", 60000),
    ("ingestion.embedding_concurrency", 2),
    ("ingestion.index_batch_max_documents", 100),
    ("ingestion.index_batch_max_bytes", 8000000),
    ("ingestion.table_summary_concurrency", 4),
    ("ingestion.table_summary_max_input_tokens", 6000),
    ("ingestion.table_summary_max_output_tokens", 300),
    ("ingestion.run_retention_days", 180),
    ("ingestion.durable_history_retention_days", 30),
)


def _compact(value: object) -> str:
    """Compact JSON in the given key order (the method rows)."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def canonical_value_json(value: SeedValue) -> str:
    """The canonical JSON of an ops-config value, as `docchat_core.opsconfig.encode_value` and
    `docchat-ops config set` write it, so a text comparison of two stored values is a value
    comparison (`80-data-model.md` section 2.24)."""
    return json.dumps({"value": value}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _nvarchar(name: str, value: str) -> sa.BindParameter[str]:
    return sa.bindparam(name, value, type_=sa.NVARCHAR())


# --- 1. Roles --------------------------------------------------------------------------------


def create_roles() -> None:
    # Rights belong to the roles and never to a user, so re-creating a contained user under a
    # new SID never loses a grant (ADR 0025).
    for role in ROLES:
        op.execute(f"CREATE ROLE {role} AUTHORIZATION dbo")


# --- 2 and 4. Access and configuration tables ------------------------------------------------


def create_access_tables() -> None:
    op.create_table(
        "users",
        sa.Column("object_id", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column("tenant_id", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column(
            "display_name", sa.NVARCHAR(length=256), server_default=sa.text("N''"), nullable=False
        ),
        sa.Column("email", sa.NVARCHAR(length=320), server_default=sa.text("N''"), nullable=False),
        sa.Column("last_known_role", sa.NVARCHAR(length=32), nullable=False),
        sa.Column(
            "first_seen_at",
            DATETIME2_3,
            server_default=sa.text("SYSUTCDATETIME()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            DATETIME2_3,
            server_default=sa.text("SYSUTCDATETIME()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "last_known_role IN (N'Admin', N'User')", name="CK_users_last_known_role"
        ),
        sa.CheckConstraint("last_seen_at >= first_seen_at", name="CK_users_seen_order"),
        sa.PrimaryKeyConstraint("object_id", name="PK_users", mssql_clustered=True),
        schema="app",
    )
    op.create_index(
        "IX_users_display_name", "users", ["display_name", "object_id"], unique=False, schema="app"
    )
    op.create_index("IX_users_email", "users", ["email", "object_id"], unique=False, schema="app")
    op.create_table(
        "projects",
        sa.Column("id", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column("name", sa.NVARCHAR(length=80), nullable=False),
        sa.Column(
            "description", sa.NVARCHAR(length=400), server_default=sa.text("N''"), nullable=False
        ),
        sa.Column(
            "instructions", sa.NVARCHAR(length=2000), server_default=sa.text("N''"), nullable=False
        ),
        sa.Column(
            "example_questions_json", sa.NVARCHAR(), server_default=sa.text("N'[]'"), nullable=False
        ),
        sa.Column("is_default", mssql.BIT(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "status", sa.NVARCHAR(length=32), server_default=sa.text("N'active'"), nullable=False
        ),
        sa.Column("archived_at", DATETIME2_3, nullable=True),
        sa.Column("archived_by", mssql.UNIQUEIDENTIFIER(), nullable=True),
        sa.Column(
            "created_at",
            DATETIME2_3,
            server_default=sa.text("SYSUTCDATETIME()"),
            nullable=False,
        ),
        sa.Column("created_by", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column(
            "updated_at",
            DATETIME2_3,
            server_default=sa.text("SYSUTCDATETIME()"),
            nullable=False,
        ),
        sa.Column("updated_by", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column("row_version", ROWVERSION, nullable=False),
        sa.CheckConstraint(
            (
                "(status = N'active' AND archived_at IS NULL AND archived_by IS NULL) OR "
                "(status = N'archived' AND archived_at IS NOT NULL AND archived_by IS "
                "NOT NULL)"
            ),
            name="CK_projects_archive_fields",
        ),
        sa.CheckConstraint(
            "ISJSON(example_questions_json) = 1 AND LEFT(example_questions_json, 1) = N'['",
            name="CK_projects_example_questions_json",
        ),
        sa.CheckConstraint(
            "is_default = 0 OR status = N'active'", name="CK_projects_default_active"
        ),
        sa.CheckConstraint("status IN (N'active', N'archived')", name="CK_projects_status"),
        sa.CheckConstraint("LEN(name) >= 1", name="CK_projects_name_not_empty"),
        sa.PrimaryKeyConstraint("id", name="PK_projects", mssql_clustered=True),
        sa.UniqueConstraint("name", name="UQ_projects_name"),
        schema="app",
    )
    op.create_index(
        "IX_projects_default",
        "projects",
        ["id"],
        unique=False,
        schema="app",
        mssql_where=sa.text("is_default = 1"),
    )
    op.create_index(
        "IX_projects_status_name", "projects", ["status", "name"], unique=False, schema="app"
    )
    op.create_table(
        "project_members",
        sa.Column("user_object_id", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column("project_id", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column("grant_source", sa.NVARCHAR(length=32), nullable=False),
        sa.Column("granted_by", mssql.UNIQUEIDENTIFIER(), nullable=True),
        sa.Column(
            "granted_at",
            DATETIME2_3,
            server_default=sa.text("SYSUTCDATETIME()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            (
                "(grant_source = N'default' AND granted_by IS NULL) OR (grant_source = "
                "N'admin' AND granted_by IS NOT NULL)"
            ),
            name="CK_project_members_granted_by",
        ),
        sa.CheckConstraint(
            "grant_source IN (N'default', N'admin')", name="CK_project_members_grant_source"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["app.projects.id"], name="FK_project_members_projects"
        ),
        sa.ForeignKeyConstraint(
            ["user_object_id"],
            ["app.users.object_id"],
            name="FK_project_members_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "user_object_id", "project_id", name="PK_project_members", mssql_clustered=True
        ),
        schema="app",
    )
    op.create_index(
        "IX_project_members_project",
        "project_members",
        ["project_id", "user_object_id"],
        unique=False,
        schema="app",
    )
    op.create_table(
        "ops_config",
        sa.Column("config_key", sa.NVARCHAR(length=64), nullable=False),
        sa.Column("value_json", sa.NVARCHAR(), nullable=False),
        sa.Column("default_json", sa.NVARCHAR(), nullable=False),
        sa.Column("change_reason", sa.NVARCHAR(length=500), nullable=True),
        sa.Column(
            "updated_at",
            DATETIME2_3,
            server_default=sa.text("SYSUTCDATETIME()"),
            nullable=False,
        ),
        sa.Column("updated_by", mssql.UNIQUEIDENTIFIER(), nullable=True),
        sa.Column("row_version", ROWVERSION, nullable=False),
        sa.CheckConstraint(
            (
                "config_key COLLATE Latin1_General_BIN2 NOT LIKE N'%[^a-z0-9_.]%' AND "
                "CHARINDEX(N'.', config_key) > 1 AND RIGHT(config_key, 1) <> N'.'"
            ),
            name="CK_ops_config_key",
        ),
        sa.CheckConstraint("ISJSON(default_json) = 1", name="CK_ops_config_default_json"),
        sa.CheckConstraint("ISJSON(value_json) = 1", name="CK_ops_config_value_json"),
        sa.CheckConstraint(
            "change_reason IS NULL OR LEN(change_reason) >= 10", name="CK_ops_config_reason"
        ),
        sa.PrimaryKeyConstraint("config_key", name="PK_ops_config", mssql_clustered=True),
        schema="app",
    )
    op.create_table(
        "audit_events",
        sa.Column(
            "id", sa.BigInteger(), sa.Identity(always=False, start=1, increment=1), nullable=False
        ),
        sa.Column(
            "occurred_at",
            DATETIME2_3,
            server_default=sa.text("SYSUTCDATETIME()"),
            nullable=False,
        ),
        sa.Column("actor_type", sa.NVARCHAR(length=32), nullable=False),
        sa.Column("actor_object_id", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column("subject_object_id", mssql.UNIQUEIDENTIFIER(), nullable=True),
        sa.Column("action", sa.NVARCHAR(length=64), nullable=False),
        sa.Column("target_type", sa.NVARCHAR(length=32), nullable=False),
        sa.Column("target_id", sa.NVARCHAR(length=128), nullable=True),
        sa.Column("project_id", mssql.UNIQUEIDENTIFIER(), nullable=True),
        sa.Column("metadata_json", sa.NVARCHAR(), nullable=True),
        sa.Column("correlation_id", sa.NVARCHAR(length=32), nullable=True),
        sa.CheckConstraint(
            (
                "action IN (N'user.provisioned', N'project.created', N'project.updated', "
                "N'project.archived', N'project.restored', N'project_member.added', "
                "N'project_member.removed', N'project_index.mapped', "
                "N'project_index.default_set', N'project_index.unmapped', "
                "N'ops_config.set', N'ops_config.reset', N'user_index_override.set', "
                "N'user_index_override.cleared', N'memory.paused', N'memory.resumed', "
                "N'memory.fact_updated', N'memory.fact_deleted', N'memory.cleared', "
                "N'memory.facts_extracted', N'conversation.read', N'user_data.exported', "
                "N'user_data.deleted', N'document.traces_redacted', N'index.created', "
                "N'index.updated', N'index.deleted', N'index.reingest_started', "
                "N'ingestion.reindex_started', N'document.upload_committed', "
                "N'document.malware_detected', N'document.source_fetched', "
                "N'document.deleted', N'document.reingest_requested', "
                "N'ingestion.run_submitted', N'ingestion.run_cancelled', "
                "N'ingestion.item_requeued', N'ingestion.item_failed', "
                "N'ingestion.rebuild_abandoned', N'ingestion.deployments_changed', "
                "N'ingestion.poison_drained', N'test_users.rotated', "
                "N'model_deployments.checked')"
            ),
            name="CK_audit_events_action",
        ),
        sa.CheckConstraint(
            "actor_type IN (N'user', N'operator', N'system')", name="CK_audit_events_actor_type"
        ),
        sa.CheckConstraint(
            (
                "correlation_id IS NULL OR (LEN(correlation_id) = 32 AND correlation_id "
                "COLLATE Latin1_General_BIN2 NOT LIKE N'%[^0-9a-f]%')"
            ),
            name="CK_audit_events_correlation",
        ),
        sa.CheckConstraint(
            (
                "target_type IN (N'user', N'project', N'project_member', "
                "N'project_index', N'ops_config', N'user_index_override', N'memory', "
                "N'memory_fact', N'session', N'search_index', N'document', "
                "N'document_version', N'ingestion_run', N'ingestion_run_item', "
                "N'physical_index', N'ingestion_queue', N'test_users', "
                "N'model_deployments')"
            ),
            name="CK_audit_events_target_type",
        ),
        sa.CheckConstraint(
            "metadata_json IS NULL OR (ISJSON(metadata_json) = 1 AND LEN(metadata_json) <= 4000)",
            name="CK_audit_events_metadata",
        ),
        sa.PrimaryKeyConstraint("id", name="PK_audit_events", mssql_clustered=True),
        schema="app",
    )
    op.create_index(
        "IX_audit_events_actor",
        "audit_events",
        ["actor_object_id", "occurred_at"],
        unique=False,
        schema="app",
    )
    op.create_index(
        "IX_audit_events_project",
        "audit_events",
        ["project_id", "occurred_at"],
        unique=False,
        schema="app",
        mssql_where=sa.text("project_id IS NOT NULL"),
    )
    op.create_index(
        "IX_audit_events_subject",
        "audit_events",
        ["subject_object_id", "occurred_at"],
        unique=False,
        schema="app",
        mssql_where=sa.text("subject_object_id IS NOT NULL"),
    )
    op.create_index(
        "IX_audit_events_target",
        "audit_events",
        ["target_type", "target_id", "occurred_at"],
        unique=False,
        schema="app",
    )


def create_access_tables_on_ingestion() -> None:
    op.create_table(
        "project_indexes",
        sa.Column("project_id", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column("search_index_id", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column("is_default", mssql.BIT(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            DATETIME2_3,
            server_default=sa.text("SYSUTCDATETIME()"),
            nullable=False,
        ),
        sa.Column("created_by", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column(
            "updated_at",
            DATETIME2_3,
            server_default=sa.text("SYSUTCDATETIME()"),
            nullable=False,
        ),
        sa.Column("updated_by", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"], ["app.projects.id"], name="FK_project_indexes_projects"
        ),
        sa.ForeignKeyConstraint(
            ["search_index_id"], ["app.search_indexes.id"], name="FK_project_indexes_search_indexes"
        ),
        sa.PrimaryKeyConstraint(
            "project_id", "search_index_id", name="PK_project_indexes", mssql_clustered=True
        ),
        schema="app",
    )
    op.create_index(
        "IX_project_indexes_index",
        "project_indexes",
        ["search_index_id", "project_id"],
        unique=False,
        schema="app",
    )
    op.create_index(
        "UX_project_indexes_default",
        "project_indexes",
        ["project_id"],
        unique=True,
        schema="app",
        mssql_where=sa.text("is_default = 1"),
    )
    op.create_table(
        "user_index_overrides",
        sa.Column("user_object_id", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column("project_id", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column("search_index_id", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column("reason", sa.NVARCHAR(length=500), nullable=False),
        sa.Column("set_by", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column(
            "set_at",
            DATETIME2_3,
            server_default=sa.text("SYSUTCDATETIME()"),
            nullable=False,
        ),
        sa.Column("expires_at", DATETIME2_3, nullable=False),
        sa.CheckConstraint("LEN(reason) >= 10", name="CK_user_index_overrides_reason"),
        sa.CheckConstraint(
            "expires_at > set_at AND expires_at <= DATEADD(DAY, 30, set_at)",
            name="CK_user_index_overrides_expiry",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "search_index_id"],
            ["app.project_indexes.project_id", "app.project_indexes.search_index_id"],
            name="FK_user_index_overrides_mapping",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_object_id"],
            ["app.users.object_id"],
            name="FK_user_index_overrides_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "user_object_id", "project_id", name="PK_user_index_overrides", mssql_clustered=True
        ),
        schema="app",
    )
    op.create_index(
        "IX_user_index_overrides_mapping",
        "user_index_overrides",
        ["project_id", "search_index_id"],
        unique=False,
        schema="app",
    )


# --- 3. Ingestion tables ---------------------------------------------------------------------


def create_ingestion_tables() -> None:
    op.create_table(
        "ingestion_methods",
        sa.Column("method_key", sa.NVARCHAR(length=64), nullable=False),
        sa.Column("method_version", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.NVARCHAR(length=100), nullable=False),
        sa.Column("description", sa.NVARCHAR(length=500), nullable=False),
        sa.Column("parameter_schema", sa.NVARCHAR(), nullable=False),
        sa.Column("default_parameters", sa.NVARCHAR(), nullable=False),
        sa.Column("supported_content_types", sa.NVARCHAR(length=400), nullable=False),
        sa.Column("is_active", mssql.BIT(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at",
            DATETIME2_3,
            server_default=sa.text("SYSUTCDATETIME()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "method_key COLLATE Latin1_General_BIN2 NOT LIKE N'%[^a-z0-9_]%'",
            name="CK_ingestion_methods_key",
        ),
        sa.CheckConstraint(
            "ISJSON(default_parameters) = 1", name="CK_ingestion_methods_default_parameters"
        ),
        sa.CheckConstraint(
            "ISJSON(parameter_schema) = 1", name="CK_ingestion_methods_parameter_schema"
        ),
        sa.CheckConstraint(
            "ISJSON(supported_content_types) = 1", name="CK_ingestion_methods_content_types"
        ),
        sa.CheckConstraint("method_version >= 1", name="CK_ingestion_methods_version"),
        sa.PrimaryKeyConstraint(
            "method_key", "method_version", name="PK_ingestion_methods", mssql_clustered=True
        ),
        schema="app",
    )
    op.create_table(
        "search_indexes",
        sa.Column("id", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column("display_name", sa.NVARCHAR(length=80), nullable=False),
        sa.Column("description", sa.NVARCHAR(length=1200), nullable=True),
        sa.Column("method_key", sa.NVARCHAR(length=64), nullable=False),
        sa.Column("method_version", sa.Integer(), nullable=False),
        sa.Column("method_parameters", sa.NVARCHAR(), nullable=False),
        sa.Column("method_parameter_hash", sa.BINARY(length=32), nullable=False),
        sa.Column("embedding_model", sa.NVARCHAR(length=64), nullable=False),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=False),
        sa.Column(
            "language_analyzer",
            sa.NVARCHAR(length=32),
            server_default=sa.text("N'standard.lucene'"),
            nullable=False,
        ),
        sa.Column("active_physical_index_id", mssql.UNIQUEIDENTIFIER(), nullable=True),
        sa.Column(
            "status",
            sa.NVARCHAR(length=32),
            server_default=sa.text("N'provisioning'"),
            nullable=False,
        ),
        sa.Column(
            "status_changed_at",
            DATETIME2_3,
            server_default=sa.text("SYSUTCDATETIME()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            DATETIME2_3,
            server_default=sa.text("SYSUTCDATETIME()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            DATETIME2_3,
            server_default=sa.text("SYSUTCDATETIME()"),
            nullable=False,
        ),
        sa.Column("created_by", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column("updated_by", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column("row_version", ROWVERSION, nullable=False),
        sa.CheckConstraint(
            (
                "status IN (N'provisioning', N'ready', N'reingest_required', "
                "N'reingesting', N'rebuilding', N'failed', N'deleting', N'deleted')"
            ),
            name="CK_search_indexes_status",
        ),
        sa.CheckConstraint(
            "ISJSON(method_parameters) = 1", name="CK_search_indexes_method_parameters"
        ),
        sa.CheckConstraint(
            "LEN(LTRIM(display_name)) BETWEEN 1 AND 80", name="CK_search_indexes_display_name"
        ),
        sa.CheckConstraint(
            "embedding_dimensions IN (256, 1024, 3072)", name="CK_search_indexes_dimensions"
        ),
        sa.ForeignKeyConstraint(
            ["method_key", "method_version"],
            ["app.ingestion_methods.method_key", "app.ingestion_methods.method_version"],
            name="FK_search_indexes_methods",
        ),
        sa.PrimaryKeyConstraint("id", name="PK_search_indexes", mssql_clustered=True),
        schema="app",
    )
    op.create_index(
        "IX_search_indexes_status", "search_indexes", ["status"], unique=False, schema="app"
    )
    op.create_index(
        "UX_search_indexes_display_name",
        "search_indexes",
        ["display_name"],
        unique=True,
        schema="app",
        mssql_where=sa.text("status != N'deleted'"),
    )
    op.create_table(
        "ingestion_runs",
        sa.Column("id", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column("kind", sa.NVARCHAR(length=32), nullable=False),
        sa.Column("search_index_id", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column("priority", mssql.TINYINT(), nullable=False),
        sa.Column(
            "state", sa.NVARCHAR(length=32), server_default=sa.text("N'queued'"), nullable=False
        ),
        sa.Column("requested_by", mssql.UNIQUEIDENTIFIER(), nullable=True),
        sa.Column("traceparent", sa.VARCHAR(length=55), nullable=True),
        sa.Column("tracestate", sa.VARCHAR(length=512), nullable=True),
        sa.Column("total_items", sa.Integer(), nullable=True),
        sa.Column("succeeded_items", sa.Integer(), nullable=True),
        sa.Column("skipped_items", sa.Integer(), nullable=True),
        sa.Column("failed_items", sa.Integer(), nullable=True),
        sa.Column("cancelled_items", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            DATETIME2_3,
            server_default=sa.text("SYSUTCDATETIME()"),
            nullable=False,
        ),
        sa.Column("started_at", DATETIME2_3, nullable=True),
        sa.Column("completed_at", DATETIME2_3, nullable=True),
        sa.CheckConstraint(
            (
                "kind IN (N'upload', N'document_reingest', N'document_delete', "
                "N'index_create', N'index_reingest', N'index_rebuild', N'index_delete', "
                "N'maintenance')"
            ),
            name="CK_ingestion_runs_kind",
        ),
        sa.CheckConstraint(
            "state IN (N'queued', N'running', N'completed', N'completed_with_errors', N'failed')",
            name="CK_ingestion_runs_state",
        ),
        sa.CheckConstraint("priority IN (0, 1)", name="CK_ingestion_runs_priority"),
        sa.CheckConstraint(
            "traceparent IS NULL OR LEN(traceparent) = 55", name="CK_ingestion_runs_traceparent"
        ),
        sa.ForeignKeyConstraint(
            ["search_index_id"], ["app.search_indexes.id"], name="FK_ingestion_runs_search_indexes"
        ),
        sa.PrimaryKeyConstraint("id", name="PK_ingestion_runs", mssql_clustered=True),
        schema="app",
    )
    op.create_index(
        "IX_ingestion_runs_created",
        "ingestion_runs",
        [sa.literal_column("created_at DESC"), sa.literal_column("id DESC")],
        unique=False,
        schema="app",
    )
    op.create_index(
        "IX_ingestion_runs_index_created",
        "ingestion_runs",
        ["search_index_id", sa.literal_column("created_at DESC"), sa.literal_column("id DESC")],
        unique=False,
        schema="app",
    )
    op.create_index(
        "IX_ingestion_runs_open",
        "ingestion_runs",
        ["state"],
        unique=False,
        schema="app",
        mssql_where=sa.text("state IN (N'queued', N'running')"),
    )
    op.create_table(
        "physical_indexes",
        sa.Column("id", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column("search_index_id", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("name", sa.NVARCHAR(length=128), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("embedding_model", sa.NVARCHAR(length=64), nullable=False),
        sa.Column("embedding_model_version", sa.NVARCHAR(length=32), nullable=False),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=False),
        sa.Column("embedding_encoding", sa.NVARCHAR(length=32), nullable=False),
        sa.Column("embedding_max_input_tokens", sa.Integer(), nullable=False),
        sa.Column("language_analyzer", sa.NVARCHAR(length=32), nullable=False),
        sa.Column("query_embedding_deployment", sa.NVARCHAR(length=64), nullable=False),
        sa.Column("ingestion_embedding_deployment", sa.NVARCHAR(length=64), nullable=False),
        sa.Column(
            "state", sa.NVARCHAR(length=32), server_default=sa.text("N'building'"), nullable=False
        ),
        sa.Column("build_run_id", mssql.UNIQUEIDENTIFIER(), nullable=True),
        sa.Column("provisioned_at", DATETIME2_3, nullable=True),
        sa.Column(
            "created_at",
            DATETIME2_3,
            server_default=sa.text("SYSUTCDATETIME()"),
            nullable=False,
        ),
        sa.Column("activated_at", DATETIME2_3, nullable=True),
        sa.Column("retiring_at", DATETIME2_3, nullable=True),
        sa.Column("delete_after", DATETIME2_3, nullable=True),
        sa.Column("deleted_at", DATETIME2_3, nullable=True),
        sa.CheckConstraint(
            "state IN (N'building', N'active', N'retiring', N'deleted')",
            name="CK_physical_indexes_state",
        ),
        sa.CheckConstraint(
            "embedding_dimensions IN (256, 1024, 3072)", name="CK_physical_indexes_dimensions"
        ),
        sa.CheckConstraint("generation >= 1", name="CK_physical_indexes_generation"),
        sa.CheckConstraint("schema_version >= 1", name="CK_physical_indexes_schema_version"),
        sa.ForeignKeyConstraint(
            ["build_run_id"], ["app.ingestion_runs.id"], name="FK_physical_indexes_build_run"
        ),
        sa.ForeignKeyConstraint(
            ["search_index_id"],
            ["app.search_indexes.id"],
            name="FK_physical_indexes_search_indexes",
        ),
        sa.PrimaryKeyConstraint("id", name="PK_physical_indexes", mssql_clustered=True),
        schema="app",
    )
    op.create_index(
        "IX_physical_indexes_retiring",
        "physical_indexes",
        ["state", "delete_after"],
        unique=False,
        schema="app",
        mssql_where=sa.text("state = N'retiring'"),
    )
    op.create_index(
        "UX_physical_indexes_generation",
        "physical_indexes",
        ["search_index_id", "generation"],
        unique=True,
        schema="app",
    )
    op.create_index(
        "UX_physical_indexes_name", "physical_indexes", ["name"], unique=True, schema="app"
    )
    op.create_index(
        "UX_physical_indexes_one_active",
        "physical_indexes",
        ["search_index_id"],
        unique=True,
        schema="app",
        mssql_where=sa.text("state = N'active'"),
    )
    op.create_index(
        "UX_physical_indexes_one_building",
        "physical_indexes",
        ["search_index_id"],
        unique=True,
        schema="app",
        mssql_where=sa.text("state = N'building'"),
    )
    op.create_foreign_key(
        "FK_search_indexes_active_physical",
        "search_indexes",
        "physical_indexes",
        ["active_physical_index_id"],
        ["id"],
        source_schema="app",
        referent_schema="app",
    )
    op.create_table(
        "documents",
        sa.Column("id", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column("search_index_id", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column("source_name", sa.NVARCHAR(length=255), nullable=False),
        sa.Column("normalized_name", sa.NVARCHAR(length=255), nullable=False),
        sa.Column("current_version_id", mssql.UNIQUEIDENTIFIER(), nullable=True),
        sa.Column("next_version_number", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("is_deleted", mssql.BIT(), server_default=sa.text("0"), nullable=False),
        sa.Column("deleted_at", DATETIME2_3, nullable=True),
        sa.Column("deleted_by", mssql.UNIQUEIDENTIFIER(), nullable=True),
        sa.Column(
            "created_at",
            DATETIME2_3,
            server_default=sa.text("SYSUTCDATETIME()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            DATETIME2_3,
            server_default=sa.text("SYSUTCDATETIME()"),
            nullable=False,
        ),
        sa.Column("created_by", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column("updated_by", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column("row_version", ROWVERSION, nullable=False),
        sa.CheckConstraint(
            "is_deleted = 0 OR deleted_at IS NOT NULL", name="CK_documents_deleted_at"
        ),
        sa.CheckConstraint("next_version_number >= 1", name="CK_documents_next_version_number"),
        sa.ForeignKeyConstraint(
            ["search_index_id"], ["app.search_indexes.id"], name="FK_documents_search_indexes"
        ),
        sa.PrimaryKeyConstraint("id", name="PK_documents", mssql_clustered=True),
        sa.UniqueConstraint("id", "search_index_id", name="UQ_documents_id_index"),
        sa.UniqueConstraint("search_index_id", "normalized_name", name="UQ_documents_index_name"),
        schema="app",
    )
    op.create_index(
        "IX_documents_index_live",
        "documents",
        ["search_index_id", "is_deleted"],
        unique=False,
        schema="app",
        mssql_include=["current_version_id", "normalized_name"],
    )
    op.create_table(
        "document_versions",
        sa.Column("id", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column("document_id", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column("search_index_id", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.BINARY(length=32), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("content_type", sa.NVARCHAR(length=100), nullable=False),
        sa.Column("source_name", sa.NVARCHAR(length=255), nullable=False),
        sa.Column("blob_path", sa.NVARCHAR(length=200), nullable=False),
        sa.Column("blob_last_modified", DATETIME2_3, nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("uploaded_by", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column("uploaded_via_project_id", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column(
            "state", sa.NVARCHAR(length=32), server_default=sa.text("N'uploading'"), nullable=False
        ),
        sa.Column("scan_required", mssql.BIT(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "scan_status",
            sa.NVARCHAR(length=32),
            server_default=sa.text("N'not_required'"),
            nullable=False,
        ),
        sa.Column("scan_raw_result", sa.NVARCHAR(length=256), nullable=True),
        sa.Column("scan_time", DATETIME2_3, nullable=True),
        sa.Column("converter_version", sa.NVARCHAR(length=64), nullable=True),
        sa.Column("converted_sha256", sa.BINARY(length=32), nullable=True),
        sa.Column("document_pdf_status", sa.NVARCHAR(length=16), nullable=True),
        sa.Column("failure_code", sa.NVARCHAR(length=64), nullable=True),
        sa.Column(
            "reserved_at",
            DATETIME2_3,
            server_default=sa.text("SYSUTCDATETIME()"),
            nullable=False,
        ),
        sa.Column("committed_at", DATETIME2_3, nullable=True),
        sa.Column("ingest_started_at", DATETIME2_3, nullable=True),
        sa.Column("ingested_at", DATETIME2_3, nullable=True),
        sa.Column("superseded_at", DATETIME2_3, nullable=True),
        sa.Column("failed_at", DATETIME2_3, nullable=True),
        sa.Column("storage_purged_at", DATETIME2_3, nullable=True),
        sa.Column(
            "created_at",
            DATETIME2_3,
            server_default=sa.text("SYSUTCDATETIME()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            (
                "content_type IN (N'application/pdf', "
                "N'application/vnd.openxmlformats-officedocument.wordprocessingml.document', "
                "N'application/vnd.openxmlformats-officedocument.presentationml.presentation')"
            ),
            name="CK_document_versions_content_type",
        ),
        sa.CheckConstraint(
            "converted_sha256 IS NULL OR content_type <> N'application/pdf'",
            name="CK_document_versions_converted",
        ),
        sa.CheckConstraint(
            "document_pdf_status IS NULL OR document_pdf_status IN (N'built', N'too_large')",
            name="CK_document_versions_document_pdf",
        ),
        sa.CheckConstraint(
            (
                "scan_status IN (N'not_required', N'awaiting', N'clean', N'malicious', "
                "N'failed', N'unavailable')"
            ),
            name="CK_document_versions_scan_status",
        ),
        sa.CheckConstraint(
            "state = N'uploading' OR committed_at IS NOT NULL OR state = N'failed'",
            name="CK_document_versions_committed",
        ),
        sa.CheckConstraint(
            (
                "state IN (N'uploading', N'pending', N'superseded', N'ingesting', "
                "N'ingested', N'failed')"
            ),
            name="CK_document_versions_state",
        ),
        sa.CheckConstraint("size_bytes > 0", name="CK_document_versions_size"),
        sa.CheckConstraint("version_number >= 1", name="CK_document_versions_version_number"),
        sa.ForeignKeyConstraint(
            ["document_id", "search_index_id"],
            ["app.documents.id", "app.documents.search_index_id"],
            name="FK_document_versions_documents",
        ),
        sa.ForeignKeyConstraint(
            ["search_index_id"],
            ["app.search_indexes.id"],
            name="FK_document_versions_search_indexes",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_via_project_id"], ["app.projects.id"], name="FK_document_versions_projects"
        ),
        sa.PrimaryKeyConstraint("id", name="PK_document_versions", mssql_clustered=True),
        sa.UniqueConstraint("document_id", "version_number", name="UQ_document_versions_number"),
        schema="app",
    )
    op.create_index(
        "IX_document_versions_document_state",
        "document_versions",
        ["document_id", "state"],
        unique=False,
        schema="app",
        mssql_include=["committed_at", "version_number", "sha256"],
    )
    op.create_index(
        "IX_document_versions_pending",
        "document_versions",
        ["state", "committed_at"],
        unique=False,
        schema="app",
        mssql_include=["document_id", "search_index_id", "scan_required", "scan_status"],
        mssql_where=sa.text("state = N'pending'"),
    )
    op.create_index(
        "IX_document_versions_unpurged",
        "document_versions",
        ["state", "superseded_at"],
        unique=False,
        schema="app",
        mssql_include=["document_id", "failed_at"],
        mssql_where=sa.text("storage_purged_at IS NULL"),
    )
    op.create_index(
        "IX_document_versions_uploading",
        "document_versions",
        ["reserved_at"],
        unique=False,
        schema="app",
        mssql_where=sa.text("state = N'uploading'"),
    )
    op.create_index(
        "UX_document_versions_document_sha",
        "document_versions",
        ["document_id", "sha256"],
        unique=True,
        schema="app",
        mssql_where=sa.text("state IN (N'pending', N'ingesting', N'ingested')"),
    )
    op.create_index(
        "UX_document_versions_index_sha",
        "document_versions",
        ["search_index_id", "sha256"],
        unique=True,
        schema="app",
        mssql_where=sa.text("state IN (N'pending', N'ingesting', N'ingested')"),
    )
    op.create_index(
        "UX_document_versions_one_ingested",
        "document_versions",
        ["document_id"],
        unique=True,
        schema="app",
        mssql_where=sa.text("state = N'ingested'"),
    )
    op.execute(SCAN_INSERT_TRIGGER)
    op.create_foreign_key(
        "FK_documents_current_version",
        "documents",
        "document_versions",
        ["current_version_id"],
        ["id"],
        source_schema="app",
        referent_schema="app",
    )
    op.create_table(
        "chunk_sets",
        sa.Column("id", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column("document_id", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column("document_version_id", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column("physical_index_id", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column("method_key", sa.NVARCHAR(length=64), nullable=False),
        sa.Column("method_version", sa.Integer(), nullable=False),
        sa.Column("parameter_hash", sa.BINARY(length=32), nullable=False),
        sa.Column("method_parameters", sa.NVARCHAR(), nullable=False),
        sa.Column("embedding_model", sa.NVARCHAR(length=64), nullable=False),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=True),
        sa.Column("parse_seconds_used", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("pdf_sha256", sa.BINARY(length=32), nullable=True),
        sa.Column(
            "state", sa.NVARCHAR(length=32), server_default=sa.text("N'building'"), nullable=False
        ),
        sa.Column("rendition_rules_version", sa.Integer(), nullable=True),
        sa.Column("build_item_id", mssql.UNIQUEIDENTIFIER(), nullable=True),
        sa.Column(
            "created_at",
            DATETIME2_3,
            server_default=sa.text("SYSUTCDATETIME()"),
            nullable=False,
        ),
        sa.Column("ready_at", DATETIME2_3, nullable=True),
        sa.Column("served_at", DATETIME2_3, nullable=True),
        sa.Column("retired_at", DATETIME2_3, nullable=True),
        sa.Column("chunks_deleted_at", DATETIME2_3, nullable=True),
        sa.CheckConstraint(
            "state IN (N'building', N'ready', N'served', N'retired')", name="CK_chunk_sets_state"
        ),
        sa.CheckConstraint("ISJSON(method_parameters) = 1", name="CK_chunk_sets_method_parameters"),
        sa.CheckConstraint(
            "chunk_count IS NULL OR chunk_count BETWEEN 1 AND 100000",
            name="CK_chunk_sets_chunk_count",
        ),
        sa.CheckConstraint("parse_seconds_used >= 0", name="CK_chunk_sets_parse_seconds"),
        sa.ForeignKeyConstraint(
            ["document_id"], ["app.documents.id"], name="FK_chunk_sets_documents"
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["app.document_versions.id"],
            name="FK_chunk_sets_document_versions",
        ),
        sa.ForeignKeyConstraint(
            ["method_key", "method_version"],
            ["app.ingestion_methods.method_key", "app.ingestion_methods.method_version"],
            name="FK_chunk_sets_methods",
        ),
        sa.ForeignKeyConstraint(
            ["physical_index_id"],
            ["app.physical_indexes.id"],
            name="FK_chunk_sets_physical_indexes",
        ),
        sa.PrimaryKeyConstraint("id", name="PK_chunk_sets", mssql_clustered=True),
        schema="app",
    )
    op.create_index(
        "IX_chunk_sets_cleanup",
        "chunk_sets",
        ["retired_at"],
        unique=False,
        schema="app",
        mssql_include=["document_id", "physical_index_id", "chunk_count"],
        mssql_where=sa.text("state = N'retired' AND chunks_deleted_at IS NULL"),
    )
    op.create_index(
        "IX_chunk_sets_document",
        "chunk_sets",
        ["document_id", "physical_index_id", "state"],
        unique=False,
        schema="app",
    )
    op.create_index(
        "IX_chunk_sets_served_rules",
        "chunk_sets",
        ["rendition_rules_version", "served_at"],
        unique=False,
        schema="app",
        mssql_include=["document_id", "document_version_id", "physical_index_id"],
        mssql_where=sa.text("state = N'served'"),
    )
    op.create_index(
        "UX_chunk_sets_build_key",
        "chunk_sets",
        [
            "document_version_id",
            "physical_index_id",
            "method_key",
            "method_version",
            "parameter_hash",
        ],
        unique=True,
        schema="app",
        mssql_where=sa.text("state IN (N'building', N'ready', N'served')"),
    )
    op.create_table(
        "served_chunk_sets",
        sa.Column("document_id", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column("physical_index_id", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column("chunk_set_id", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column("document_version_id", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column(
            "served_at",
            DATETIME2_3,
            server_default=sa.text("SYSUTCDATETIME()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["chunk_set_id"], ["app.chunk_sets.id"], name="FK_served_chunk_sets_chunk_sets"
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["app.documents.id"], name="FK_served_chunk_sets_documents"
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["app.document_versions.id"],
            name="FK_served_chunk_sets_document_versions",
        ),
        sa.ForeignKeyConstraint(
            ["physical_index_id"],
            ["app.physical_indexes.id"],
            name="FK_served_chunk_sets_physical_indexes",
        ),
        sa.PrimaryKeyConstraint(
            "document_id", "physical_index_id", name="PK_served_chunk_sets", mssql_clustered=True
        ),
        sa.UniqueConstraint("chunk_set_id", name="UQ_served_chunk_sets_chunk_set"),
        schema="app",
    )
    op.create_index(
        "IX_served_chunk_sets_physical",
        "served_chunk_sets",
        ["physical_index_id", "document_id"],
        unique=False,
        schema="app",
        mssql_include=["document_version_id", "chunk_set_id"],
    )
    op.create_table(
        "ingestion_run_items",
        sa.Column("id", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column("run_id", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column(
            "queue_seq",
            sa.BigInteger(),
            sa.Identity(always=False, start=1, increment=1),
            nullable=False,
        ),
        sa.Column("operation", sa.NVARCHAR(length=32), nullable=False),
        sa.Column("search_index_id", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column("physical_index_id", mssql.UNIQUEIDENTIFIER(), nullable=True),
        sa.Column("document_id", mssql.UNIQUEIDENTIFIER(), nullable=True),
        sa.Column("document_version_id", mssql.UNIQUEIDENTIFIER(), nullable=True),
        sa.Column("priority", mssql.TINYINT(), nullable=False),
        sa.Column(
            "state", sa.NVARCHAR(length=32), server_default=sa.text("N'queued'"), nullable=False
        ),
        sa.Column("stage", sa.NVARCHAR(length=32), nullable=True),
        sa.Column("claim_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("instance_id", sa.NVARCHAR(length=100), nullable=True),
        sa.Column("lease_expires_at", DATETIME2_3, nullable=True),
        sa.Column("not_before", DATETIME2_3, nullable=True),
        sa.Column("chunk_set_id", mssql.UNIQUEIDENTIFIER(), nullable=True),
        sa.Column("outcome_code", sa.NVARCHAR(length=64), nullable=True),
        sa.Column("outcome_message", sa.NVARCHAR(length=400), nullable=True),
        sa.Column("warnings", sa.NVARCHAR(length=4000), nullable=True),
        sa.Column("embedding_tokens", sa.Integer(), nullable=True),
        sa.Column("summary_input_tokens", sa.Integer(), nullable=True),
        sa.Column("summary_output_tokens", sa.Integer(), nullable=True),
        sa.Column("docintel_pages", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            DATETIME2_3,
            server_default=sa.text("SYSUTCDATETIME()"),
            nullable=False,
        ),
        sa.Column("started_at", DATETIME2_3, nullable=True),
        sa.Column("completed_at", DATETIME2_3, nullable=True),
        sa.CheckConstraint(
            (
                "(operation = N'ingest' AND document_id IS NOT NULL AND "
                "document_version_id IS NOT NULL AND physical_index_id IS NOT NULL) OR "
                "(operation = N'cleanup_document' AND document_id IS NOT NULL AND "
                "document_version_id IS NULL) OR (operation = N'rerender' AND "
                "document_id IS NOT NULL AND document_version_id IS NOT NULL AND "
                "physical_index_id IS NOT NULL AND chunk_set_id IS NOT NULL) OR "
                "(operation IN (N'create_physical_index', N'delete_physical_index') AND "
                "physical_index_id IS NOT NULL AND document_id IS NULL) OR (operation = "
                "N'purge_index_blobs' AND document_id IS NULL AND physical_index_id IS "
                "NULL)"
            ),
            name="CK_run_items_shape",
        ),
        sa.CheckConstraint(
            (
                "operation IN (N'ingest', N'cleanup_document', N'rerender', "
                "N'create_physical_index', N'delete_physical_index', "
                "N'purge_index_blobs')"
            ),
            name="CK_run_items_operation",
        ),
        sa.CheckConstraint(
            (
                "stage IS NULL OR stage IN (N'validate', N'extract', N'enrich', "
                "N'chunk', N'embed', N'index', N'render', N'flip', N'clean_up', "
                "N'purge_document', N'create_index', N'delete_index', N'purge_blobs')"
            ),
            name="CK_run_items_stage",
        ),
        sa.CheckConstraint(
            "state IN (N'queued', N'running', N'succeeded', N'skipped', N'failed', N'cancelled')",
            name="CK_run_items_state",
        ),
        sa.CheckConstraint(
            "warnings IS NULL OR ISJSON(warnings) = 1", name="CK_run_items_warnings"
        ),
        sa.ForeignKeyConstraint(
            ["chunk_set_id"], ["app.chunk_sets.id"], name="FK_run_items_chunk_sets"
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["app.documents.id"], name="FK_run_items_documents"
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["app.document_versions.id"],
            name="FK_run_items_document_versions",
        ),
        sa.ForeignKeyConstraint(
            ["physical_index_id"], ["app.physical_indexes.id"], name="FK_run_items_physical_indexes"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["app.ingestion_runs.id"], name="FK_run_items_runs"),
        sa.ForeignKeyConstraint(
            ["search_index_id"], ["app.search_indexes.id"], name="FK_run_items_search_indexes"
        ),
        sa.PrimaryKeyConstraint("id", name="PK_ingestion_run_items", mssql_clustered=True),
        sa.UniqueConstraint("queue_seq", name="UQ_run_items_queue_seq"),
        schema="app",
    )
    op.create_index(
        "IX_run_items_claim",
        "ingestion_run_items",
        ["priority", "queue_seq"],
        unique=False,
        schema="app",
        mssql_include=[
            "document_id",
            "document_version_id",
            "physical_index_id",
            "operation",
            "not_before",
            "created_at",
        ],
        mssql_where=sa.text("state = N'queued'"),
    )
    op.create_index(
        "IX_run_items_document",
        "ingestion_run_items",
        ["document_id", sa.literal_column("created_at DESC")],
        unique=False,
        schema="app",
    )
    op.create_index(
        "IX_run_items_run", "ingestion_run_items", ["run_id", "state"], unique=False, schema="app"
    )
    op.create_index(
        "IX_run_items_running",
        "ingestion_run_items",
        ["lease_expires_at"],
        unique=False,
        schema="app",
        mssql_include=["document_id", "physical_index_id"],
        mssql_where=sa.text("state = N'running'"),
    )
    op.create_index(
        "UX_run_items_run_version",
        "ingestion_run_items",
        ["run_id", "document_version_id"],
        unique=True,
        schema="app",
        mssql_where=sa.text("document_version_id IS NOT NULL"),
    )
    op.create_index(
        "UX_run_items_running_target",
        "ingestion_run_items",
        ["document_id", "physical_index_id"],
        unique=True,
        schema="app",
        mssql_where=sa.text("state = N'running'"),
    )
    op.create_table(
        "ingestion_dispatcher_state",
        # Not an identity: the single row is id 1, inserted below (80-data-model.md section 2.18).
        sa.Column("id", mssql.TINYINT(), autoincrement=False, nullable=False),
        sa.Column("generation", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "last_sweep_at",
            DATETIME2_3,
            server_default=sa.text("SYSUTCDATETIME()"),
            nullable=False,
        ),
        sa.Column("last_claim_at", DATETIME2_3, nullable=True),
        sa.Column("running_items", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "updated_at",
            DATETIME2_3,
            server_default=sa.text("SYSUTCDATETIME()"),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name="CK_ingestion_dispatcher_state_id"),
        sa.PrimaryKeyConstraint("id", name="PK_ingestion_dispatcher_state", mssql_clustered=True),
        schema="app",
    )
    op.create_table(
        "scan_canaries",
        sa.Column("id", mssql.UNIQUEIDENTIFIER(), nullable=False),
        sa.Column("blob_path", sa.NVARCHAR(length=256), nullable=False),
        sa.Column(
            "written_at",
            DATETIME2_3,
            server_default=sa.text("SYSUTCDATETIME()"),
            nullable=False,
        ),
        sa.Column("result", sa.NVARCHAR(length=32), nullable=True),
        sa.Column("scanned_at", DATETIME2_3, nullable=True),
        sa.Column("minutes_to_result", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "result IS NULL OR result IN (N'clean', N'malicious', N'failed', N'unavailable')",
            name="CK_scan_canaries_result",
        ),
        sa.CheckConstraint(
            "minutes_to_result IS NULL OR minutes_to_result >= 0", name="CK_scan_canaries_minutes"
        ),
        sa.PrimaryKeyConstraint("id", name="PK_scan_canaries", mssql_clustered=True),
        schema="app",
    )
    op.create_index(
        "IX_scan_canaries_written_at",
        "scan_canaries",
        ["written_at"],
        unique=False,
        schema="app",
        mssql_include=["result"],
    )


# --- 5. Grants -------------------------------------------------------------------------------


def grant_access() -> None:
    for statement in ACCESS_GRANTS:
        op.execute(statement)


def grant_ingestion() -> None:
    for statement in INGESTION_GRANTS:
        op.execute(statement)


# --- 6. Seed rows ----------------------------------------------------------------------------


def seed_ingestion() -> None:
    op.execute("INSERT INTO app.ingestion_dispatcher_state (id) VALUES (1)")
    for method in INGESTION_METHODS:
        op.execute(
            sa.text(
                "INSERT INTO app.ingestion_methods (method_key, method_version, "
                "display_name, "
                "description, parameter_schema, default_parameters, "
                "supported_content_types) "
                "VALUES (:key, :version, :display_name, :description, :schema, "
                ":defaults, :types)"
            ).bindparams(
                _nvarchar("key", str(method["method_key"])),
                sa.bindparam("version", method["method_version"], type_=sa.Integer()),
                _nvarchar("display_name", str(method["display_name"])),
                _nvarchar("description", str(method["description"])),
                _nvarchar("schema", _compact(method["parameter_schema"])),
                _nvarchar("defaults", _compact(method["default_parameters"])),
                _nvarchar("types", _compact(CONTENT_TYPES)),
            )
        )


def seed_ops_config(rows: tuple[tuple[str, SeedValue], ...]) -> None:
    for key, value in rows:
        op.execute(
            sa.text(
                "INSERT INTO app.ops_config (config_key, value_json, default_json) "
                "VALUES (:key, :json, :json)"
            ).bindparams(_nvarchar("key", key), _nvarchar("json", canonical_value_json(value)))
        )


def create_schema() -> None:
    """Schema `app` holds every application table (80-data-model.md section 2.1 item 2); an
    empty database has none, and `upgrade head` creates it (that file's acceptance criterion 1)."""
    op.execute("CREATE SCHEMA app AUTHORIZATION dbo")


def upgrade() -> None:
    create_schema()
    create_roles()
    create_access_tables()
    create_ingestion_tables()
    create_access_tables_on_ingestion()
    grant_access()
    grant_ingestion()
    seed_ingestion()
    # One call per owning area: the API conventions, the chat pipeline, the conversation store
    # and ingestion (`81-config.md` section 3.2).
    seed_ops_config(API_OPS_CONFIG)
    seed_ops_config(CHAT_PIPELINE_OPS_CONFIG)
    seed_ops_config(CONVERSATIONS_OPS_CONFIG)
    seed_ops_config(INGESTION_OPS_CONFIG)


def downgrade() -> None:
    raise NotImplementedError("roll forward")
