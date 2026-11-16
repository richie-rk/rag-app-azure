"""The `memory` document of the `memories` container and its one nested type, `Fact`
(`80-data-model.md` section 3.8; `13-conversations-and-memory.md` sections 7.1 and 7.2).

One document per user, `id` = `userId`; the container holds nothing else. Every access is a point
read or a create or replace conditioned on the document's `_etag`, which the API returns as the
memory's `etag`. The duplicate rule between fact texts and the token caps need the fact
validators and the ops config, so the memory operations apply them; this model holds the fixed
limits. The categories and the length limit of a fact's text are `docchat_core.memory`'s, which
owns them.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, ClassVar, Final, Literal, Self
from uuid import UUID

from pydantic import AfterValidator, Field, StringConstraints, model_validator

from docchat_core.cosmos.base import CosmosDocument, CosmosModel
from docchat_core.cosmos.conversations.turn import SCHEMA_VERSION
from docchat_core.memory.categories import FactCategory
from docchat_core.memory.validators import FACT_MAX_CODE_POINTS
from docchat_core.models import CanonicalUuid, UtcDateTime

FACTS_MAX: Final = 200
"""The ceiling of `memory.max_facts`; the memory operations apply the configured value."""
MEMORY_MAX_BYTES: Final = 200_000
_RANDOM_UUID_VERSION: Final = 4


class FactSourceKind(StrEnum):
    EXTRACTED = "extracted"
    """Written by extraction; a later extraction's `replace` may change it."""
    USER = "user"
    """Written or rewritten by the user; extraction never replaces it."""


def _random_uuid(value: UUID) -> UUID:
    if value.version != _RANDOM_UUID_VERSION:
        raise ValueError("a fact ID is a version-4 UUID")
    return value


class FactSource(CosmosModel):
    """Where a fact came from: the session and turn of an extracted fact, nothing for a user's."""

    kind: FactSourceKind
    session_id: CanonicalUuid | None = None
    turn_id: CanonicalUuid | None = None

    @model_validator(mode="after")
    def _turn_only_when_extracted(self) -> Self:
        named = (self.session_id is not None, self.turn_id is not None)
        expected = (True, True) if self.kind is FactSourceKind.EXTRACTED else (False, False)
        if named != expected:
            raise ValueError("an extracted fact names its session and turn; a user's names none")
        return self


class Fact(CosmosModel):
    """One remembered fact, its text in the stored form (NFC, whitespace collapsed)."""

    id: Annotated[CanonicalUuid, AfterValidator(_random_uuid)]
    text: Annotated[str, StringConstraints(min_length=1, max_length=FACT_MAX_CODE_POINTS)]
    category: FactCategory
    tokens: Annotated[int, Field(ge=1)]
    source: FactSource
    created_at: UtcDateTime
    updated_at: UtcDateTime


class MemoryDocument(CosmosDocument):
    """A user's memory: the pause switch, the facts and the times of the user's operations."""

    max_bytes: ClassVar[int] = MEMORY_MAX_BYTES

    id: CanonicalUuid
    user_id: CanonicalUuid
    type: Literal["memory"] = "memory"
    schema_version: Literal[1] = SCHEMA_VERSION
    paused: bool = False
    paused_at: UtcDateTime | None = None
    facts: Annotated[list[Fact], Field(max_length=FACTS_MAX)] = Field(default_factory=list)
    user_edited_at: UtcDateTime | None = None
    cleared_at: UtcDateTime | None = None
    created_at: UtcDateTime
    updated_at: UtcDateTime

    @classmethod
    def empty(cls, user_id: UUID, now: datetime) -> MemoryDocument:
        """The document as the first `getMemory` or extraction creates it."""
        return cls(id=user_id, user_id=user_id, created_at=now, updated_at=now)

    @model_validator(mode="after")
    def _identity(self) -> Self:
        if self.id != self.user_id:
            raise ValueError("a memory document's id is its userId")
        ids = [fact.id for fact in self.facts]
        if len(set(ids)) != len(ids):
            raise ValueError("each fact ID appears once")
        return self
