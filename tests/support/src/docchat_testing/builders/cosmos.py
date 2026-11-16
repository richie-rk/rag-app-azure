"""Builders of every Cosmos DB document type (`80-data-model.md` section 3;
`13-conversations-and-memory.md`; `12-chat-pipeline.md` section 3).

Each builder returns a validated document model with valid defaults: fresh user and session IDs
(the partition keys, so tests stay apart), IDs derived as production derives them, canary text
for every content field and times of now. Overrides use the models' Python field names and pass
through the models' validation, so a builder never produces a document the stores would refuse.
Write one with `container.create_item(document.to_item())`.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID, uuid4

from docchat_core.cosmos.conversations.ids import turn_id, turn_trace_id
from docchat_core.cosmos.conversations.session import SessionDocument
from docchat_core.cosmos.conversations.trace import RewriteOutcome, TurnTraceDocument
from docchat_core.cosmos.conversations.turn import (
    CitationContentType,
    FinishReason,
    StoredCitation,
    TurnDocument,
    TurnStatus,
)
from docchat_core.cosmos.memories import (
    Fact,
    FactSource,
    FactSourceKind,
    MemoryDocument,
)
from docchat_core.cosmos.usage import (
    DOWNLOAD_LEASE_SECONDS,
    DownloadLease,
    DownloadRateWindow,
    RateWindow,
    StreamLease,
    UsageDayCounter,
    UsageMonthCounter,
    WriteRateWindow,
    day_counter_id,
    download_lease_id,
    download_rate_window_id,
    epoch_minute,
    month_counter_id,
    rate_window_id,
    stream_lease_id,
    write_rate_window_id,
)
from docchat_core.memory import FactCategory
from docchat_testing.builders.canaries import canary

TURN_DEADLINE: Final = timedelta(seconds=180)
"""`chat.max_turn_seconds` at its default."""
STREAM_LEASE_TTL_SECONDS: Final = 210
"""A stream lease lives the turn's deadline plus its 30-second offset."""


def cosmos_now() -> datetime:
    """Now in UTC, to the millisecond the stored times keep."""
    now = datetime.now(UTC)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


def new_trace_id() -> str:
    """A W3C trace ID: 32 lower-case hex digits, not all zero."""
    return secrets.token_hex(16)


def session_document(
    *, user_id: UUID | None = None, session_id: UUID | None = None, **overrides: Any
) -> SessionDocument:
    """A session with one turn and no turn in progress."""
    session = uuid4() if session_id is None else session_id
    now = cosmos_now()
    values: dict[str, Any] = {
        "id": session,
        "user_id": uuid4() if user_id is None else user_id,
        "session_id": session,
        "project_id": uuid4(),
        "title": canary("title"),
        "created_at": now,
        "updated_at": now,
        "last_turn_at": now,
        "turn_count": 1,
    }
    return SessionDocument.model_validate(values | overrides)


def turn_document(
    *,
    user_id: UUID | None = None,
    session_id: UUID | None = None,
    client_turn_id: UUID | None = None,
    **overrides: Any,
) -> TurnDocument:
    """A completed first turn with a canary question and answer and no citations."""
    session = uuid4() if session_id is None else session_id
    client_turn = uuid4() if client_turn_id is None else client_turn_id
    now = cosmos_now()
    values: dict[str, Any] = {
        "id": turn_id(session, client_turn),
        "user_id": uuid4() if user_id is None else user_id,
        "session_id": session,
        "seq": 1,
        "client_turn_id": client_turn,
        "project_id": uuid4(),
        "status": TurnStatus.COMPLETED,
        "question": canary("question"),
        "answer": canary("answer"),
        "finish_reason": FinishReason.STOP,
        "trace_id": new_trace_id(),
        "deadline_at": now + TURN_DEADLINE,
        "created_at": now,
        "ended_at": now,
        "updated_at": now,
    }
    return TurnDocument.model_validate(values | overrides)


def stored_citation(
    *,
    number: int = 1,
    document_id: UUID | None = None,
    document_version_id: UUID | None = None,
    chunk_set_id: UUID | None = None,
    search_index_id: UUID | None = None,
    **overrides: Any,
) -> StoredCitation:
    """Citation `number` of a PDF's page 1, with a canary title and heading; the IDs that locate
    the cited chunk are fresh unless given, so a test names the rows it laid out."""
    document = uuid4() if document_id is None else document_id
    chunk_set = uuid4() if chunk_set_id is None else chunk_set_id
    values: dict[str, Any] = {
        "number": number,
        "title": canary("title"),
        "page_number": 1,
        "heading_path": [canary("title")],
        "content_type": CitationContentType.PDF,
        "open_as": "pdf_page",
        "source_id": f"S{number}",
        "chunk_key": f"{document}_{chunk_set}_0",
        "search_index_id": uuid4() if search_index_id is None else search_index_id,
        "physical_index_id": uuid4(),
        "document_id": document,
        "document_version_id": uuid4() if document_version_id is None else document_version_id,
        "chunk_set_id": chunk_set,
    }
    return StoredCitation.model_validate(values | overrides)


def turn_trace_document(
    *,
    user_id: UUID | None = None,
    session_id: UUID | None = None,
    turn: UUID | None = None,
    **overrides: Any,
) -> TurnTraceDocument:
    """The trace of a turn whose question needed no rewrite, with canary queries."""
    of_turn = uuid4() if turn is None else turn
    values: dict[str, Any] = {
        "id": turn_trace_id(of_turn),
        "user_id": uuid4() if user_id is None else user_id,
        "session_id": uuid4() if session_id is None else session_id,
        "turn_id": of_turn,
        "rewrite_outcome": RewriteOutcome.SKIPPED,
        "standalone_query": canary("question"),
        "search_query": canary("question"),
        "created_at": cosmos_now(),
    }
    return TurnTraceDocument.model_validate(values | overrides)


def fact(**overrides: Any) -> Fact:
    """A user-written memory fact with canary text."""
    now = cosmos_now()
    values: dict[str, Any] = {
        "id": uuid4(),
        "text": canary("fact"),
        "category": FactCategory.TOPIC,
        "tokens": 8,
        "source": FactSource(kind=FactSourceKind.USER),
        "created_at": now,
        "updated_at": now,
    }
    return Fact.model_validate(values | overrides)


def memory_document(*, user_id: UUID | None = None, **overrides: Any) -> MemoryDocument:
    """A user's memory, not paused, holding one fact."""
    user = uuid4() if user_id is None else user_id
    now = cosmos_now()
    values: dict[str, Any] = {
        "id": user,
        "user_id": user,
        "facts": [fact()],
        "created_at": now,
        "updated_at": now,
    }
    return MemoryDocument.model_validate(values | overrides)


def memory_if_match(written: Mapping[str, Any]) -> str:
    """The `If-Match` value naming a memory document a test wrote directly, as the API's `etag`
    shows it and a client sends it (`11-api.md` section 1.9): the `_etag` verbatim when it
    starts and ends with `"` (Azure Cosmos DB's), otherwise in double quotes (the Linux
    emulator's `_etag` is a bare GUID)."""
    etag = str(written["_etag"])
    if len(etag) > 1 and etag.startswith('"') and etag.endswith('"'):
        return etag
    return f'"{etag}"'


def usage_day_counter(*, user_id: UUID | None = None, **overrides: Any) -> UsageDayCounter:
    """Today's (UTC) token counter of a user, with no calls counted."""
    now = cosmos_now()
    values: dict[str, Any] = {
        "id": day_counter_id(now.date()),
        "user_id": uuid4() if user_id is None else user_id,
        "period": now.date().isoformat(),
        "created_at": now,
        "updated_at": now,
    }
    return UsageDayCounter.model_validate(values | overrides)


def usage_month_counter(*, user_id: UUID | None = None, **overrides: Any) -> UsageMonthCounter:
    """This month's (UTC) token counter of a user, with no calls counted."""
    now = cosmos_now()
    values: dict[str, Any] = {
        "id": month_counter_id(now.date()),
        "user_id": uuid4() if user_id is None else user_id,
        "period": now.strftime("%Y-%m"),
        "created_at": now,
        "updated_at": now,
    }
    return UsageMonthCounter.model_validate(values | overrides)


def rate_window(*, user_id: UUID | None = None, **overrides: Any) -> RateWindow:
    """This minute's turn-rate window of a user, counting one request."""
    values: dict[str, Any] = {
        "id": rate_window_id(epoch_minute(cosmos_now())),
        "user_id": uuid4() if user_id is None else user_id,
        "count": 1,
    }
    return RateWindow.model_validate(values | overrides)


def download_rate_window(*, user_id: UUID | None = None, **overrides: Any) -> DownloadRateWindow:
    """This minute's download-rate window of a user, counting one request."""
    values: dict[str, Any] = {
        "id": download_rate_window_id(epoch_minute(cosmos_now())),
        "user_id": uuid4() if user_id is None else user_id,
        "count": 1,
    }
    return DownloadRateWindow.model_validate(values | overrides)


def write_rate_window(*, user_id: UUID | None = None, **overrides: Any) -> WriteRateWindow:
    """This minute's write-rate window of a user, counting one request."""
    values: dict[str, Any] = {
        "id": write_rate_window_id(epoch_minute(cosmos_now())),
        "user_id": uuid4() if user_id is None else user_id,
        "count": 1,
    }
    return WriteRateWindow.model_validate(values | overrides)


def stream_lease(*, user_id: UUID | None = None, slot: int = 1, **overrides: Any) -> StreamLease:
    """A stream lease in `slot`, acquired now for a turn at the default deadline."""
    acquired = cosmos_now()
    ttl = int(overrides.pop("ttl", STREAM_LEASE_TTL_SECONDS))
    values: dict[str, Any] = {
        "id": stream_lease_id(slot),
        "user_id": uuid4() if user_id is None else user_id,
        "turn_id": uuid4(),
        "session_id": uuid4(),
        "acquired_at": acquired,
        "expires_at": acquired + timedelta(seconds=ttl),
        "ttl": ttl,
    }
    return StreamLease.model_validate(values | overrides)


def download_lease(
    *, user_id: UUID | None = None, slot: int = 1, **overrides: Any
) -> DownloadLease:
    """A download lease in `slot` of a citation download, acquired now."""
    acquired = cosmos_now()
    values: dict[str, Any] = {
        "id": download_lease_id(slot),
        "user_id": uuid4() if user_id is None else user_id,
        "turn_id": uuid4(),
        "session_id": uuid4(),
        "acquired_at": acquired,
        "expires_at": acquired + timedelta(seconds=DOWNLOAD_LEASE_SECONDS),
    }
    return DownloadLease.model_validate(values | overrides)
