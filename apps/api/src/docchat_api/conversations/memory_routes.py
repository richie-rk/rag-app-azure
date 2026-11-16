"""The memory operations: `getMemory`, `updateMemorySettings`, `updateMemoryFact`,
`deleteMemoryFact` and `clearMemory` (`13-conversations-and-memory.md` sections 4.1, 7.1 to 7.9
and 7.16; `11-api.md` sections 1.9, 3.6 and 4.6; `82-security.md` SEC-18, SEC-29 and SEC-42).

Each acts on the caller's own `memory` document (partition key and `id` both the caller's `oid`);
administrators reach no one else's. `getMemory` creates the document empty when it doesn't exist,
so a client holds an `etag` before its first write. The four writes share these steps, after the
route class's checks (the burst and write limits among them) and body validation:

1. read the document;
2. `updateMemoryFact` and `deleteMemoryFact`: a missing document or fact answers
   `404 memory_fact_not_found`, whatever `If-Match` holds;
3. `If-Match` holds exactly one strong tag (`428 precondition_required` otherwise) equal to the
   document's `_etag`, quotes included (`412 precondition_failed` otherwise; with no document a
   well-formed tag can't match); the Linux emulator's bare-GUID `_etag` counts in its quoted
   form (`entity_tag`), which is also the `etag` the operations return;
4. the operation's own checks; an unchanged value answers `200` with no write and no audit row;
5. `replace_item` conditioned on the same ETag, so the check and the write are one atomic
   condition (a `412` there is a change between the read and the write: `412`);
6. after the commit, the audit row through `write_audit_event_after_commit` (which reports a row
   it can't write instead of failing the committed change) and `docchat.memory.user_changes`;
   then `200` with the new view and its new `etag`.

An edited fact is normalised to its stored form, checked by the fact validators against every
other fact and by the token cap (`422` at `body.text`), and, when its text changed, screened by
Prompt Shields (`422 contains_instruction`; `503` when screening is unavailable). The fact then
becomes the user's own (`source.kind` `user`), which extraction never replaces.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi import Path as PathParam
from pydantic import Field, StrictBool

from docchat_api.chat.screening import ScreeningUnavailable, shield_user_prompt
from docchat_api.conversations.store import StoredMemory, problem_on_failure
from docchat_api.conversations.telemetry import ChargeOperation, Charges
from docchat_api.http.access import access
from docchat_api.http.audit import write_audit_after_commit
from docchat_api.http.models import RequestModel, ResponseModel
from docchat_api.http.preconditions import (
    IfMatchHeader,
    PreconditionFailed,
    PreconditionRequired,
    entity_tag,
    if_match_header,
)
from docchat_api.http.problems import DependencyUnavailable, ValidationFailed
from docchat_api.http.routing import CurrentPrincipal, DocChatRoute
from docchat_api.http.state import ApiProcess, current_process, request_ops
from docchat_api.tokens import count_tokens
from docchat_core.audit import AuditAction, AuditEvent, AuditMetadata, AuditTargetType
from docchat_core.audit.models import (
    MemoryClearedMetadata,
    MemoryFactDeletedMetadata,
    MemoryFactUpdatedMetadata,
)
from docchat_core.auth import Principal
from docchat_core.cosmos.memories import FACTS_MAX, Fact, FactSourceKind, MemoryDocument
from docchat_core.cosmos.memories import FactSource as StoredFactSource
from docchat_core.errors import DocChatProblem, FieldIssue
from docchat_core.memory import (
    CATEGORY_ORDER,
    FACT_MAX_CODE_POINTS,
    FactCategory,
    stored_form,
    validate_fact_text,
)
from docchat_core.models import CanonicalUuid, Count, UtcDateTime
from docchat_core.opsconfig import OpsConfig
from docchat_core.sql.engine import SqlTimeout, SqlUnavailable
from docchat_core.telemetry import set_attributes, start_span
from docchat_core.telemetry.instruments import counter

router = APIRouter(route_class=DocChatRoute)

TAG: Final = "memory"
MEMORY_PATH: Final = "/memory"
FACTS_PATH: Final = "/memory/facts"
FACT_PATH: Final = "/memory/facts/{factId}"
EDITED_TEXT_MAX_CODE_POINTS: Final = 800
"""An edited fact as sent: four times the fact limit, room for the whitespace normalisation
collapses."""
ETAG_MAX_LENGTH: Final = 128
TOKENS_MAX: Final = 4_000
TEXT_FIELD: Final = "body.text"

SETTINGS_EXAMPLE: Final[dict[str, Any]] = {"paused": True}
FACT_EXAMPLE: Final[dict[str, Any]] = {
    "text": "Works on supplier contract renewals",
    "category": "topic",
}
"""The OpenAPI request examples of `11-api.md` sections 3.6.2 and 3.6.3."""

USER_CHANGES: Final = counter("docchat.memory.user_changes")
FACT_SCREENINGS: Final = counter("docchat.memory.fact_screenings")

type MemoryOutcome = Literal[
    "ok",
    "unchanged",
    "precondition_failed",
    "precondition_required",
    "not_found",
    "invalid",
    "unavailable",
]
type UserChange = Literal["paused", "resumed", "fact_updated", "fact_deleted", "cleared"]


class MemoryFactNotFound(DocChatProblem):
    """`404 memory_fact_not_found`: no such fact in the caller's memory, or no memory at all."""

    code = "memory_fact_not_found"


FactIdPath = Annotated[CanonicalUuid, PathParam(alias="factId")]


# --- Request and response models ---------------------------------------------------------------


class MemorySettingsRequest(RequestModel):
    paused: StrictBool


class MemoryFactRequest(RequestModel):
    text: Annotated[str, Field(max_length=EDITED_TEXT_MAX_CODE_POINTS)]
    category: FactCategory


class FactSource(ResponseModel):
    """Where a fact came from: the session of an extracted fact; `null` for the user's own."""

    kind: FactSourceKind
    session_id: UUID | None


class FactView(ResponseModel):
    fact_id: UUID
    text: Annotated[str, Field(min_length=1, max_length=FACT_MAX_CODE_POINTS)]
    category: FactCategory
    source: FactSource
    created_at: UtcDateTime
    updated_at: UtcDateTime


class MemoryLimits(ResponseModel):
    max_facts: Annotated[int, Field(ge=1, le=FACTS_MAX)]
    max_fact_chars: Literal[200]
    max_tokens: Annotated[int, Field(ge=100, le=TOKENS_MAX)]
    used_facts: Annotated[int, Field(ge=0, le=FACTS_MAX)]
    used_tokens: Count


class MemoryView(ResponseModel):
    """The caller's memory as every memory operation answers it; `etag` is the `If-Match` value
    of the next write."""

    enabled: bool
    paused: bool
    facts: Annotated[list[FactView], Field(max_length=FACTS_MAX)]
    limits: MemoryLimits
    etag: Annotated[str, Field(min_length=1, max_length=ETAG_MAX_LENGTH)]
    updated_at: UtcDateTime


def ordered_facts(facts: Sequence[Fact]) -> list[Fact]:
    """By category (`role`, `team`, `expertise`, `topic`, `answer_style`), then newest first."""
    newest_first = sorted(facts, key=lambda fact: fact.updated_at, reverse=True)
    return sorted(newest_first, key=lambda fact: CATEGORY_ORDER.index(fact.category))


def memory_view(stored: StoredMemory, ops: OpsConfig) -> MemoryView:
    document = stored.document
    facts = ordered_facts(document.facts)
    return MemoryView(
        enabled=ops.memory.enabled,
        paused=document.paused,
        facts=[
            FactView(
                fact_id=fact.id,
                text=fact.text,
                category=fact.category,
                source=FactSource(kind=fact.source.kind, session_id=fact.source.session_id),
                created_at=fact.created_at,
                updated_at=fact.updated_at,
            )
            for fact in facts
        ],
        limits=MemoryLimits(
            max_facts=ops.memory.max_facts,
            max_fact_chars=FACT_MAX_CODE_POINTS,
            max_tokens=ops.memory.max_tokens,
            used_facts=len(facts),
            used_tokens=sum(fact.tokens for fact in facts),
        ),
        etag=entity_tag(stored.etag),
        updated_at=document.updated_at,
    )


# --- Telemetry ---------------------------------------------------------------------------------


@dataclass(slots=True)
class MemoryCall:
    """What one memory operation's span records besides its request charge."""

    outcome: MemoryOutcome | None = "ok"
    facts_count: int | None = None


def outcome_of(error: BaseException) -> MemoryOutcome | None:
    """The outcome a refusal records; `None` for a defect, whose `internal_error` the failure
    boundary records."""
    if isinstance(error, PreconditionFailed):
        return "precondition_failed"
    if isinstance(error, PreconditionRequired):
        return "precondition_required"
    if isinstance(error, MemoryFactNotFound):
        return "not_found"
    if isinstance(error, ValidationFailed):
        return "invalid"
    if isinstance(error, DependencyUnavailable | SqlUnavailable | SqlTimeout):
        return "unavailable"
    return None


@contextmanager
def memory_span(name: str, charges: Charges, operation: ChargeOperation) -> Iterator[MemoryCall]:
    """The operation's span: its outcome, the facts the document holds afterwards and the
    request charge; never fact text."""
    call = MemoryCall()
    with start_span(name) as span:
        try:
            yield call
        except BaseException as error:
            call.outcome = outcome_of(error)
            raise
        finally:
            attributes: dict[str, object] = {
                "docchat.cosmos.request_charge": charges.record(operation)
            }
            if call.outcome is not None:
                attributes["docchat.outcome"] = call.outcome
            if call.facts_count is not None:
                attributes["docchat.memory.facts_count"] = call.facts_count
            set_attributes(span, **attributes)


# --- Shared steps ------------------------------------------------------------------------------


async def read_memory(
    process: ApiProcess, principal: Principal, charges: Charges
) -> StoredMemory | None:
    """Step 1: the caller's document, `None` when there is none yet."""
    with problem_on_failure():
        return await process.conversations.read_memory(principal.oid, charges)


def current(stored: StoredMemory | None, if_match: IfMatchHeader) -> StoredMemory:
    """Step 3: `428` for a missing or malformed `If-Match`, `412` for another version."""
    if stored is None:
        if_match.require()
        raise PreconditionFailed()
    if_match.require_etag(stored.etag)
    return stored


def find_fact(stored: StoredMemory | None, fact_id: UUID) -> tuple[StoredMemory, Fact]:
    """Step 2: the fact, or `404 memory_fact_not_found` before `If-Match` is looked at."""
    if stored is not None:
        for fact in stored.document.facts:
            if fact.id == fact_id:
                return stored, fact
    raise MemoryFactNotFound()


async def replace(
    process: ApiProcess, document: MemoryDocument, etag: str, charges: Charges
) -> StoredMemory:
    """Step 5: the conditioned replace; a change since the read answers `412`."""
    with problem_on_failure():
        written = await process.conversations.replace_memory(document, etag, charges)
    if written is None:
        raise PreconditionFailed()
    return written


async def after_commit(
    request: Request,
    process: ApiProcess,
    principal: Principal,
    *,
    change: UserChange,
    action: AuditAction,
    target_type: AuditTargetType,
    target_id: UUID,
    metadata: AuditMetadata | None = None,
) -> None:
    """Step 6: the audit row of the committed change and the user-change count."""
    event = AuditEvent(
        action=action,
        actor_type="user",
        actor_object_id=principal.oid,
        subject_object_id=principal.oid,
        target_type=target_type,
        target_id=str(target_id),
        project_id=None,
        metadata=metadata,
    )
    await write_audit_after_commit(request, process.sql, event)
    USER_CHANGES.add(1, action=change)


def invalid_text(reason: str) -> ValidationFailed:
    return ValidationFailed(errors=[FieldIssue(TEXT_FIELD, reason)])


async def screen(text: str) -> None:
    """Prompt Shields on an edited fact's new text: an attack is `contains_instruction`;
    screening unavailable after its retry is `503`, with nothing written."""
    try:
        flagged = await shield_user_prompt(text)
    except ScreeningUnavailable:
        FACT_SCREENINGS.add(1, source="user", outcome="unavailable")
        raise DependencyUnavailable() from None
    FACT_SCREENINGS.add(1, source="user", outcome="flagged" if flagged else "passed")
    if flagged:
        raise invalid_text("contains_instruction")


# --- The operations ----------------------------------------------------------------------------

Process = Annotated[ApiProcess, Depends(current_process)]
Ops = Annotated[OpsConfig, Depends(request_ops)]
IfMatch = Annotated[IfMatchHeader, Depends(if_match_header)]


@router.get(MEMORY_PATH, operation_id="getMemory", tags=[TAG], response_model=MemoryView)
@access("user")
async def get_memory(principal: CurrentPrincipal, process: Process, ops: Ops) -> MemoryView:
    charges = Charges()
    with memory_span("docchat.memory.get", charges, "memory_read") as call:
        with problem_on_failure():
            stored = await process.conversations.ensure_memory(principal.oid, charges)
        call.facts_count = len(stored.document.facts)
    return memory_view(stored, ops)


@router.patch(
    MEMORY_PATH,
    operation_id="updateMemorySettings",
    tags=[TAG],
    response_model=MemoryView,
    openapi_extra={"requestBody": {"content": {"application/json": {"example": SETTINGS_EXAMPLE}}}},
)
@access("user", if_match=True)
async def update_memory_settings(
    *,
    request: Request,
    body: MemorySettingsRequest,
    principal: CurrentPrincipal,
    process: Process,
    ops: Ops,
    if_match: IfMatch,
) -> MemoryView:
    charges = Charges()
    with memory_span("docchat.memory.update_settings", charges, "memory_write") as call:
        stored = current(await read_memory(process, principal, charges), if_match)
        document = stored.document
        call.facts_count = len(document.facts)
        if document.paused == body.paused:
            call.outcome = "unchanged"
            return memory_view(stored, ops)
        now = process.conversations.clock.now()
        written = await replace(
            process,
            document.model_copy(
                update={
                    "paused": body.paused,
                    "paused_at": now if body.paused else document.paused_at,
                    "user_edited_at": now,
                    "updated_at": now,
                }
            ),
            stored.etag,
            charges,
        )
    await after_commit(
        request,
        process,
        principal,
        change="paused" if body.paused else "resumed",
        action="memory.paused" if body.paused else "memory.resumed",
        target_type="memory",
        target_id=principal.oid,
    )
    return memory_view(written, ops)


@router.put(
    FACT_PATH,
    operation_id="updateMemoryFact",
    tags=[TAG],
    response_model=MemoryView,
    openapi_extra={"requestBody": {"content": {"application/json": {"example": FACT_EXAMPLE}}}},
)
@access("user", scoped_params=("factId",), error_codes=["memory_fact_not_found"], if_match=True)
async def update_memory_fact(
    *,
    request: Request,
    fact_id: FactIdPath,
    body: MemoryFactRequest,
    principal: CurrentPrincipal,
    process: Process,
    ops: Ops,
    if_match: IfMatch,
) -> MemoryView:
    charges = Charges()
    with memory_span("docchat.memory.update_fact", charges, "memory_write") as call:
        stored, fact = find_fact(await read_memory(process, principal, charges), fact_id)
        current(stored, if_match)
        document = stored.document
        call.facts_count = len(document.facts)
        others = [other for other in document.facts if other.id != fact_id]
        text = stored_form(body.text)
        reason = validate_fact_text(
            text, body.category, [(other.text, other.category) for other in others]
        )
        if reason is not None:
            raise invalid_text(reason)
        tokens = await count_tokens(process.conversations.tokenizer, text)
        if sum(other.tokens for other in others) + tokens > ops.memory.max_tokens:
            raise invalid_text("memory_full")
        if text == fact.text and body.category == fact.category:
            call.outcome = "unchanged"
            return memory_view(stored, ops)
        if text != fact.text:
            await screen(text)
        now = process.conversations.clock.now()
        edited = fact.model_copy(
            update={
                "text": text,
                "category": body.category,
                "tokens": tokens,
                "source": StoredFactSource(kind=FactSourceKind.USER),
                "updated_at": now,
            }
        )
        written = await replace(
            process,
            document.model_copy(
                update={
                    "facts": [edited if item.id == fact_id else item for item in document.facts],
                    "user_edited_at": now,
                    "updated_at": now,
                }
            ),
            stored.etag,
            charges,
        )
    await after_commit(
        request,
        process,
        principal,
        change="fact_updated",
        action="memory.fact_updated",
        target_type="memory_fact",
        target_id=fact_id,
        metadata=MemoryFactUpdatedMetadata(
            category=body.category.value,
            previous_category=fact.category.value,
            previous_source_kind=fact.source.kind.value,
        ),
    )
    return memory_view(written, ops)


@router.delete(FACT_PATH, operation_id="deleteMemoryFact", tags=[TAG], response_model=MemoryView)
@access("user", scoped_params=("factId",), error_codes=["memory_fact_not_found"], if_match=True)
async def delete_memory_fact(
    *,
    request: Request,
    fact_id: FactIdPath,
    principal: CurrentPrincipal,
    process: Process,
    ops: Ops,
    if_match: IfMatch,
) -> MemoryView:
    charges = Charges()
    with memory_span("docchat.memory.delete_fact", charges, "memory_write") as call:
        stored, fact = find_fact(await read_memory(process, principal, charges), fact_id)
        current(stored, if_match)
        document = stored.document
        call.facts_count = len(document.facts)
        now = process.conversations.clock.now()
        written = await replace(
            process,
            document.model_copy(
                update={
                    "facts": [item for item in document.facts if item.id != fact_id],
                    "user_edited_at": now,
                    "updated_at": now,
                }
            ),
            stored.etag,
            charges,
        )
        call.facts_count = len(written.document.facts)
    await after_commit(
        request,
        process,
        principal,
        change="fact_deleted",
        action="memory.fact_deleted",
        target_type="memory_fact",
        target_id=fact_id,
        metadata=MemoryFactDeletedMetadata(
            category=fact.category.value, source_kind=fact.source.kind.value
        ),
    )
    return memory_view(written, ops)


@router.delete(FACTS_PATH, operation_id="clearMemory", tags=[TAG], response_model=MemoryView)
@access("user", if_match=True)
async def clear_memory(
    request: Request,
    principal: CurrentPrincipal,
    process: Process,
    ops: Ops,
    if_match: IfMatch,
) -> MemoryView:
    charges = Charges()
    with memory_span("docchat.memory.clear", charges, "memory_write") as call:
        stored = current(await read_memory(process, principal, charges), if_match)
        document = stored.document
        removed = len(document.facts)
        call.facts_count = removed
        if not removed:
            call.outcome = "unchanged"
            return memory_view(stored, ops)
        now = process.conversations.clock.now()
        written = await replace(
            process,
            document.model_copy(
                update={"facts": [], "cleared_at": now, "user_edited_at": now, "updated_at": now}
            ),
            stored.etag,
            charges,
        )
        call.facts_count = 0
    await after_commit(
        request,
        process,
        principal,
        change="cleared",
        action="memory.cleared",
        target_type="memory",
        target_id=principal.oid,
        metadata=MemoryClearedMetadata(facts_removed=removed),
    )
    return memory_view(written, ops)
