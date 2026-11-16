"""The audit-action and target-type catalogues (`80-data-model.md` section 2.21; ADR 0029).

These are the one list: `CK_audit_events_action` and `CK_audit_events_target_type` in
`docchat_core.sql.tables.access` are built from the tuples here, and the tuples are read from the
`Literal` types, so the type checker, the constraint and the metadata models agree. A new action
or target type is added here, to its metadata model and, in an expand revision, to both
constraints.
"""

from __future__ import annotations

from typing import Final, Literal, get_args

type AuditActorType = Literal["user", "operator", "system"]

AUDIT_ACTOR_TYPES: Final[tuple[str, ...]] = get_args(AuditActorType.__value__)

type AuditAction = Literal[
    # Access and configuration (14).
    "user.provisioned",
    "project.created",
    "project.updated",
    "project.archived",
    "project.restored",
    "project_member.added",
    "project_member.removed",
    "project_index.mapped",
    "project_index.default_set",
    "project_index.unmapped",
    "ops_config.set",
    "ops_config.reset",
    "user_index_override.set",
    "user_index_override.cleared",
    # Conversations, memory and user data (10).
    "memory.paused",
    "memory.resumed",
    "memory.fact_updated",
    "memory.fact_deleted",
    "memory.cleared",
    "memory.facts_extracted",
    "conversation.read",
    "user_data.exported",
    "user_data.deleted",
    "document.traces_redacted",
    # Ingestion (17).
    "index.created",
    "index.updated",
    "index.deleted",
    "index.reingest_started",
    "ingestion.reindex_started",
    "document.upload_committed",
    "document.malware_detected",
    "document.source_fetched",
    "document.deleted",
    "document.reingest_requested",
    "ingestion.run_submitted",
    "ingestion.run_cancelled",
    "ingestion.item_requeued",
    "ingestion.item_failed",
    "ingestion.rebuild_abandoned",
    "ingestion.deployments_changed",
    "ingestion.poison_drained",
    # Quality (2).
    "test_users.rotated",
    "model_deployments.checked",
]

AUDIT_ACTIONS: Final[tuple[str, ...]] = get_args(AuditAction.__value__)
"""The 43 actions, in catalogue order."""

type AuditTargetType = Literal[
    "user",
    "project",
    "project_member",
    "project_index",
    "ops_config",
    "user_index_override",
    "memory",
    "memory_fact",
    "session",
    "search_index",
    "document",
    "document_version",
    "ingestion_run",
    "ingestion_run_item",
    "physical_index",
    "ingestion_queue",
    "test_users",
    "model_deployments",
]

AUDIT_TARGET_TYPES: Final[tuple[str, ...]] = get_args(AuditTargetType.__value__)
"""The 18 target types, in catalogue order."""

ACTIONS_WITHOUT_METADATA: Final = frozenset({"project.restored", "memory.paused", "memory.resumed"})
"""The three actions whose `metadata_json` is `NULL`."""

ACTIONS_WITHOUT_TARGET_ID: Final = frozenset({"test_users.rotated", "model_deployments.checked"})
"""The two actions whose `target_id` is `NULL`."""
