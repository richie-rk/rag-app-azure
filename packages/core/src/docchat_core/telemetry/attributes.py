"""The attribute registry (`85-observability.md` sections 3.2 to 3.5 and 4).

Frozen module-level mappings of every name a record may carry: `KEYS` (span and log attribute
keys with their types and allowed values), `SPANS` (first-party span names with their kind, their
complete attribute list and any narrower value sets), `LOG_EVENTS` (log record names with their
level and keys), `EVENTS` (custom events with their lowerCamelCase properties), `METRICS` (every
instrument with its kind, unit and dimensions) and `DIMENSIONS` (the closed metric-dimension set).
Telemetry carries metadata only (ADR 0014): a value outside its key's type never leaves the
process, so content can't reach a record through an allowed key. A change that adds a key, span,
record, event or metric adds it here and to section 4 of the observability design in the same
pull request.

Three enum keys name members of closed sets this package can't import (contract 4 of
`01-repository.md` section 2.4): the audit actions (`docchat_core.audit`), the ops-config areas
and keys (`docchat_core.opsconfig`) and the configured Azure OpenAI deployment names (the
settings of the areas that call the models). Their specs carry the shape every member of the set
has, so a value of any other shape is refused here and the owning module keeps the set itself.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from opentelemetry.trace import SpanKind

from docchat_core.errors import CODES, ITEM_CODES
from docchat_core.models import LOWER_CASE_UUID_PATTERN, UTC_TIME_PATTERN

QUALIFIED_TYPE_NAME_PATTERN: Final = r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_<>][A-Za-z0-9_<>]*)*$"
"""`module.QualName` of an exception class; `<locals>` appears in a nested class's name."""

CAMEL_CASE_PATTERN: Final = r"^[a-z][a-zA-Z0-9]*$"
"""A custom event property: lowerCamelCase, never snake_case or dotted."""

DEPLOYMENT_NAME_PATTERN: Final = r"^[A-Za-z0-9._-]{2,64}$"
"""An Azure OpenAI deployment name (`81-config.md` section 2.4)."""

MODEL_NAME_PATTERN: Final = r"^[a-z0-9][a-z0-9.\-]{0,63}$"
"""A model name, in the `pricing.model_rates` key form (`81-config.md` section 2.4)."""

AUDIT_ACTION_PATTERN: Final = r"^(?=.{3,64}$)[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$"
"""An audit action, `<area>.<event>` (`80-data-model.md` section 2.21)."""

AUDIT_TARGET_TYPE_PATTERN: Final = r"^[a-z][a-z0-9_]{0,31}$"
AUDIT_TARGET_ID_PATTERN: Final = r"^[a-z0-9][a-z0-9_.-]{0,127}$"
"""Lower-case UUID text, an ops-config key or a queue name (`80-data-model.md` section 2.8)."""

OPS_CONFIG_AREA_PATTERN: Final = r"^[a-z0-9_]{1,64}$"
OPS_CONFIG_KEY_PATTERN: Final = r"^(?=.{3,64}$)[a-z0-9_]+\.[a-z0-9_.]*[a-z0-9_]$"
"""An ops-config key, `<area>.<name>` (`81-config.md` section 1.2)."""

HTTP_ROUTE_PATTERN: Final = r"^/[A-Za-z0-9_{}/.:-]*$"
QUEUE_MESSAGE_ID_PATTERN: Final = r"^[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}$"

CUSTOM_EVENT_NAME_ATTRIBUTE: Final = "microsoft.custom_event.name"
"""The log attribute that makes the exporter store a record in `AppEvents`."""

SQL_SPAN_PREFIX: Final = "docchat.sql."
SQL_SPAN_FAMILY: Final = "docchat.sql.*"
"""The SQL layer's spans are `docchat.sql.<statement-set name>`, one name per unit of work."""

_STATEMENT_SET_RE: Final = re.compile(r"^[a-z][a-z0-9_]*$")
_EVENT_NAME_RE: Final = re.compile(r"^[a-z][a-z0-9_]*$")


class ValueKind(StrEnum):
    UUID = "uuid"
    ENUM = "enum"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    STRING = "string"
    TIMESTAMP = "timestamp"


@dataclass(frozen=True, slots=True, kw_only=True)
class ValueSpec:
    """The type and allowed values of one attribute key or event property.

    `values` closes an enum (strings) or an integer (the allowed numbers); `pattern` is the shape
    of a string, or of every member of an enum whose set lives outside this package; `minimum`
    and `maximum` bound a number and `max_length` a string.
    """

    kind: ValueKind
    values: frozenset[str] | frozenset[int] | None = None
    pattern: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    max_length: int | None = None
    _compiled: re.Pattern[str] | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.pattern is not None:
            object.__setattr__(self, "_compiled", re.compile(self.pattern))
        if self.kind is ValueKind.ENUM and self.values is None and self.pattern is None:
            raise ValueError("an enum needs its values or the shape of its members")

    def narrowed(self, values: Iterable[str]) -> ValueSpec:
        """The same spec closed to `values`, for a span or record that allows fewer members."""
        return ValueSpec(kind=self.kind, values=frozenset(values))

    def accepts(self, value: object) -> bool:
        """Whether `value` has this spec's type and lies within its allowed values."""
        match self.kind:
            case ValueKind.BOOLEAN:
                return isinstance(value, bool)
            case ValueKind.INTEGER:
                return (
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and self._in_range(value)
                    and (self.values is None or value in self.values)
                )
            case ValueKind.FLOAT:
                return (
                    isinstance(value, int | float)
                    and not isinstance(value, bool)
                    and math.isfinite(value)
                    and self._in_range(value)
                )
            case ValueKind.UUID:
                return isinstance(value, str) and _UUID_RE.fullmatch(value) is not None
            case ValueKind.TIMESTAMP:
                return isinstance(value, str) and _UTC_TIME_RE.fullmatch(value) is not None
            case ValueKind.ENUM | ValueKind.STRING:
                return isinstance(value, str) and self._string_ok(value)

    def _in_range(self, value: float) -> bool:
        if self.minimum is not None and value < self.minimum:
            return False
        return self.maximum is None or value <= self.maximum

    def _string_ok(self, value: str) -> bool:
        if self.values is not None:
            return value in self.values
        if self.max_length is not None and len(value) > self.max_length:
            return False
        return self._compiled is None or self._compiled.fullmatch(value) is not None


_UUID_RE: Final = re.compile(LOWER_CASE_UUID_PATTERN)
_UTC_TIME_RE: Final = re.compile(UTC_TIME_PATTERN)


def _uuid() -> ValueSpec:
    return ValueSpec(kind=ValueKind.UUID)


def _enum(*values: str) -> ValueSpec:
    return ValueSpec(kind=ValueKind.ENUM, values=frozenset(values))


def _shaped_enum(pattern: str) -> ValueSpec:
    return ValueSpec(kind=ValueKind.ENUM, pattern=pattern)


def _int(minimum: int | None = 0, maximum: int | None = None) -> ValueSpec:
    return ValueSpec(kind=ValueKind.INTEGER, minimum=minimum, maximum=maximum)


def _int_of(*values: int) -> ValueSpec:
    return ValueSpec(kind=ValueKind.INTEGER, values=frozenset(values))


def _float(minimum: float | None = 0.0) -> ValueSpec:
    return ValueSpec(kind=ValueKind.FLOAT, minimum=minimum)


def _bool() -> ValueSpec:
    return ValueSpec(kind=ValueKind.BOOLEAN)


def _string(pattern: str | None = None, max_length: int | None = None) -> ValueSpec:
    return ValueSpec(kind=ValueKind.STRING, pattern=pattern, max_length=max_length)


def _timestamp() -> ValueSpec:
    return ValueSpec(kind=ValueKind.TIMESTAMP)


def _frozen[K, V](items: Mapping[K, V]) -> Mapping[K, V]:
    return MappingProxyType(dict(items))


# Closed sets several keys, events and spans share.

INTERNAL_CODES: Final = frozenset(
    {"model_unavailable", "content_filtered", "invalid_output", "store_conflict"}
)
"""The internal codes stored in conversation documents (`11-api.md` section 2.6): the summary and
extraction spans carry them, and the operators' failure queries filter those spans by them
(`13-conversations-and-memory.md` section 10.12 item 8)."""

ERROR_CODES: Final = frozenset(CODES) | frozenset(ITEM_CODES) | INTERNAL_CODES
"""`docchat.error_code`: a code of `CODES`, an ingestion item code, or an internal code of the
conversation store."""

ACTIVITY_NAMES: Final = frozenset(
    {
        "load_dispatch_settings",
        "sweep_ingestion_state",
        "claim_ingestion_items",
        "validate_item",
        "extract_document",
        "enrich_document",
        "chunk_document",
        "embed_chunks",
        "index_chunks",
        "render_pages",
        "rerender_pages",
        "flip_chunk_set",
        "clean_up_item",
        "purge_document_artifacts",
        "create_search_index",
        "delete_search_index",
        "purge_index_blobs",
        "record_item_outcome",
    }
)
"""The ingestion activities of `14-ingestion.md` section 4.2."""

CONTENT_TYPES: Final = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }
)
"""The three accepted content types (`14-ingestion.md` section 2.2)."""

RUN_KINDS: Final = frozenset(
    {
        "upload",
        "document_reingest",
        "document_delete",
        "index_create",
        "index_reingest",
        "index_rebuild",
        "index_delete",
        "maintenance",
    }
)
ITEM_OPERATIONS: Final = frozenset(
    {
        "ingest",
        "cleanup_document",
        "rerender",
        "create_physical_index",
        "delete_physical_index",
        "purge_index_blobs",
    }
)
ITEM_STAGES: Final = frozenset(
    {
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
    }
)
FINISHED_RUN_STATES: Final = frozenset({"completed", "completed_with_errors", "failed"})
"""The states a run finishes in (`80-data-model.md` section 2, `CK_ingestion_runs_state`)."""

MODEL_CALLS: Final = frozenset(
    {
        "rewrite",
        "search_passage",
        "answer",
        "followups",
        "query_embedding",
        "passage_embedding",
        "summary",
        "memory",
    }
)
MEMORY_REJECTION_REASONS: Final = (
    "sensitivity",
    "invalid_target",
    "empty",
    "too_long",
    "contains_link",
    "contains_code",
    "contains_instruction",
    "contains_secret",
    "duplicate_fact",
    "cap_reached",
    "over_limit",
)
AUTH_REASONS: Final = frozenset(
    {
        "missing_header",
        "bad_scheme",
        "duplicate_header",
        "token_too_long",
        "malformed",
        "bad_alg",
        "missing_kid",
        "unknown_kid",
        "keys_unavailable",
        "bad_signature",
        "expired",
        "not_yet_valid",
        "bad_iat",
        "bad_issuer",
        "bad_audience",
        "missing_claim",
        "bad_version",
        "bad_tenant",
        "bad_client",
        "missing_scope",
        "bad_oid",
        "bad_roles",
        "no_role",
    }
)
HTTP_METHODS: Final = frozenset(
    {"CONNECT", "DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "TRACE", "_OTHER"}
)

# Outcome sets of `docchat.outcome`, one per span or record that carries it (section 4).
_OK_ERROR: Final = ("ok", "error")
_TURN_OUTCOMES: Final = ("completed", "failed", "cancelled", "replayed")
_MODEL_CALL_OUTCOMES: Final = ("ok", "filtered", "cancelled", "timed_out", "failed")
_SAVE_OUTCOMES: Final = ("saved", "superseded", "failed")
_MEMORY_ROUTE_OUTCOMES: Final = (
    "ok",
    "unchanged",
    "precondition_failed",
    "precondition_required",
    "not_found",
    "invalid",
    "unavailable",
)

_ID_KEYS: Final = (
    "docchat.project_id",
    "docchat.session_id",
    "docchat.turn_id",
    "docchat.index_id",
    "docchat.physical_index_id",
    "docchat.document_id",
    "docchat.version_id",
    "docchat.chunk_set_id",
    "docchat.run_id",
    "docchat.item_id",
)


@dataclass(frozen=True, slots=True, kw_only=True)
class SpanSpec:
    """A first-party span: its kind, its complete attribute list, the narrower value sets some of
    its keys have on it, and whether it always starts a trace of its own."""

    kind: SpanKind
    attributes: frozenset[str]
    values: Mapping[str, frozenset[str]] = field(default_factory=lambda: MappingProxyType({}))
    root: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class LogEventSpec:
    """A log record: its level and the keys it may carry."""

    level: int
    keys: frozenset[str]
    values: Mapping[str, frozenset[str]] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True, slots=True, kw_only=True)
class EventSpec:
    """A custom event: its lowerCamelCase properties and their types."""

    properties: Mapping[str, ValueSpec]


class InstrumentKind(StrEnum):
    COUNTER = "counter"
    HISTOGRAM = "histogram"
    GAUGE = "gauge"
    OBSERVABLE_GAUGE = "observable gauge"


@dataclass(frozen=True, slots=True, kw_only=True)
class MetricSpec:
    """An instrument: its kind, UCUM unit and declared dimension keys."""

    kind: InstrumentKind
    unit: str
    dimensions: frozenset[str]


def _span(
    kind: SpanKind,
    *attributes: str,
    values: Mapping[str, Iterable[str]] | None = None,
    root: bool = False,
) -> SpanSpec:
    return SpanSpec(
        kind=kind,
        attributes=frozenset(attributes),
        values=MappingProxyType({key: frozenset(v) for key, v in (values or {}).items()}),
        root=root,
    )


def _outcomes(*outcomes: str) -> Mapping[str, Iterable[str]]:
    return {"docchat.outcome": outcomes}


def _record(
    level: int, *keys: str, values: Mapping[str, Iterable[str]] | None = None
) -> LogEventSpec:
    return LogEventSpec(
        level=level,
        keys=frozenset(keys),
        values=MappingProxyType({key: frozenset(v) for key, v in (values or {}).items()}),
    )


def _event(**properties: ValueSpec) -> EventSpec:
    return EventSpec(properties=MappingProxyType(properties))


def _metric(kind: InstrumentKind, unit: str, *dimensions: str) -> MetricSpec:
    return MetricSpec(kind=kind, unit=unit, dimensions=frozenset(dimensions))


_INTERNAL: Final = SpanKind.INTERNAL
_MEMORY_ROUTE: Final = _span(
    _INTERNAL,
    "docchat.outcome",
    "docchat.memory.facts_count",
    "docchat.cosmos.request_charge",
    values=_outcomes(*_MEMORY_ROUTE_OUTCOMES),
)
_SESSION_READ: Final = _span(
    _INTERNAL,
    "docchat.session_id",
    "docchat.page.limit",
    "docchat.page.returned",
    "docchat.projects.reachable",
    "docchat.outcome",
    "docchat.cosmos.request_charge",
    values=_outcomes("ok", "not_found"),
)
_MODEL_CALL: Final = _span(
    _INTERNAL,
    "docchat.deployment",
    "docchat.outcome",
    "docchat.prompt_tokens",
    "docchat.completion_tokens",
    values=_outcomes(*_MODEL_CALL_OUTCOMES),
)

SPANS: Final[Mapping[str, SpanSpec]] = _frozen(
    {
        # 4.2 Shared spans.
        SQL_SPAN_FAMILY: _span(
            SpanKind.CLIENT,
            "db.system.name",
            "docchat.sql.attempts",
            "docchat.sql.error_number",
            "docchat.outcome",
            values={"docchat.outcome": _OK_ERROR, "db.system.name": ("microsoft.sql_server",)},
        ),
        # 4.3 Identity and access.
        "docchat.auth.validate_token": _span(
            _INTERNAL,
            "docchat.auth.outcome",
            "docchat.auth.reason",
            "docchat.auth.client",
            values={"docchat.auth.outcome": ("ok", "invalid", "no_role", "unavailable")},
        ),
        "docchat.auth.fetch_signing_keys": _span(
            _INTERNAL,
            "docchat.auth.trigger",
            "docchat.auth.outcome",
            "docchat.auth.key_count",
            "http.response.status_code",
            values={"docchat.auth.outcome": _OK_ERROR},
        ),
        "docchat.users.ensure": _span(
            _INTERNAL, "docchat.users.outcome", "docchat.users.default_project_count"
        ),
        "docchat.access.authorize_project": _span(
            _INTERNAL,
            "docchat.project_id",
            "docchat.access.outcome",
            "docchat.access.via",
            values={"docchat.access.outcome": ("allowed", "not_found")},
        ),
        "docchat.access.resolve_index": _span(
            _INTERNAL,
            "docchat.project_id",
            "docchat.index_id",
            "docchat.access.outcome",
            "docchat.access.index_source",
            "docchat.access.index_cache",
            values={"docchat.access.outcome": ("ok", "unavailable")},
        ),
        "docchat.access.reachable_projects": _span(_INTERNAL, "docchat.access.project_count"),
        "docchat.opsconfig.refresh": _span(
            _INTERNAL, "docchat.opsconfig.outcome", "docchat.opsconfig.invalid_areas"
        ),
        "docchat.ratelimit.write_check": _span(
            _INTERNAL, "docchat.ratelimit.outcome", "docchat.ratelimit.attempts"
        ),
        # 4.4 Chat pipeline.
        "docchat.chat.prestream": _span(
            _INTERNAL,
            "docchat.project_id",
            "docchat.session_id",
            "docchat.outcome",
            values=_outcomes("accepted", *CODES),
        ),
        "docchat.chat.turn": _span(
            _INTERNAL,
            "docchat.project_id",
            "docchat.session_id",
            "docchat.turn_id",
            "docchat.outcome",
            "docchat.error_code",
            "docchat.ttft_ms",
            "docchat.broadened",
            "docchat.rerank_partial",
            "docchat.sources_sent",
            "docchat.sources_flagged",
            "docchat.prompt_tokens",
            "docchat.finish_reason",
            values=_outcomes(*_TURN_OUTCOMES),
        ),
        "docchat.chat.rewrite": _MODEL_CALL,
        "docchat.chat.search_passage": _MODEL_CALL,
        "docchat.chat.followups": _MODEL_CALL,
        "docchat.chat.embed": _span(
            _INTERNAL, "docchat.deployment", "docchat.dimensions", "docchat.pass"
        ),
        "docchat.chat.search": _span(
            _INTERNAL,
            "docchat.physical_index_id",
            "docchat.pass",
            "docchat.results",
            "docchat.rerank_partial",
        ),
        "docchat.chat.served_filter": _span(_INTERNAL, "docchat.candidates", "docchat.served"),
        "docchat.chat.screen": _span(
            _INTERNAL,
            "docchat.calls",
            "docchat.segments",
            "docchat.characters",
            "docchat.flagged",
            "docchat.outcome",
            values=_outcomes("passed", "flagged", "unavailable"),
        ),
        "docchat.chat.assemble": _span(
            _INTERNAL, "docchat.prompt_tokens", "docchat.dropped_turns", "docchat.dropped_sources"
        ),
        "docchat.chat.generate": _span(
            _INTERNAL,
            "docchat.deployment",
            "docchat.model",
            "docchat.reasoning_effort",
            "docchat.first_chunk_ms",
            "docchat.prompt_tokens",
            "docchat.completion_tokens",
            "docchat.reasoning_tokens",
            "docchat.finish_reason",
        ),
        "docchat.chat.persist": _span(
            _INTERNAL, "docchat.outcome", "docchat.attempts", values=_outcomes(*_SAVE_OUTCOMES)
        ),
        "docchat.citation.open": _span(
            _INTERNAL,
            "docchat.operation",
            "docchat.outcome",
            "docchat.open_as",
            values=_outcomes(
                "ok", "not_found", "gone", "unavailable", "missing", "limited", "aborted"
            ),
        ),
        # 4.5 Conversations and memory.
        "docchat.conversations.begin_turn": _span(
            _INTERNAL,
            "docchat.session_id",
            "docchat.turn_id",
            "docchat.turn_seq",
            "docchat.session_new",
            "docchat.outcome",
            "docchat.attempts",
            "docchat.cosmos.request_charge",
            values=_outcomes(
                "started",
                "replay",
                "in_progress",
                "already_ended",
                "conflict",
                "not_found",
                "turn_limit",
                "unavailable",
            ),
        ),
        "docchat.conversations.read_recent_turns": _span(
            _INTERNAL,
            "docchat.session_id",
            "docchat.history.turns_loaded",
            "docchat.history.tokens_loaded",
            "docchat.history.stopped_at_budget",
            "docchat.cosmos.request_charge",
        ),
        "docchat.memory.prompt_read": _span(
            _INTERNAL,
            "docchat.memory.active",
            "docchat.memory.facts_rendered",
            "docchat.outcome",
            values=_outcomes("ok", "empty", "unavailable"),
        ),
        "docchat.conversations.ensure_post_turn_work": _span(
            _INTERNAL,
            "docchat.session_id",
            "docchat.summary.due",
            "docchat.history.unsummarised_tokens",
            "docchat.history.unsummarised_turns",
            "docchat.maintenance.scheduled",
        ),
        "docchat.conversations.finalize_turn": _span(
            _INTERNAL,
            "docchat.outcome",
            "docchat.turn.status",
            "docchat.turn.error_code",
            "docchat.trace.bytes",
            "docchat.attempts",
            "docchat.cosmos.request_charge",
            values=_outcomes(*_SAVE_OUTCOMES),
        ),
        "docchat.conversations.attach_followups": _span(
            _INTERNAL,
            "docchat.session_id",
            "docchat.turn_id",
            "docchat.followups.count",
            "docchat.outcome",
            values=_outcomes("saved", "not_saved"),
        ),
        "docchat.sessions.list": _span(
            _INTERNAL,
            "docchat.page.limit",
            "docchat.page.returned",
            "docchat.projects.reachable",
            "docchat.filter.project",
            "docchat.cosmos.request_charge",
        ),
        "docchat.sessions.get": _SESSION_READ,
        "docchat.sessions.list_turns": _SESSION_READ,
        "docchat.feedback.put": _span(
            _INTERNAL,
            "docchat.session_id",
            "docchat.turn_id",
            "docchat.project_id",
            "docchat.feedback.rating",
            "docchat.feedback.category",
            "docchat.feedback.is_change",
            "docchat.outcome",
            values=_outcomes("ok", *CODES),
        ),
        "docchat.memory.get": _MEMORY_ROUTE,
        "docchat.memory.update_settings": _MEMORY_ROUTE,
        "docchat.memory.update_fact": _MEMORY_ROUTE,
        "docchat.memory.delete_fact": _MEMORY_ROUTE,
        "docchat.memory.clear": _MEMORY_ROUTE,
        "docchat.maintenance.post_turn": _span(
            _INTERNAL,
            "docchat.session_id",
            "docchat.turn_id",
            "docchat.maintenance.extractions",
            "docchat.maintenance.summary_ran",
        ),
        "docchat.summary.rebuild": _span(
            _INTERNAL,
            "docchat.session_id",
            "docchat.deployment",
            "docchat.summary.folded_turns",
            "docchat.summary.input_tokens",
            "docchat.summary.output_tokens",
            "docchat.summary.truncated",
            "docchat.outcome",
            "docchat.error_code",
            values=_outcomes(
                "succeeded", "nothing_to_fold", "deferred_quota", "lease_taken", "failed"
            ),
        ),
        "docchat.memory.extract": _span(
            _INTERNAL,
            "docchat.session_id",
            "docchat.turn_id",
            "docchat.deployment",
            "docchat.memory.proposed",
            "docchat.memory.accepted",
            *(f"docchat.memory.rejected.{reason}" for reason in MEMORY_REJECTION_REASONS),
            "docchat.memory.shield_calls",
            "docchat.outcome",
            "docchat.error_code",
            values=_outcomes(
                "done",
                "skipped_paused",
                "skipped_disabled",
                "superseded",
                "expired",
                "deferred_quota",
                "deferred_screening",
                "failed",
            ),
        ),
        # 4.6 Ingestion.
        "docchat.ingestion.upload": _span(
            _INTERNAL,
            "docchat.index_id",
            "docchat.document_id",
            "docchat.version_id",
            "docchat.ingestion.upload_bytes",
            "docchat.outcome",
            "docchat.error_code",
            values=_outcomes("ok", "failed"),
        ),
        "docchat.ingestion.submit": _span(
            _INTERNAL,
            "docchat.index_id",
            "docchat.run_id",
            "docchat.ingestion.run_kind",
            "docchat.ingestion.item_count",
        ),
        "docchat.ingestion.queue_message": _span(
            SpanKind.CONSUMER,
            "docchat.run_id",
            "docchat.ingestion.dequeue_count",
            "docchat.ingestion.dispatcher_started",
        ),
        "docchat.ingestion.activity": _span(
            _INTERNAL,
            "docchat.activity_name",
            "docchat.run_id",
            "docchat.item_id",
            "docchat.ingestion.claim",
            "docchat.document_id",
            "docchat.version_id",
            "docchat.chunk_set_id",
            "docchat.physical_index_id",
            "docchat.attempts",
            "docchat.outcome",
            "docchat.error_code",
            values=_outcomes("ok", "failed"),
        ),
        "docchat.ingestion.parse": _span(
            _INTERNAL,
            "docchat.ingestion.parse_command",
            "docchat.ingestion.content_type",
            "docchat.ingestion.pages",
            "docchat.ingestion.parse_peak_rss_mb",
            "docchat.ingestion.parse_exit",
        ),
        "docchat.ingestion.dispatcher.sweep": _span(
            _INTERNAL,
            "docchat.ingestion.generation",
            "docchat.ingestion.items_requeued",
            "docchat.ingestion.runs_finalised",
            root=True,
        ),
        "docchat.ingestion.dispatcher.claim": _span(
            _INTERNAL,
            "docchat.ingestion.generation",
            "docchat.ingestion.items_claimed",
            root=True,
        ),
    }
)
"""Every first-party span of section 4. Instrumentation spans (server, Azure SDK and `httpx`)
keep their semantic-convention names and aren't listed."""

SERVER_SPAN_ATTRIBUTES: Final = frozenset(
    {"enduser.id", "docchat.auth.client", "docchat.audit.action", "docchat.error_code"}
)
"""The first-party attributes the API sets on the instrumentation's server span (section 4.2)."""

LOG_EVENTS: Final[Mapping[str, LogEventSpec]] = _frozen(
    {
        "request_failed": _record(
            logging.ERROR,
            "error.type",
            "docchat.error_code",
            values={"docchat.error_code": ("internal_error",)},
        ),
        "signing_keys_fetch_failed": _record(
            logging.WARNING, "http.response.status_code", "error.type", "docchat.auth.trigger"
        ),
        "ops_config_invalid": _record(
            logging.ERROR, "docchat.opsconfig.area", "docchat.opsconfig.key"
        ),
        "ops_config_unreachable": _record(logging.WARNING),
        "audit_write_failed": _record(
            logging.ERROR, "docchat.audit.action", "docchat.session_id", "docchat.turn_id"
        ),
        "chat_turn_finished": _record(
            logging.INFO,
            "docchat.project_id",
            "docchat.session_id",
            "docchat.turn_id",
            "docchat.outcome",
            "docchat.error_code",
            "docchat.finish_reason",
            "docchat.sources_sent",
            "docchat.sources_flagged",
            "docchat.dropped_turns",
            "docchat.dropped_sources",
            "docchat.ttft_ms",
            "docchat.chat.duration_ms",
            "docchat.prompt_tokens",
            "docchat.completion_tokens",
            "docchat.reasoning_tokens",
            "docchat.chat.estimated_cost_micros",
            "docchat.chat.usage_estimated",
            values=_outcomes(*_TURN_OUTCOMES),
        ),
        "usage_record_failed": _record(
            logging.ERROR, "docchat.session_id", "docchat.turn_id", "docchat.deployment"
        ),
        "citation_blob_missing": _record(
            logging.ERROR,
            "docchat.session_id",
            "docchat.turn_id",
            "docchat.citation.number",
            "docchat.document_id",
            "docchat.version_id",
        ),
        "citation_stream_failed": _record(
            logging.ERROR,
            "docchat.session_id",
            "docchat.turn_id",
            "docchat.citation.number",
            "docchat.citation.bytes_sent",
            "error.type",
        ),
        "citation_download_aborted": _record(
            logging.WARNING,
            "docchat.session_id",
            "docchat.turn_id",
            "docchat.citation.number",
            "docchat.citation.bytes_sent",
            "docchat.citation.abort_reason",
        ),
        "document_pdf_blob_missing": _record(
            logging.ERROR, "docchat.index_id", "docchat.document_id", "docchat.version_id"
        ),
        "chat_drained": _record(
            logging.WARNING,
            "docchat.chat.turn_tasks",
            "docchat.chat.background_tasks",
            "docchat.chat.downloads",
        ),
        "memory_unavailable": _record(
            logging.WARNING, "docchat.session_id", "docchat.turn_id", "error.type"
        ),
        "post_turn_step_failed": _record(
            logging.WARNING,
            "docchat.maintenance.step",
            "docchat.error_code",
            "docchat.session_id",
            "docchat.turn_id",
        ),
        "activity_failed": _record(
            logging.ERROR,
            "error.type",
            "docchat.error_code",
            values={"docchat.error_code": ("internal_error",)},
        ),
        "job_failed": _record(logging.ERROR, "error.type", "docchat.error_code"),
        "sweep_duty_failed": _record(logging.WARNING, "docchat.ingestion.duty", "error.type"),
        "maintenance_failed": _record(logging.WARNING, "docchat.ingestion.step", "error.type"),
        "scan_time_unusable": _record(logging.WARNING, "docchat.version_id"),
        "parse_uid_quarantined": _record(logging.WARNING, "docchat.ingestion.parse_uid"),
        "local_emulators_in_azure": _record(logging.ERROR),
        "insecure_endpoint": _record(logging.ERROR),
    }
)
"""Every log record of section 4.13; the record's message is its name."""


def _all_outcomes() -> frozenset[str]:
    narrowed = [spec.values for spec in SPANS.values()]
    narrowed += [spec.values for spec in LOG_EVENTS.values()]
    return frozenset(value for values in narrowed for value in values.get("docchat.outcome", ()))


KEYS: Final[Mapping[str, ValueSpec]] = _frozen(
    {
        # Cross-cutting keys.
        "enduser.id": _uuid(),
        **{key: _uuid() for key in _ID_KEYS},
        "docchat.outcome": ValueSpec(kind=ValueKind.ENUM, values=_all_outcomes()),
        "docchat.error_code": ValueSpec(kind=ValueKind.ENUM, values=ERROR_CODES),
        "docchat.deployment": _shaped_enum(DEPLOYMENT_NAME_PATTERN),
        "docchat.model": _string(MODEL_NAME_PATTERN),
        "docchat.activity_name": ValueSpec(kind=ValueKind.ENUM, values=ACTIVITY_NAMES),
        "docchat.attempts": _int(1, 100),
        "docchat.sql.attempts": _int(1, 10),
        "docchat.sql.error_number": _int(),
        "error.type": _string(QUALIFIED_TYPE_NAME_PATTERN, max_length=512),
        "http.route": _string(HTTP_ROUTE_PATTERN, max_length=512),
        "http.request.method": ValueSpec(kind=ValueKind.ENUM, values=HTTP_METHODS),
        "http.response.status_code": _int(100, 599),
        "db.system.name": _enum("microsoft.sql_server", "azure.cosmosdb"),
        "exception.type": _string(QUALIFIED_TYPE_NAME_PATTERN, max_length=512),
        "exception.message": _string(max_length=512),
        "exception.stacktrace": _string(),
        # Identity and access.
        "docchat.auth.outcome": _enum("ok", "invalid", "no_role", "unavailable", "error"),
        "docchat.auth.reason": ValueSpec(kind=ValueKind.ENUM, values=AUTH_REASONS),
        "docchat.auth.client": _enum("web", "test", "unknown"),
        "docchat.auth.trigger": _enum("startup", "scheduled", "unknown_kid"),
        "docchat.auth.key_count": _int(),
        "docchat.users.outcome": _enum("skipped", "touched", "provisioned", "failed"),
        "docchat.users.default_project_count": _int(),
        "docchat.access.outcome": _enum("allowed", "not_found", "ok", "unavailable"),
        "docchat.access.via": _enum("admin", "member"),
        "docchat.access.index_source": _enum("user_override", "project_default"),
        "docchat.access.index_cache": _enum("hit", "miss"),
        "docchat.access.project_count": _int(),
        "docchat.opsconfig.outcome": _enum("ok", "partial", "unreachable"),
        "docchat.opsconfig.invalid_areas": _int(),
        "docchat.opsconfig.area": _shaped_enum(OPS_CONFIG_AREA_PATTERN),
        "docchat.opsconfig.key": _shaped_enum(OPS_CONFIG_KEY_PATTERN),
        "docchat.ratelimit.outcome": _enum("allowed", "limited", "unavailable"),
        "docchat.ratelimit.attempts": _int(1, 5),
        "docchat.audit.action": _shaped_enum(AUDIT_ACTION_PATTERN),
        # Ingestion.
        "docchat.ingestion.upload_bytes": _int(),
        "docchat.ingestion.run_kind": ValueSpec(kind=ValueKind.ENUM, values=RUN_KINDS),
        "docchat.ingestion.item_count": _int(),
        "docchat.ingestion.dequeue_count": _int(1),
        "docchat.ingestion.dispatcher_started": _bool(),
        "docchat.ingestion.claim": _int(1),
        "docchat.ingestion.parse_command": _enum(
            "inspect", "convert", "partition", "render", "assemble", "verify-render"
        ),
        "docchat.ingestion.parse_uid": _int(10002, 10017),
        "docchat.ingestion.content_type": ValueSpec(kind=ValueKind.ENUM, values=CONTENT_TYPES),
        "docchat.ingestion.pages": _int(),
        "docchat.ingestion.parse_peak_rss_mb": _int(),
        "docchat.ingestion.parse_exit": _enum("ok", "timeout", "memory", "error"),
        "docchat.ingestion.generation": _int(1),
        "docchat.ingestion.items_requeued": _int(),
        "docchat.ingestion.items_claimed": _int(),
        "docchat.ingestion.runs_finalised": _int(),
        "docchat.ingestion.duty": _enum(
            "heartbeat",
            "expired_leases",
            "scan_results",
            "stale_uploads",
            "orphaned_versions",
            "leftover_cleanup",
            "repoint",
            "due_deletions",
            "finalisation",
            "counts",
            "poison_gauge",
            "scanner_canaries",
            "stale_renditions",
        ),
        "docchat.ingestion.step": _enum(
            "durable_history", "runs", "scanner_canary", "canary_retention"
        ),
        # Chat pipeline.
        "docchat.broadened": _bool(),
        "docchat.rerank_partial": _bool(),
        "docchat.sources_sent": _int(0, 16),
        "docchat.sources_flagged": _int(0, 16),
        "docchat.prompt_tokens": _int(),
        "docchat.completion_tokens": _int(),
        "docchat.reasoning_tokens": _int(),
        "docchat.finish_reason": _enum("stop", "length", "content_filter", "cancelled", "error"),
        "docchat.ttft_ms": _int(),
        "docchat.first_chunk_ms": _int(),
        "docchat.reasoning_effort": _enum("none", "low", "medium", "high"),
        "docchat.dimensions": _int_of(256, 1024, 3072),
        "docchat.pass": _enum("first", "broadened"),
        "docchat.results": _int(),
        "docchat.candidates": _int(),
        "docchat.served": _int(),
        "docchat.calls": _int(),
        "docchat.segments": _int(),
        "docchat.characters": _int(),
        "docchat.flagged": _int(),
        "docchat.dropped_turns": _int(),
        "docchat.dropped_sources": _int(),
        "docchat.operation": _enum("getCitation", "getCitationContent", "getCitationDocument"),
        "docchat.open_as": _enum("pdf_page", "document_pdf"),
        "docchat.citation.number": _int(1, 16),
        "docchat.citation.bytes_sent": _int(),
        "docchat.citation.abort_reason": _enum("slow_client", "duration", "drain"),
        "docchat.chat.turn_tasks": _int(),
        "docchat.chat.background_tasks": _int(),
        "docchat.chat.downloads": _int(),
        "docchat.chat.duration_ms": _int(),
        "docchat.chat.estimated_cost_micros": _int(),
        "docchat.chat.usage_estimated": _bool(),
        # Conversations and memory.
        "docchat.turn_seq": _int(1),
        "docchat.session_new": _bool(),
        "docchat.cosmos.request_charge": _float(),
        "docchat.history.turns_loaded": _int(),
        "docchat.history.tokens_loaded": _int(),
        "docchat.history.unsummarised_tokens": _int(),
        "docchat.history.unsummarised_turns": _int(),
        "docchat.history.stopped_at_budget": _bool(),
        "docchat.memory.active": _bool(),
        "docchat.memory.facts_rendered": _int(),
        "docchat.memory.facts_count": _int(),
        "docchat.memory.proposed": _int(),
        "docchat.memory.accepted": _int(),
        "docchat.memory.shield_calls": _int(),
        **{f"docchat.memory.rejected.{reason}": _int() for reason in MEMORY_REJECTION_REASONS},
        "docchat.summary.due": _bool(),
        "docchat.summary.truncated": _bool(),
        "docchat.summary.folded_turns": _int(),
        "docchat.summary.input_tokens": _int(),
        "docchat.summary.output_tokens": _int(),
        "docchat.maintenance.scheduled": _bool(),
        "docchat.maintenance.summary_ran": _bool(),
        "docchat.maintenance.extractions": _int(),
        "docchat.maintenance.step": _enum("memory_extraction", "summary_rebuild"),
        "docchat.turn.status": _enum("completed", "failed", "cancelled"),
        "docchat.turn.error_code": ValueSpec(kind=ValueKind.ENUM, values=frozenset(CODES)),
        "docchat.trace.bytes": _int(),
        "docchat.followups.count": _int(0, 3),
        "docchat.page.limit": _int(),
        "docchat.page.returned": _int(),
        "docchat.projects.reachable": _int(),
        "docchat.filter.project": _bool(),
        "docchat.feedback.rating": _enum("up", "down"),
        "docchat.feedback.category": _enum(
            "incorrect", "incomplete", "not_relevant", "other", "none"
        ),
        "docchat.feedback.is_change": _bool(),
    }
)
"""Every span and log attribute key of section 3.4 with its type and allowed values."""

_ACTION: Final = _shaped_enum(AUDIT_ACTION_PATTERN)
_TARGET_TYPE: Final = _shaped_enum(AUDIT_TARGET_TYPE_PATTERN)
_DEPLOYMENT: Final = _shaped_enum(DEPLOYMENT_NAME_PATTERN)
_COUNT: Final = _int()

EVENTS: Final[Mapping[str, EventSpec]] = _frozen(
    {
        "user_provisioned": _event(oid=_uuid(), defaultProjectCount=_COUNT),
        "index_override_used": _event(
            oid=_uuid(), projectId=_uuid(), indexId=_uuid(), expiresAt=_timestamp()
        ),
        "admin_change": _event(action=_ACTION, targetType=_TARGET_TYPE, projectId=_uuid()),
        "signing_keys_refresh_failed": _event(
            trigger=_enum("startup", "scheduled", "unknown_kid"),
            statusCode=_int(0, 599),
            errorType=_string(QUALIFIED_TYPE_NAME_PATTERN, max_length=512),
        ),
        "ops_config_invalid": _event(
            area=_shaped_enum(OPS_CONFIG_AREA_PATTERN), key=_shaped_enum(OPS_CONFIG_KEY_PATTERN)
        ),
        "audit_write_failed": _event(
            action=_ACTION,
            actorType=_enum("user", "operator", "system"),
            targetType=_TARGET_TYPE,
            targetId=_string(AUDIT_TARGET_ID_PATTERN),
            sessionId=_uuid(),
            turnId=_uuid(),
        ),
        "source_screening_flagged": _event(
            documentId=_uuid(),
            chunkSetId=_uuid(),
            indexId=_uuid(),
            projectId=_uuid(),
            sessionId=_uuid(),
            turnId=_uuid(),
        ),
        "content_filter_triggered": _event(
            where=_enum("prompt", "completion"),
            call=ValueSpec(kind=ValueKind.ENUM, values=MODEL_CALLS),
            deployment=_DEPLOYMENT,
            projectId=_uuid(),
            sessionId=_uuid(),
            turnId=_uuid(),
        ),
        "content_filter_degraded": _event(
            deployment=_DEPLOYMENT, projectId=_uuid(), sessionId=_uuid(), turnId=_uuid()
        ),
        "turn_feedback_saved": _event(
            rating=_enum("up", "down"),
            category=_enum("incorrect", "incomplete", "not_relevant", "other", "none"),
            projectId=_uuid(),
            turnId=_uuid(),
            sessionId=_uuid(),
            isChange=_bool(),
            hasComment=_bool(),
        ),
        "followups_not_saved": _event(sessionId=_uuid(), turnId=_uuid()),
        "ingestion_run_finished": _event(
            runId=_uuid(),
            kind=ValueSpec(kind=ValueKind.ENUM, values=RUN_KINDS),
            state=ValueSpec(kind=ValueKind.ENUM, values=FINISHED_RUN_STATES),
            itemsTotal=_COUNT,
            itemsSucceeded=_COUNT,
            itemsSkipped=_COUNT,
            itemsFailed=_COUNT,
            itemsCancelled=_COUNT,
            durationSeconds=_float(),
        ),
        "ingestion_item_failed": _event(
            itemId=_uuid(),
            runId=_uuid(),
            operation=ValueSpec(kind=ValueKind.ENUM, values=ITEM_OPERATIONS),
            stage=ValueSpec(kind=ValueKind.ENUM, values=ITEM_STAGES),
            outcomeCode=ValueSpec(kind=ValueKind.ENUM, values=frozenset(ITEM_CODES)),
        ),
        "malware_detected": _event(indexId=_uuid(), documentId=_uuid(), versionId=_uuid()),
        "index_repointed": _event(
            indexId=_uuid(),
            oldPhysicalIndexId=_uuid(),
            newPhysicalIndexId=_uuid(),
            generation=_int(1),
        ),
        "physical_index_deleted": _event(physicalIndexId=_uuid()),
        "ingestion_dispatcher_restarted": _event(
            oldGeneration=_int(0), reason=_enum("heartbeat_stale")
        ),
        "ingestion_queue_send_failed": _event(runId=_uuid()),
        "ingestion_message_invalid": _event(messageId=_string(QUEUE_MESSAGE_ID_PATTERN)),
        "scanner_canary_result": _event(
            result=_enum("clean", "malicious", "failed", "unavailable"), minutes=_float()
        ),
        "ingestion_worker_started": _event(),
    }
)
"""Every custom event of section 4.12 with exactly its properties."""

_C: Final = InstrumentKind.COUNTER
_H: Final = InstrumentKind.HISTOGRAM
_G: Final = InstrumentKind.GAUGE
_OG: Final = InstrumentKind.OBSERVABLE_GAUGE

METRICS: Final[Mapping[str, MetricSpec]] = _frozen(
    {
        # 4.7 Runtime, SQL and telemetry.
        "docchat.runtime.event_loop_lag": _metric(_H, "ms", "service"),
        "docchat.sql.duration": _metric(_H, "ms", "operation", "outcome"),
        "docchat.sql.retries": _metric(_C, "1", "error_number"),
        "docchat.sql.failures": _metric(_C, "1", "error_number", "reason"),
        "docchat.sql.pool.wait": _metric(_H, "ms", "service"),
        "docchat.sql.pool.in_use": _metric(_OG, "{connection}", "service"),
        "docchat.sql.idle_releases": _metric(_C, "1", "service"),
        "docchat.telemetry.scrub_errors": _metric(_C, "1", "service"),
        "docchat.telemetry.dropped_attributes": _metric(_C, "1", "service"),
        # 4.8 Identity and access.
        "docchat.auth.validations": _metric(_C, "1", "outcome", "reason", "client"),
        "docchat.auth.signing_key_fetches": _metric(_C, "1", "trigger", "outcome"),
        "docchat.auth.signing_key_age": _metric(_OG, "s"),
        "docchat.access.denials": _metric(_C, "1", "kind", "operation"),
        "docchat.users.provisioned": _metric(_C, "1"),
        "docchat.api.problems": _metric(_C, "1", "code", "status", "operation"),
        "docchat.config.invalid": _metric(_C, "1", "area", "key"),
        "docchat.opsconfig.refresh_failures": _metric(_C, "1", "reason"),
        "docchat.access.index_overrides_used": _metric(_C, "1"),
        "docchat.ratelimit.rejections": _metric(_C, "1", "reason"),
        "docchat.audit.write_failures": _metric(_C, "1", "action"),
        # 4.9 Chat pipeline.
        "docchat.chat.time_to_first_token": _metric(_H, "ms", "broadened", "deployment"),
        "docchat.chat.turn.duration": _metric(_H, "ms", "outcome"),
        "docchat.chat.turns": _metric(_C, "1", "outcome", "error_code"),
        "docchat.chat.broadened_searches": _metric(_C, "1", "outcome"),
        "docchat.chat.screening.flagged_sources": _metric(_C, "1"),
        "docchat.chat.screening.failures": _metric(_C, "1", "reason"),
        "docchat.chat.prompt.tokens": _metric(_H, "{token}"),
        "docchat.chat.prompt.dropped": _metric(_C, "1", "kind"),
        "docchat.chat.disconnects": _metric(_C, "1", "phase"),
        "docchat.chat.heartbeats": _metric(_C, "1"),
        "docchat.chat.drained_turns": _metric(_C, "1"),
        "docchat.openai.duration": _metric(_H, "ms", "deployment", "call", "outcome"),
        "docchat.openai.first_chunk_latency": _metric(_H, "ms", "deployment"),
        "docchat.openai.throttled": _metric(_C, "1", "deployment", "call"),
        "docchat.openai.tokens": _metric(_C, "{token}", "deployment", "call", "kind"),
        "docchat.openai.estimated_cost": _metric(_C, "{micro_unit}", "deployment", "model"),
        "docchat.search.duration": _metric(_H, "ms", "pass"),
        "docchat.search.rerank_partial": _metric(_C, "1"),
        "docchat.citation.requests": _metric(_C, "1", "operation", "outcome"),
        "docchat.citation.download_aborts": _metric(_C, "1", "reason"),
        "docchat.usage.record_failures": _metric(_C, "1"),
        "docchat.pricing.rate_missing": _metric(_C, "1", "model"),
        # 4.10 Conversations and memory.
        "docchat.conversations.turns_finalized": _metric(_C, "1", "status", "error_code"),
        "docchat.conversations.duplicates": _metric(_C, "1", "outcome"),
        "docchat.conversations.abandoned": _metric(_C, "1", "source"),
        "docchat.conversations.finalize_superseded": _metric(_C, "1"),
        "docchat.conversations.trace_truncated": _metric(_C, "1"),
        "docchat.conversations.followups_not_saved": _metric(_C, "1"),
        "docchat.conversations.store_conflicts": _metric(_C, "1", "operation"),
        "docchat.conversations.request_charge": _metric(_H, "{RU}", "operation"),
        "docchat.summary.rebuilds": _metric(_C, "1", "outcome"),
        "docchat.summary.duration": _metric(_H, "ms", "outcome"),
        "docchat.summary.truncated": _metric(_C, "1"),
        "docchat.summary.unsummarised_tokens": _metric(_H, "{token}"),
        "docchat.memory.extractions": _metric(_C, "1", "outcome"),
        "docchat.memory.fact_rejections": _metric(_C, "1", "reason"),
        "docchat.memory.facts_changed": _metric(_C, "1", "action"),
        "docchat.memory.user_changes": _metric(_C, "1", "action"),
        "docchat.memory.read_failures": _metric(_C, "1"),
        "docchat.memory.fact_screenings": _metric(_C, "1", "source", "outcome"),
        "docchat.feedback.saved": _metric(_C, "1", "rating", "category"),
        # 4.11 Ingestion.
        "docchat.ingestion.items.completed": _metric(_C, "1", "operation", "state", "outcome_code"),
        "docchat.ingestion.item.duration": _metric(_H, "s", "operation", "state"),
        "docchat.ingestion.stage.duration": _metric(_H, "s", "stage", "method_key"),
        "docchat.ingestion.items.queued": _metric(_G, "1", "priority"),
        "docchat.ingestion.items.running": _metric(_G, "1"),
        "docchat.ingestion.queue.oldest_age": _metric(_G, "s"),
        "docchat.ingestion.dispatcher.heartbeat_age": _metric(_G, "s"),
        "docchat.ingestion.poison_queue.length": _metric(_G, "{message}"),
        "docchat.ingestion.tokens": _metric(_C, "{token}", "deployment", "kind"),
        "docchat.ingestion.docintel.pages": _metric(_C, "{page}"),
        "docchat.ingestion.throttled": _metric(_C, "1", "service", "deployment"),
        "docchat.ingestion.scan.results": _metric(_C, "1", "result"),
        "docchat.ingestion.parse.peak_rss": _metric(_H, "MBy", "parse_command"),
        "docchat.ingestion.convert.duration": _metric(_H, "s", "content_type", "outcome"),
        "docchat.ingestion.convert.pages": _metric(_H, "{page}", "content_type"),
        "docchat.uploads.completed": _metric(_C, "1", "outcome", "error_code"),
        "docchat.uploads.size": _metric(_H, "By", "content_type"),
    }
)
"""Every metric of sections 4.7 to 4.11 with its kind, unit and dimension keys."""

DIMENSIONS: Final = frozenset(
    {
        "outcome",
        "reason",
        "deployment",
        "call",
        "kind",
        "state",
        "stage",
        "operation",
        "status",
        "error_code",
        "phase",
        "client",
        "trigger",
        "area",
        "key",
        "service",
        "content_type",
        "method_key",
        "priority",
        "result",
        "action",
        "rating",
        "category",
        "pass",
        "broadened",
        "model",
        "error_number",
        "outcome_code",
        "code",
        "source",
        "parse_command",
    }
)
"""The closed set of metric dimension keys (section 3.5); no identifier is among them."""


def span_spec(name: str) -> SpanSpec | None:
    """The registered spec of a first-party span name, or `None` when it isn't registered."""
    spec = SPANS.get(name)
    if spec is not None:
        return spec
    if name.startswith(SQL_SPAN_PREFIX) and _STATEMENT_SET_RE.fullmatch(
        name.removeprefix(SQL_SPAN_PREFIX)
    ):
        return SPANS[SQL_SPAN_FAMILY]
    return None


def is_event_name(name: str) -> bool:
    """A record or event name: a static snake_case word."""
    return _EVENT_NAME_RE.fullmatch(name) is not None


def is_camel_case(name: str) -> bool:
    return re.fullmatch(CAMEL_CASE_PATTERN, name) is not None


def value_spec(key: str, narrowed: Mapping[str, frozenset[str]] | None = None) -> ValueSpec | None:
    """The spec a value of `key` must meet, narrowed by a span's or record's own value set."""
    spec = KEYS.get(key)
    if spec is None:
        return None
    if narrowed is not None and key in narrowed:
        return spec.narrowed(narrowed[key])
    return spec
