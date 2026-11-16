"""The audit trail (`80-data-model.md` sections 2.8 and 2.21; `82-security.md` SEC-84;
ADR 0029): the action and target-type catalogues, `AuditEvent` with one metadata model per
action, `write_audit_event` and `write_audit_event_after_commit`."""

from docchat_core.audit.catalogue import (
    ACTIONS_WITHOUT_METADATA,
    ACTIONS_WITHOUT_TARGET_ID,
    AUDIT_ACTIONS,
    AUDIT_ACTOR_TYPES,
    AUDIT_TARGET_TYPES,
    AuditAction,
    AuditActorType,
    AuditTargetType,
)
from docchat_core.audit.models import (
    METADATA_MAX_CHARACTERS,
    METADATA_MODELS,
    AuditEvent,
    AuditMetadata,
    value_sha256,
)
from docchat_core.audit.writer import (
    AFTER_COMMIT_RETRY_DELAYS,
    audit_row,
    current_correlation_id,
    write_audit_event,
    write_audit_event_after_commit,
)

__all__ = [
    "ACTIONS_WITHOUT_METADATA",
    "ACTIONS_WITHOUT_TARGET_ID",
    "AFTER_COMMIT_RETRY_DELAYS",
    "AUDIT_ACTIONS",
    "AUDIT_ACTOR_TYPES",
    "AUDIT_TARGET_TYPES",
    "METADATA_MAX_CHARACTERS",
    "METADATA_MODELS",
    "AuditAction",
    "AuditActorType",
    "AuditEvent",
    "AuditMetadata",
    "AuditTargetType",
    "audit_row",
    "current_correlation_id",
    "value_sha256",
    "write_audit_event",
    "write_audit_event_after_commit",
]
