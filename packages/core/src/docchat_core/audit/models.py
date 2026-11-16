"""`AuditEvent` and one metadata model per action (`80-data-model.md` section 2.21 rules 3 to 7;
ADR 0029 item 4).

Every metadata model forbids unknown keys and uses camelCase names, and every field is an
identifier, a count, a code, a field name, an enum value, a boolean, a time or an ops-config value;
no field can carry a name, a title, a question, a fact or a file name. Every list has a maximum,
so every row fits `CK_audit_events_metadata`'s 4,000 characters.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated, Any, ClassVar, Final, Literal, Self
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    model_serializer,
    model_validator,
)
from pydantic.alias_generators import to_camel
from pydantic_core.core_schema import SerializerFunctionWrapHandler

from docchat_core.audit.catalogue import (
    ACTIONS_WITHOUT_METADATA,
    ACTIONS_WITHOUT_TARGET_ID,
    AuditAction,
    AuditActorType,
    AuditTargetType,
)
from docchat_core.models import (
    LOWER_CASE_UUID_PATTERN,
    CanonicalUuid,
    Count,
    Sha256Hex,
    UtcDateTime,
)

METADATA_MAX_CHARACTERS: Final = 4000
TICKET_PATTERN: Final = r"^[A-Za-z0-9_#-]{1,64}$"
POISON_QUEUE: Final = "ingestion-requests-poison"

Ticket = Annotated[str, StringConstraints(pattern=TICKET_PATTERN)]
"""The `--ticket` value of `docchat-ops`: an identifier, never free text."""

AtLeastOne = Annotated[int, Field(ge=1, le=2_147_483_647)]
HttpStatus = Annotated[int, Field(ge=0, le=599)]
OpsConfigArea = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
MethodKey = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,64}$")]
ModelName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9.\-]{0,63}$")]
# Three allowlisted analyzers carry upper-case letters (`pt-Pt.microsoft`, `pt-Br.microsoft`,
# `zh-Hans.microsoft`), so the pattern admits them.
AnalyzerName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z.\-]{0,31}$")]
DeploymentName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.\-]{1,63}$")]

MemoryCategory = Literal["role", "team", "expertise", "topic", "answer_style"]
MemorySourceKind = Literal["user", "extracted"]
ReingestMode = Literal["same_index", "new_physical_index"]
IndexStatus = Literal[
    "provisioning",
    "ready",
    "reingest_required",
    "reingesting",
    "rebuilding",
    "failed",
    "deleting",
    "deleted",
]
ContentType = Literal[
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
]
ProjectField = Literal["name", "description", "instructions", "exampleQuestions", "isDefault"]
IndexField = Literal[
    "displayName",
    "description",
    "methodKey",
    "methodVersion",
    "methodParameters",
    "embeddingModel",
    "embeddingDimensions",
    "languageAnalyzer",
]
ModelCheckCall = Literal[
    "effort",
    "standalone_query",
    "search_passage",
    "followup_questions",
    "memory_extraction",
    "tokenizer",
    "summary_latency",
    "memory_latency",
    "shield_request",
    "shield_empty_user_prompt",
]
ReasoningEffort = Literal["none", "low", "medium", "high"]


def _unique[T](values: list[T]) -> list[T]:
    if len(set(values)) != len(values):
        raise ValueError("values must be unique")
    return values


def _http_status(value: int) -> int:
    if value != 0 and value < 100:  # noqa: PLR2004 - 0 marks a non-HTTP failure
        raise ValueError("an HTTP status from 100 to 599, or 0")
    return value


Status = Annotated[HttpStatus, AfterValidator(_http_status)]


def value_sha256(value: JsonValue) -> str:
    """The SHA-256 of an ops-config value's compact JSON with sorted keys (rule 7)."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class AuditMetadata(BaseModel):
    """Base of every metadata model: closed, camelCase, immutable."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, alias_generator=to_camel, populate_by_name=True
    )

    def to_json(self) -> str:
        """The compact JSON of `metadata_json`; a `None` field is written as `null`."""
        return self.model_dump_json(by_alias=True)


# --- Shared parts ---------------------------------------------------------------------------


class DeletionCounts(AuditMetadata):
    """`counts` of a deletion: items per Cosmos DB container, rows per SQL table."""

    conversations: Count
    memories: Count
    usage: Count
    users: Count
    project_members: Count
    user_index_overrides: Count


class ExportCounts(DeletionCounts):
    """`counts` of an export: the deletion's members plus the audit and actor rows."""

    audit_events: Count
    actor_references: Count


class FailedModelCall(AuditMetadata):
    deployment: Annotated[DeploymentName | Literal["contentsafety"], Field(max_length=64)]
    call: ModelCheckCall
    reasoning_effort: ReasoningEffort | None
    status: Status


class TokenizerProbe(AuditMetadata):
    deployment: DeploymentName
    local_tokens: Count
    reported_tokens: Count


class LatencyProbe(AuditMetadata):
    call: Literal["summary", "memory"]
    calls: Count
    p95_ms: Count
    timeout_seconds: Count


class PromptShieldsProbe(AuditMetadata):
    probe: Literal["request", "empty_user_prompt"]
    status: Status
    attack_detected: bool | None
    outcome: Literal["passed", "documents_refused", "user_prompt_refused", "failed"]


# --- Access and configuration ---------------------------------------------------------------


class UserProvisionedMetadata(AuditMetadata):
    default_project_count: Count


class ProjectCreatedMetadata(AuditMetadata):
    is_default: bool


class ProjectUpdatedMetadata(AuditMetadata):
    changed_fields: Annotated[
        list[ProjectField], Field(min_length=1, max_length=5), AfterValidator(_unique)
    ]


class ProjectArchivedMetadata(AuditMetadata):
    was_default: bool


class ProjectMemberAddedMetadata(AuditMetadata):
    grant_source: Literal["admin"]


class ProjectMemberRemovedMetadata(AuditMetadata):
    grant_source: Literal["default", "admin"]


class ProjectIndexMappedMetadata(AuditMetadata):
    is_default: bool


class ProjectIndexDefaultSetMetadata(AuditMetadata):
    previous_default_index_id: CanonicalUuid


class ProjectIndexUnmappedMetadata(AuditMetadata):
    was_default: bool
    overrides_removed: Count


class _OpsConfigValues(AuditMetadata):
    """Rule 7: the values themselves, or, when they would push the row past 4,000 characters,
    their hashes; exactly one form, and the absent form's keys are left out."""

    _value_fields: ClassVar[tuple[str, ...]]
    _hash_fields: ClassVar[tuple[str, ...]]

    @model_validator(mode="after")
    def _one_form(self) -> Self:
        values = [name in self.model_fields_set for name in self._value_fields]
        hashes = [getattr(self, name) is not None for name in self._hash_fields]
        if not ((all(values) and not any(hashes)) or (all(hashes) and not any(values))):
            raise ValueError("either the values or their hashes")
        return self

    @model_serializer(mode="wrap")
    def _drop_absent_form(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        uses_values = self._value_fields[0] in self.model_fields_set
        dropped = self._hash_fields if uses_values else self._value_fields
        for name in dropped:
            data.pop(to_camel(name), None)
            data.pop(name, None)
        return data


class OpsConfigSetMetadata(_OpsConfigValues):
    _value_fields = ("previous", "new")
    _hash_fields = ("previous_sha256", "new_sha256")

    area: OpsConfigArea
    previous: JsonValue = None
    new: JsonValue = None
    previous_sha256: Sha256Hex | None = None
    new_sha256: Sha256Hex | None = None
    ticket: Ticket

    @classmethod
    def build(cls, *, area: str, previous: JsonValue, new: JsonValue, ticket: str) -> Self:
        """The values when the row fits, otherwise both hashes."""
        inline = cls(area=area, previous=previous, new=new, ticket=ticket)
        if len(inline.to_json()) <= METADATA_MAX_CHARACTERS:
            return inline
        return cls(
            area=area,
            previous_sha256=value_sha256(previous),
            new_sha256=value_sha256(new),
            ticket=ticket,
        )


class OpsConfigResetMetadata(_OpsConfigValues):
    _value_fields = ("previous",)
    _hash_fields = ("previous_sha256",)

    area: OpsConfigArea
    previous: JsonValue = None
    previous_sha256: Sha256Hex | None = None
    ticket: Ticket

    @classmethod
    def build(cls, *, area: str, previous: JsonValue, ticket: str) -> Self:
        """The value when the row fits, otherwise its hash."""
        inline = cls(area=area, previous=previous, ticket=ticket)
        if len(inline.to_json()) <= METADATA_MAX_CHARACTERS:
            return inline
        return cls(area=area, previous_sha256=value_sha256(previous), ticket=ticket)


class UserIndexOverrideSetMetadata(AuditMetadata):
    search_index_id: CanonicalUuid
    expires_at: UtcDateTime
    replaced: bool
    ticket: Ticket


class UserIndexOverrideClearedMetadata(AuditMetadata):
    search_index_id: CanonicalUuid
    ticket: Ticket


# --- Conversations, memory and user data -------------------------------------------------------


class MemoryFactUpdatedMetadata(AuditMetadata):
    category: MemoryCategory
    previous_category: MemoryCategory
    previous_source_kind: MemorySourceKind


class MemoryFactDeletedMetadata(AuditMetadata):
    category: MemoryCategory
    source_kind: MemorySourceKind


class MemoryClearedMetadata(AuditMetadata):
    facts_removed: Annotated[int, Field(ge=1, le=200)]


class MemoryFactsExtractedMetadata(AuditMetadata):
    added_fact_ids: Annotated[list[CanonicalUuid], Field(max_length=10)]
    replaced_fact_ids: Annotated[list[CanonicalUuid], Field(max_length=10)]
    categories: Annotated[
        list[MemoryCategory], Field(min_length=1, max_length=5), AfterValidator(_unique)
    ]
    session_id: CanonicalUuid
    turn_id: CanonicalUuid

    @model_validator(mode="after")
    def _one_to_ten_facts(self) -> Self:
        if not 1 <= len(self.added_fact_ids) + len(self.replaced_fact_ids) <= 10:  # noqa: PLR2004
            raise ValueError("an extraction records 1 to 10 facts")
        return self


class ConversationReadMetadata(AuditMetadata):
    turn_ids: Annotated[list[CanonicalUuid], Field(max_length=50)]
    ticket: Ticket
    level: Literal["read"]


class UserDataExportedMetadata(AuditMetadata):
    ticket: Ticket
    counts: ExportCounts
    manifest_sha256: Sha256Hex


class UserDataDeletedMetadata(AuditMetadata):
    ticket: Ticket
    counts: DeletionCounts


class DocumentTracesRedactedMetadata(AuditMetadata):
    document_id: CanonicalUuid
    trace_count: Count
    ticket: Ticket


# --- Ingestion ----------------------------------------------------------------------------


class IndexCreatedMetadata(AuditMetadata):
    method_key: MethodKey
    method_version: AtLeastOne
    embedding_model: ModelName
    embedding_dimensions: Count
    language_analyzer: AnalyzerName
    physical_index_id: CanonicalUuid
    run_id: CanonicalUuid


class IndexUpdatedMetadata(AuditMetadata):
    changed_fields: Annotated[
        list[IndexField], Field(min_length=1, max_length=8), AfterValidator(_unique)
    ]
    status: IndexStatus


class IndexDeletedMetadata(AuditMetadata):
    run_id: CanonicalUuid
    physical_index_count: Count
    document_count: Count


class IndexReingestStartedMetadata(AuditMetadata):
    run_id: CanonicalUuid
    mode: ReingestMode
    item_count: Count


class IngestionReindexStartedMetadata(AuditMetadata):
    run_id: CanonicalUuid
    mode: ReingestMode
    item_count: Count
    ticket: Ticket


class DocumentUploadCommittedMetadata(AuditMetadata):
    index_id: CanonicalUuid
    document_id: CanonicalUuid
    version_number: AtLeastOne
    size_bytes: Annotated[int, Field(ge=1)]
    content_type: ContentType


class DocumentMalwareDetectedMetadata(AuditMetadata):
    index_id: CanonicalUuid
    document_id: CanonicalUuid
    version_number: AtLeastOne


class DocumentSourceFetchedMetadata(AuditMetadata):
    index_id: CanonicalUuid
    document_id: CanonicalUuid
    version_number: AtLeastOne
    size_bytes: Annotated[int, Field(ge=1)]
    ticket: Ticket


class DocumentDeletedMetadata(AuditMetadata):
    index_id: CanonicalUuid
    run_id: CanonicalUuid
    superseded_version_count: Count
    cancelled_item_count: Count


class DocumentReingestRequestedMetadata(AuditMetadata):
    index_id: CanonicalUuid
    run_id: CanonicalUuid | None
    item_count: Annotated[int, Field(ge=0, le=1)]


class IngestionRunSubmittedMetadata(AuditMetadata):
    index_id: CanonicalUuid
    item_count: AtLeastOne
    not_queued_count: Count


class IngestionRunCancelledMetadata(AuditMetadata):
    index_id: CanonicalUuid
    cancelled_item_count: Count
    ticket: Ticket


class IngestionItemRequeuedMetadata(AuditMetadata):
    run_id: CanonicalUuid
    claim_count: Count
    ticket: Ticket


class IngestionItemFailedMetadata(AuditMetadata):
    run_id: CanonicalUuid
    outcome_code: Literal["processing_interrupted"]
    ticket: Ticket


class IngestionRebuildAbandonedMetadata(AuditMetadata):
    index_id: CanonicalUuid
    generation: AtLeastOne
    cancelled_item_count: Count
    run_id: CanonicalUuid
    ticket: Ticket


class IngestionDeploymentsChangedMetadata(AuditMetadata):
    index_id: CanonicalUuid
    generation: AtLeastOne
    previous_query_deployment: DeploymentName
    query_deployment: DeploymentName
    previous_ingestion_deployment: DeploymentName
    ingestion_deployment: DeploymentName
    ticket: Ticket


class IngestionPoisonDrainedMetadata(AuditMetadata):
    deleted_count: Count
    ticket: Ticket


# --- Quality ----------------------------------------------------------------------------------


class TestUsersRotatedMetadata(AuditMetadata):
    # The name follows the catalogue's rule; pytest must not take it for a test class.
    __test__ = False

    environment: Literal["dev"]
    alias_count: Annotated[int, Field(ge=1, le=25)]


class ModelDeploymentsCheckedMetadata(AuditMetadata):
    environment: Literal["dev"]
    result: Literal["passed", "failed"]
    call_count: Annotated[int, Field(ge=1, le=60)]
    failures: Annotated[list[FailedModelCall], Field(max_length=10)]
    tokenizer: Annotated[list[TokenizerProbe], Field(max_length=2)]
    latency: Annotated[list[LatencyProbe], Field(max_length=2)]
    prompt_shields: Annotated[list[PromptShieldsProbe], Field(max_length=2)]
    ticket: Ticket


METADATA_MODELS: Final[dict[str, type[AuditMetadata] | None]] = {
    "user.provisioned": UserProvisionedMetadata,
    "project.created": ProjectCreatedMetadata,
    "project.updated": ProjectUpdatedMetadata,
    "project.archived": ProjectArchivedMetadata,
    "project.restored": None,
    "project_member.added": ProjectMemberAddedMetadata,
    "project_member.removed": ProjectMemberRemovedMetadata,
    "project_index.mapped": ProjectIndexMappedMetadata,
    "project_index.default_set": ProjectIndexDefaultSetMetadata,
    "project_index.unmapped": ProjectIndexUnmappedMetadata,
    "ops_config.set": OpsConfigSetMetadata,
    "ops_config.reset": OpsConfigResetMetadata,
    "user_index_override.set": UserIndexOverrideSetMetadata,
    "user_index_override.cleared": UserIndexOverrideClearedMetadata,
    "memory.paused": None,
    "memory.resumed": None,
    "memory.fact_updated": MemoryFactUpdatedMetadata,
    "memory.fact_deleted": MemoryFactDeletedMetadata,
    "memory.cleared": MemoryClearedMetadata,
    "memory.facts_extracted": MemoryFactsExtractedMetadata,
    "conversation.read": ConversationReadMetadata,
    "user_data.exported": UserDataExportedMetadata,
    "user_data.deleted": UserDataDeletedMetadata,
    "document.traces_redacted": DocumentTracesRedactedMetadata,
    "index.created": IndexCreatedMetadata,
    "index.updated": IndexUpdatedMetadata,
    "index.deleted": IndexDeletedMetadata,
    "index.reingest_started": IndexReingestStartedMetadata,
    "ingestion.reindex_started": IngestionReindexStartedMetadata,
    "document.upload_committed": DocumentUploadCommittedMetadata,
    "document.malware_detected": DocumentMalwareDetectedMetadata,
    "document.source_fetched": DocumentSourceFetchedMetadata,
    "document.deleted": DocumentDeletedMetadata,
    "document.reingest_requested": DocumentReingestRequestedMetadata,
    "ingestion.run_submitted": IngestionRunSubmittedMetadata,
    "ingestion.run_cancelled": IngestionRunCancelledMetadata,
    "ingestion.item_requeued": IngestionItemRequeuedMetadata,
    "ingestion.item_failed": IngestionItemFailedMetadata,
    "ingestion.rebuild_abandoned": IngestionRebuildAbandonedMetadata,
    "ingestion.deployments_changed": IngestionDeploymentsChangedMetadata,
    "ingestion.poison_drained": IngestionPoisonDrainedMetadata,
    "test_users.rotated": TestUsersRotatedMetadata,
    "model_deployments.checked": ModelDeploymentsCheckedMetadata,
}
"""Each action's metadata model; `None` for the three actions without metadata."""

_LOWER_UUID_RE: Final = re.compile(LOWER_CASE_UUID_PATTERN)
_OPS_CONFIG_KEY_RE: Final = re.compile(r"^[a-z0-9_]+\.[a-z0-9_.]*[a-z0-9_]$")


def _target_text(value: object) -> object:
    return str(value) if isinstance(value, UUID) else value


class AuditEvent(BaseModel):
    """One row of `app.audit_events` before its database columns (`id`, `occurred_at`) and its
    correlation ID are added."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: AuditAction
    actor_type: AuditActorType
    actor_object_id: CanonicalUuid
    subject_object_id: CanonicalUuid | None = None
    target_type: AuditTargetType
    target_id: Annotated[
        Annotated[str, StringConstraints(max_length=128)] | None, BeforeValidator(_target_text)
    ]
    project_id: CanonicalUuid | None = None
    metadata: AuditMetadata | None

    @model_validator(mode="after")
    def _matches_the_catalogue(self) -> Self:
        model = METADATA_MODELS[self.action]
        if model is None:
            if self.metadata is not None or self.action not in ACTIONS_WITHOUT_METADATA:
                raise ValueError("this action carries no metadata")
        elif type(self.metadata) is not model:
            raise ValueError("the metadata isn't this action's model")
        if self.action in ACTIONS_WITHOUT_TARGET_ID:
            if self.target_id is not None:
                raise ValueError("this action has no target ID")
        elif self.target_id is None:
            raise ValueError("this action names its target")
        elif self.target_type == "ops_config":
            if not _OPS_CONFIG_KEY_RE.fullmatch(self.target_id):
                raise ValueError("an ops-config target is the key")
        elif self.target_type == "ingestion_queue":
            if self.target_id != POISON_QUEUE:
                raise ValueError("the queue target is the poison queue")
        elif not _LOWER_UUID_RE.fullmatch(self.target_id):
            raise ValueError("the target ID is lower-case UUID text")
        return self

    def metadata_json(self) -> str | None:
        """The `metadata_json` column's text."""
        return None if self.metadata is None else self.metadata.to_json()
