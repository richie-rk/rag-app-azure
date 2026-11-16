"""`ConversationStore`: the turn path of the conversation store (`13-conversations-and-memory.md`
sections 1 to 3, 5, 7.11, 7.14 and 7.15; `12-chat-pipeline.md` sections 4 and 12).

The one object through which the API reads and writes the `conversations` and `memories`
containers. Every read, write and query names the caller's own `oid` as the first partition-key
level, so no session ID can reach another user's data, and every call passes its per-operation
`timeout` (point read 2 s, query page 5 s, batch, patch, replace and create 5 s).

- `begin_turn` derives the session and turn IDs, finds a duplicate request by a point read of the
  derived turn ID, enforces the session-to-project rule, the turn limit and the one-streaming-turn
  guard, takes over an expired active turn, and creates the turn (and a new session) in state
  `streaming` in one transactional batch.
- `read_recent_turns` returns the unsummarised `completed` turns the prompt budget can hold,
  oldest first, from one single-partition query read newest first.
- `get_prompt_memory` renders the user's memory lines for the preferences block; it never raises.
- `finalize_turn` writes the terminal turn, its `turn_trace` and the session in one batch, with
  `historyTokens`, the initial `memoryExtraction` and the cleared `activeTurn`.
- `attach_followups` stores the follow-ups and their usage record; `get_turn` serves the citation
  operations.
- For the session, feedback and memory routes (sections 4, 7 and 8): the session point read, the
  session list's prefix query, the session's turn query, the turn read and the feedback patch
  conditioned on its ETag, and the memory document's read, create-if-missing and conditioned
  replace.

Failures raise `ConversationError`, which the chat route maps to its problem response before the
stream starts and to its `error` event after; the other routes map it with `problem_on_failure`.
"""

from __future__ import annotations

import asyncio
import math
import unicodedata
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http import HTTPStatus
from typing import Any, Final, Literal, Protocol, cast
from uuid import UUID

from azure.core import MatchConditions
from azure.core.exceptions import (
    AzureError,
    ServiceRequestTimeoutError,
    ServiceResponseTimeoutError,
)
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosBatchOperationError,
    CosmosClientTimeoutError,
    CosmosHttpResponseError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)
from pydantic import ValidationError

from docchat_api.conversations import telemetry
from docchat_api.conversations.telemetry import Charges
from docchat_api.http.problems import DependencyUnavailable, InternalError
from docchat_api.tokens import INLINE_COUNT_MAX_CHARACTERS, Tokenizer, count_sync, count_tokens
from docchat_core.auth import Principal
from docchat_core.chat.citations import strip_citation_tokens
from docchat_core.cosmos.base import DocumentTooLargeError
from docchat_core.cosmos.conversations import (
    TRACE_MAX_BYTES,
    ActiveTurn,
    ExtractionState,
    Feedback,
    FinishReason,
    MemoryExtractionState,
    ModelCallUsage,
    SessionDocument,
    SessionHistory,
    StoredCitation,
    TurnDocument,
    TurnError,
    TurnStatus,
    TurnTraceData,
    TurnTraceDocument,
    TurnUsage,
    new_session_id,
    turn_trace_id,
)
from docchat_core.cosmos.conversations import turn_id as derive_turn_id
from docchat_core.cosmos.memories import Fact, MemoryDocument
from docchat_core.errors import CODES, DocChatProblem
from docchat_core.memory import CATEGORY_LABELS, CATEGORY_ORDER
from docchat_core.models import format_utc
from docchat_core.models.clock import Clock, SystemClock
from docchat_core.opsconfig import OpsConfig
from docchat_core.sql.engine import SqlError
from docchat_core.telemetry import set_attributes, start_span

type Sleep = Callable[[float], Awaitable[None]]
type PartitionKey = list[str]
type BatchOperation = tuple[str, tuple[Any, ...]] | tuple[str, tuple[Any, ...], dict[str, Any]]

POINT_READ_TIMEOUT_SECONDS: Final = 2
QUERY_PAGE_TIMEOUT_SECONDS: Final = 5
WRITE_TIMEOUT_SECONDS: Final = 5
HISTORY_PAGE_SIZE: Final = 20
DEADLINE_OFFSET_SECONDS: Final = 30
"""`deadlineAt` = receipt + `chat.max_turn_seconds` + 30 s: the 15-second shielded finaliser
that follows the turn cap, with 15 s to spare."""
BEGIN_WAITS: Final = (0.025, 0.05, 0.1)
"""`begin_turn`'s 3 attempts after a `412`, and its 3 re-reads of a concurrent duplicate."""
FINALIZE_WAITS: Final = (0.025, 0.05, 0.1, 0.2, 0.4)
FINALIZE_ATTEMPTS: Final = 5
FINALIZE_BUDGET_SECONDS: Final = 15
FOLLOWUPS_ATTEMPTS: Final = 3
DEPENDENCY_RETRY_AFTER_SECONDS: Final = 5
TURN_ABANDONED: Final = "turn_abandoned"

TITLE_MAX_CODE_POINTS: Final = 80
TITLE_CUT_WINDOW: Final = 79
TITLE_SPACE_FLOOR: Final = 40
TITLE_TRAILING: Final = " .,;:-"
ELLIPSIS: Final = "\N{HORIZONTAL ELLIPSIS}"
_TITLE_REMOVED_CATEGORIES: Final = frozenset({"Cc", "Cf", "Co", "Cs"})

ETAG_FIELD: Final = "_etag"
BATCH_ETAG_FIELD: Final = "eTag"
TURN_TYPE: Final = "turn"

RECENT_TURNS_QUERY: Final = (
    "SELECT c.seq, c.question, c.answer, c.followups, c.historyTokens FROM c "
    "WHERE c.userId = @userId AND c.sessionId = @sessionId AND c.type = @type "
    "AND c.status = @status AND c.seq > @covered "
    "ORDER BY c.seq DESC"
)
"""The history read (section 3.5): single-partition, constant text, every literal a parameter."""

SESSION_TYPE: Final = "session"
_SESSION_LIST_SELECT: Final = (
    "SELECT TOP @take c.sessionId, c.projectId, c.title, c.createdAt, c.lastTurnAt, c.turnCount "
    "FROM c WHERE c.userId = @userId AND c.type = @type "
    "AND ARRAY_CONTAINS(@projectIds, c.projectId) "
)
_SESSION_LIST_ORDER: Final = "ORDER BY c.lastTurnAt DESC, c.sessionId DESC"
SESSIONS_QUERY: Final = _SESSION_LIST_SELECT + _SESSION_LIST_ORDER
"""The session list's first page (section 4.2): the one app query on the `[oid]` prefix besides
the after-response re-check."""
SESSIONS_AFTER_QUERY: Final = (
    _SESSION_LIST_SELECT
    + "AND (c.lastTurnAt < @t OR (c.lastTurnAt = @t AND c.sessionId < @s)) "
    + _SESSION_LIST_ORDER
)
"""The session list after a cursor: the second constant text, keyset on (`lastTurnAt`,
`sessionId`)."""
SESSION_TURNS_QUERY: Final = (
    "SELECT TOP @take c.id, c.seq, c.status, c.question, c.answer, c.answerTruncated, c.error, "
    "c.citations, c.allowedLinks, c.followups, c.feedback, c.createdAt, c.endedAt, c.deadlineAt "
    "FROM c WHERE c.userId = @userId AND c.sessionId = @sessionId AND c.type = @type "
    "AND c.seq < @before "
    "ORDER BY c.seq DESC"
)
"""The turns of `getSession` and `listSessionTurns` (section 4.4): single-partition, newest
first."""

_RETRIED_STATUSES: Final = frozenset(
    {HTTPStatus.REQUEST_TIMEOUT, HTTPStatus.TOO_MANY_REQUESTS, 449}  # 449: Cosmos DB's retry-with
)
_TERMINAL: Final = frozenset({TurnStatus.COMPLETED, TurnStatus.FAILED, TurnStatus.CANCELLED})

# The positions of the finalisation batch's operations.
_TURN_OPERATION: Final = 0
_TRACE_OPERATION: Final = 1
_SESSION_OPERATION: Final = 2


# --- Ports ----------------------------------------------------------------------------------


class QueryPages(Protocol):
    """A query's pager: pages fetched one request at a time."""

    def by_page(
        self, continuation_token: str | None = None
    ) -> AsyncIterator[AsyncIterator[Mapping[str, Any]]]: ...


class ConversationsContainer(Protocol):
    """The calls the store makes on the `conversations` container (`azure.cosmos.aio`)."""

    async def read_item(
        self, item: str, partition_key: PartitionKey, **kwargs: Any
    ) -> Mapping[str, Any]: ...

    def query_items(self, query: str, **kwargs: Any) -> QueryPages: ...

    async def execute_item_batch(
        self, batch_operations: Sequence[BatchOperation], partition_key: PartitionKey, **kwargs: Any
    ) -> Sequence[Mapping[str, Any]]: ...

    async def replace_item(
        self, item: str, body: dict[str, Any], **kwargs: Any
    ) -> Mapping[str, Any]: ...

    async def patch_item(
        self,
        item: str,
        partition_key: PartitionKey,
        patch_operations: list[dict[str, Any]],
        **kwargs: Any,
    ) -> Mapping[str, Any]: ...


class MemoriesContainer(Protocol):
    """The calls the store makes on the `memories` container: the turn path's point read, and the
    memory operations' create and conditioned replace."""

    async def read_item(
        self, item: str, partition_key: str, **kwargs: Any
    ) -> Mapping[str, Any]: ...

    async def create_item(self, body: dict[str, Any], **kwargs: Any) -> Mapping[str, Any]: ...

    async def replace_item(
        self, item: str, body: dict[str, Any], **kwargs: Any
    ) -> Mapping[str, Any]: ...


class OpsSource(Protocol):
    """The worker's `OpsConfigStore`."""

    async def snapshot(self) -> OpsConfig: ...


# --- Errors ---------------------------------------------------------------------------------


class SessionNotFound(DocChatProblem):
    """`404 session_not_found`: one body for a missing session and for one of another project."""

    code = "session_not_found"


class TurnInProgress(DocChatProblem):
    """`409 turn_in_progress`, with `retryAfterSeconds`, `sessionId` and `turnId`."""

    code = "turn_in_progress"


class TurnAlreadyEnded(DocChatProblem):
    """`409 turn_already_ended`, with `sessionId` and `turnId`."""

    code = "turn_already_ended"


class ClientTurnIdConflict(DocChatProblem):
    """`409 client_turn_id_conflict`, with `sessionId` and `turnId`."""

    code = "client_turn_id_conflict"


class SessionTurnLimit(DocChatProblem):
    """`409 session_turn_limit`, with `sessionId` only: no turn exists yet."""

    code = "session_turn_limit"


_PROBLEMS: Final[Mapping[str, type[DocChatProblem]]] = {
    "session_not_found": SessionNotFound,
    "turn_in_progress": TurnInProgress,
    "turn_already_ended": TurnAlreadyEnded,
    "client_turn_id_conflict": ClientTurnIdConflict,
    "session_turn_limit": SessionTurnLimit,
    "dependency_unavailable": DependencyUnavailable,
    "internal_error": InternalError,
}


class ConversationError(Exception):
    """A store failure: `code` from the catalogue, `status` its HTTP status (`None` for the
    stream-only codes `conversation_unavailable` and `turn_save_failed`), and the extension
    members its problem carries."""

    def __init__(
        self,
        code: str,
        status: int | None = None,
        retry_after_seconds: int | None = None,
        session_id: UUID | None = None,
        turn_id: UUID | None = None,
    ) -> None:
        if code not in CODES:
            raise ValueError(f"code {code!r} isn't registered in CODES")
        super().__init__(code)
        self.code = code
        self.status = CODES[code].status if status is None else status
        self.retry_after_seconds = retry_after_seconds
        self.session_id = session_id
        self.turn_id = turn_id

    def problem(self) -> DocChatProblem:
        """The problem the route answers with before the stream starts; a stream-only code has
        none and raises `TypeError`."""
        problem_type = _PROBLEMS.get(self.code)
        if problem_type is None:
            raise TypeError(f"{self.code} is a stream code, never a problem")
        return problem_type(
            retry_after_seconds=self.retry_after_seconds,
            session_id=self.session_id,
            turn_id=self.turn_id,
        )


@contextmanager
def problem_on_failure() -> Iterator[None]:
    """For a route that calls the store: a `ConversationError` raised in the block becomes its
    problem (`503 dependency_unavailable`, `500 internal_error`)."""
    try:
        yield
    except ConversationError as error:
        raise error.problem() from error


def _unavailable() -> ConversationError:
    return ConversationError(
        "dependency_unavailable", retry_after_seconds=DEPENDENCY_RETRY_AFTER_SECONDS
    )


def _internal_error() -> ConversationError:
    return ConversationError("internal_error")


# --- Types ----------------------------------------------------------------------------------


@dataclass(slots=True, kw_only=True)
class TurnHandle:
    """A started turn. Besides the fields of section 3.2 it keeps the turn and the session as
    the store last wrote or read them, with their ETags: `finalize_turn` builds the session
    replacement from the latest known session and `attach_followups` appends to the usage that
    finalisation wrote, so both update the handle in place."""

    oid: UUID
    session_id: UUID
    turn_id: UUID
    seq: int
    project_id: UUID
    turn_etag: str
    session_etag: str
    deadline_at: datetime
    turn: TurnDocument
    session: SessionDocument

    @property
    def partition_key(self) -> PartitionKey:
        """`[oid, sessionId]`, the session's logical partition."""
        return [str(self.oid), str(self.session_id)]


@dataclass(frozen=True, slots=True)
class Started:
    """A new turn in state `streaming`; `session` is the session as `begin_turn` wrote it."""

    handle: TurnHandle
    session: SessionDocument
    is_new_session: bool


@dataclass(frozen=True, slots=True)
class Replay:
    """A duplicate of a `completed` turn: the chat pipeline streams the stored turn."""

    turn: TurnDocument


type TurnStart = Started | Replay


@dataclass(frozen=True, slots=True, kw_only=True)
class HistoryTurn:
    """An earlier `completed` turn for the prompt; the answer is free of citation tokens, and the
    follow-ups serve only the suggested-question test."""

    seq: int
    question: str
    answer: str
    followups: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class TurnOutcome:
    """The turn fields the chat pipeline supplies to `finalize_turn` (`12-chat-pipeline.md`
    section 12.2)."""

    status: TurnStatus
    trace: TurnTraceData
    answer: str = ""
    answer_truncated: bool = False
    finish_reason: FinishReason | None = None
    error: TurnError | None = None
    citations: Sequence[StoredCitation] = ()
    allowed_links: Sequence[str] = ()
    usage: TurnUsage = field(default_factory=TurnUsage.empty)
    question_suggested: bool = False

    def __post_init__(self) -> None:
        if self.status not in _TERMINAL:
            raise ValueError("a turn is finalised with a terminal status")


@dataclass(frozen=True, slots=True)
class FinalizeResult:
    saved: bool
    turn_etag: str | None


@dataclass(frozen=True, slots=True)
class StoredTurn:
    """A turn as one point read returned it, with the ETag a conditioned patch names."""

    turn: TurnDocument
    etag: str


@dataclass(frozen=True, slots=True)
class StoredMemory:
    """A user's memory document with its `_etag`, which the memory operations return as `etag`
    and compare with `If-Match`."""

    document: MemoryDocument
    etag: str


# --- Session titles and documents -------------------------------------------------------------


def session_title(question: str) -> str:
    """The session's title, derived from its first question (section 5): NFC; each whitespace
    run one space; Cc, Cf, Co and Cs characters removed; trimmed; at most 80 code points, cut at a
    space at position 40 or later, or at 79, with trailing `.,;:-` removed and `…` appended."""
    text = unicodedata.normalize("NFC", question)
    collapsed: list[str] = []
    for character in text:
        if character.isspace():
            if not collapsed or collapsed[-1] != " ":
                collapsed.append(" ")
        else:
            collapsed.append(character)
    kept = "".join(
        character
        for character in collapsed
        if unicodedata.category(character) not in _TITLE_REMOVED_CATEGORIES
    ).strip(" ")
    if not kept:
        return ELLIPSIS
    if len(kept) <= TITLE_MAX_CODE_POINTS:
        return kept
    space = kept.rfind(" ", 0, TITLE_CUT_WINDOW)
    position = space + 1
    cut = (
        kept[: position - 1]
        if space >= 0 and position >= TITLE_SPACE_FLOOR
        else kept[:TITLE_CUT_WINDOW]
    )
    return cut.rstrip(TITLE_TRAILING) + ELLIPSIS


def _new_session(
    oid: UUID,
    session_id: UUID,
    project_id: UUID,
    first_question: str,
    now: datetime,
    *,
    turn_id: UUID,
    deadline_at: datetime,
) -> SessionDocument:
    return SessionDocument(
        id=session_id,
        user_id=oid,
        session_id=session_id,
        project_id=project_id,
        title=session_title(first_question),
        created_at=now,
        updated_at=now,
        last_turn_at=now,
        turn_count=1,
        active_turn=ActiveTurn(turn_id=turn_id, deadline_at=deadline_at),
    )


def new_session_document(
    oid: UUID,
    session_id: UUID,
    project_id: UUID,
    first_question: str,
    now: datetime,
    *,
    turn_id: UUID,
    deadline_at: datetime,
) -> dict[str, Any]:
    """The `session` document a first turn creates: `turnCount` 1, `lastTurnAt` = now,
    `activeTurn` = the new turn and its deadline, the history counters 0, the empty summary,
    every `summaryJob` field null or 0, and the title of section 5."""
    return _new_session(
        oid, session_id, project_id, first_question, now, turn_id=turn_id, deadline_at=deadline_at
    ).to_item()


# --- Prompt memory ----------------------------------------------------------------------------


def _memory_line(fact: Fact) -> str:
    return f"{CATEGORY_LABELS[fact.category]}: {fact.text}"


def _rendered(facts: Sequence[Fact]) -> str:
    # As the chat pipeline's `render_memory_block` writes them: `- ` before each, joined with LF.
    return "\n".join(f"- {_memory_line(fact)}" for fact in facts)


def ordered_facts(facts: Sequence[Fact]) -> list[Fact]:
    """The facts by category in the order `role`, `team`, `expertise`, `topic`, `answer_style`,
    newest `updatedAt` first within a category (section 7.15 step 2): the order of the memory
    lines and of the extraction call's fact labels (section 7.13)."""
    return sorted(
        facts,
        key=lambda fact: (CATEGORY_ORDER.index(fact.category), -fact.updated_at.timestamp()),
    )


def fit_memory_lines(facts: Sequence[Fact], tokenizer: Tokenizer, max_tokens: int) -> list[str]:
    """The memory lines (section 7.15): facts in category order, newest first within a
    category, then the fact with the oldest `updatedAt` removed while the rendered lines exceed
    `max_tokens`."""
    kept = ordered_facts(facts)
    while kept and count_sync(tokenizer, [_rendered(kept)]) > max_tokens:
        kept.remove(min(kept, key=lambda fact: fact.updated_at))
    return [_memory_line(fact) for fact in kept]


# --- Helpers ----------------------------------------------------------------------------------


def _status(exc: BaseException) -> int | None:
    if isinstance(exc, CosmosHttpResponseError):
        status = exc.status_code
        return status if isinstance(status, int) else None
    return None


def _is_timeout(exc: BaseException) -> bool:
    return isinstance(
        exc,
        TimeoutError
        | CosmosClientTimeoutError
        | ServiceRequestTimeoutError
        | ServiceResponseTimeoutError,
    )


def _retryable(exc: BaseException) -> bool:
    """Throttling, `408`, a timeout or a `5xx`: the writes that resubmit within their attempts."""
    status = _status(exc)
    if status is not None and (
        status in _RETRIED_STATUSES or status >= HTTPStatus.INTERNAL_SERVER_ERROR
    ):
        return True
    return _is_timeout(exc)


def _etag(item: Mapping[str, Any]) -> str:
    etag = item.get(ETAG_FIELD)
    if not isinstance(etag, str):
        raise _unavailable()
    return etag


def _batch_etag(result: Mapping[str, Any]) -> str:
    etag = result.get(BATCH_ETAG_FIELD)
    if isinstance(etag, str):
        return etag
    body = result.get("resourceBody")
    if isinstance(body, Mapping):
        return _etag(body)
    raise _unavailable()


def _seconds_until(now: datetime, deadline: datetime) -> int:
    return max(1, math.ceil((deadline - now).total_seconds()))


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _history_turn(row: Mapping[str, Any]) -> HistoryTurn:
    followups = row.get("followups")
    return HistoryTurn(
        seq=_int(row.get("seq")) or 0,
        question=str(row.get("question", "")),
        answer=strip_citation_tokens(str(row.get("answer", ""))),
        followups=tuple(item for item in followups if isinstance(item, str))
        if isinstance(followups, list)
        else (),
    )


@dataclass(slots=True, kw_only=True)
class _Begin:
    """One `begin_turn` call's inputs and running telemetry."""

    oid: UUID
    session_id: UUID
    turn_id: UUID
    client_turn_id: UUID
    project_id: UUID
    question: str
    trace_id: str
    now: datetime
    deadline_at: datetime
    max_turns: int
    is_new_session: bool
    charges: Charges
    attempts: int = 1

    @property
    def partition_key(self) -> PartitionKey:
        return [str(self.oid), str(self.session_id)]


_BEGIN_OUTCOMES: Final[Mapping[str, str]] = {
    "turn_in_progress": "in_progress",
    "turn_already_ended": "already_ended",
    "client_turn_id_conflict": "conflict",
    "session_not_found": "not_found",
    "session_turn_limit": "turn_limit",
    "dependency_unavailable": "unavailable",
}


# --- The store --------------------------------------------------------------------------------


class ConversationStore:
    """Created once per API worker at start-up over the process's `conversations` and
    `memories` container clients; it builds no client and no credential."""

    def __init__(
        self,
        conversations: ConversationsContainer,
        memories: MemoriesContainer,
        *,
        ops: OpsSource,
        tokenizer: Tokenizer,
        clock: Clock | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._conversations = conversations
        self._memories = memories
        self._ops = ops
        self._tokenizer = tokenizer
        self._clock: Clock = SystemClock() if clock is None else clock
        self._sleep = sleep

    # begin_turn ----------------------------------------------------------------------------

    async def begin_turn(
        self,
        principal: Principal,
        *,
        project_id: UUID,
        session_id: UUID | None,
        client_turn_id: UUID,
        question: str,
        received_at: datetime,
        trace_id: str,
        ops: OpsConfig | None = None,
    ) -> TurnStart:
        """Start the turn (sections 2.2 to 2.5). `ops` is the turn's ops-config snapshot, so the
        deadline and the stream lease use the same `chat.max_turn_seconds`; without it the store
        takes the current snapshot."""
        snapshot = ops if ops is not None else await self._ops.snapshot()
        derived_session = (
            new_session_id(principal.oid, client_turn_id) if session_id is None else session_id
        )
        begin = _Begin(
            oid=principal.oid,
            session_id=derived_session,
            turn_id=derive_turn_id(derived_session, client_turn_id),
            client_turn_id=client_turn_id,
            project_id=project_id,
            question=question,
            trace_id=trace_id,
            now=self._clock.now(),
            deadline_at=received_at
            + timedelta(seconds=snapshot.chat.max_turn_seconds + DEADLINE_OFFSET_SECONDS),
            max_turns=snapshot.conversations.max_turns_per_session,
            is_new_session=session_id is None,
            charges=Charges(),
        )
        with start_span(
            "docchat.conversations.begin_turn",
            **{
                "docchat.session_id": str(begin.session_id),
                "docchat.turn_id": str(begin.turn_id),
                "docchat.session_new": begin.is_new_session,
            },
        ) as span:
            outcome: str | None = None
            try:
                started = await self._begin(begin)
            except ConversationError as error:
                outcome = _BEGIN_OUTCOMES.get(error.code)
                raise
            else:
                if isinstance(started, Started):
                    outcome = "started"
                    set_attributes(span, **{"docchat.turn_seq": started.handle.seq})
                else:
                    outcome = "replay"
                return started
            finally:
                attributes: dict[str, object] = {
                    "docchat.attempts": begin.attempts,
                    "docchat.cosmos.request_charge": begin.charges.record("begin_turn"),
                }
                if outcome is not None:
                    attributes["docchat.outcome"] = outcome
                set_attributes(span, **attributes)

    async def _begin(self, begin: _Begin) -> TurnStart:
        stored = await self._read_turn(begin)
        if stored is not None:
            return await self._duplicate(begin, stored)
        if begin.is_new_session:
            return await self._create_session(begin)
        return await self._add_turn(begin)

    async def _read(
        self, item_id: UUID, partition_key: PartitionKey, charges: Charges
    ) -> Mapping[str, Any] | None:
        try:
            return await self._conversations.read_item(
                str(item_id),
                partition_key,
                timeout=POINT_READ_TIMEOUT_SECONDS,
                response_hook=charges.hook,
            )
        except CosmosResourceNotFoundError:
            return None

    async def _read_turn(self, begin: _Begin) -> Mapping[str, Any] | None:
        try:
            return await self._read(begin.turn_id, begin.partition_key, begin.charges)
        except (AzureError, TimeoutError) as exc:
            raise _unavailable() from exc

    async def _read_session(self, begin: _Begin) -> Mapping[str, Any] | None:
        try:
            return await self._read(begin.session_id, begin.partition_key, begin.charges)
        except (AzureError, TimeoutError) as exc:
            raise _unavailable() from exc

    async def _batch(
        self, partition_key: PartitionKey, operations: Sequence[BatchOperation], charges: Charges
    ) -> Sequence[Mapping[str, Any]]:
        return await self._conversations.execute_item_batch(
            operations,
            partition_key,
            timeout=WRITE_TIMEOUT_SECONDS,
            response_hook=charges.hook,
        )

    def _new_turn(self, begin: _Begin, seq: int) -> TurnDocument:
        try:
            return TurnDocument(
                id=begin.turn_id,
                user_id=begin.oid,
                session_id=begin.session_id,
                seq=seq,
                client_turn_id=begin.client_turn_id,
                project_id=begin.project_id,
                status=TurnStatus.STREAMING,
                question=begin.question,
                trace_id=begin.trace_id,
                deadline_at=begin.deadline_at,
                created_at=begin.now,
                updated_at=begin.now,
            )
        except ValidationError as exc:
            raise _internal_error() from exc

    def _started(
        self,
        begin: _Begin,
        turn: TurnDocument,
        session: SessionDocument,
        results: Sequence[Mapping[str, Any]],
        *,
        session_index: int,
        turn_index: int,
    ) -> Started:
        handle = TurnHandle(
            oid=begin.oid,
            session_id=begin.session_id,
            turn_id=begin.turn_id,
            seq=turn.seq,
            project_id=begin.project_id,
            turn_etag=_batch_etag(results[turn_index]),
            session_etag=_batch_etag(results[session_index]),
            deadline_at=begin.deadline_at,
            turn=turn,
            session=session,
        )
        return Started(handle=handle, session=session, is_new_session=begin.is_new_session)

    async def _create_session(self, begin: _Begin) -> TurnStart:
        """Step 3: the session and the first turn in one batch."""
        turn = self._new_turn(begin, 1)
        try:
            session = _new_session(
                begin.oid,
                begin.session_id,
                begin.project_id,
                begin.question,
                begin.now,
                turn_id=begin.turn_id,
                deadline_at=begin.deadline_at,
            )
            operations: list[BatchOperation] = [
                ("create", (session.to_item(),)),
                ("create", (turn.to_item(),)),
            ]
        except (ValidationError, DocumentTooLargeError) as exc:
            raise _internal_error() from exc
        try:
            results = await self._batch(begin.partition_key, operations, begin.charges)
        except CosmosBatchOperationError as exc:
            if exc.status_code == HTTPStatus.CONFLICT:
                # A concurrent identical request created them first.
                telemetry.record_conflict("begin_turn")
                return await self._concurrent_duplicate(begin)
            raise _unavailable() from exc
        except (AzureError, TimeoutError) as exc:
            raise _unavailable() from exc
        return self._started(begin, turn, session, results, session_index=0, turn_index=1)

    async def _concurrent_duplicate(self, begin: _Begin) -> TurnStart:
        """A `409` on a turn create: re-read the turn up to 3 times, then decide on it."""
        for wait in BEGIN_WAITS:
            await self._sleep(wait)
            stored = await self._read_turn(begin)
            if stored is not None:
                return await self._duplicate(begin, stored)
        raise _unavailable()

    async def _add_turn(self, begin: _Begin) -> TurnStart:
        """Steps 4 to 6 for a session that exists, at most 3 attempts after `412`s."""
        for attempt, wait in enumerate(BEGIN_WAITS, start=1):
            begin.attempts = attempt
            session, session_etag = self._checked_session(begin, await self._read_session(begin))
            stale = await self._stale_turn(begin, session.active_turn)
            turn = self._new_turn(begin, session.turn_count + 1)
            try:
                replacement = session.model_copy(
                    update={
                        "turn_count": session.turn_count + 1,
                        "last_turn_at": begin.now,
                        "active_turn": ActiveTurn(
                            turn_id=begin.turn_id, deadline_at=begin.deadline_at
                        ),
                        "updated_at": begin.now,
                    }
                )
                operations: list[BatchOperation] = [
                    (
                        "replace",
                        (str(begin.session_id), replacement.to_item()),
                        _if_match(session_etag),
                    ),
                    ("create", (turn.to_item(),)),
                ]
            except (ValidationError, DocumentTooLargeError) as exc:
                raise _internal_error() from exc
            if stale is not None:
                operations.append(stale)
            try:
                results = await self._batch(begin.partition_key, operations, begin.charges)
            except CosmosBatchOperationError as exc:
                if exc.status_code == HTTPStatus.CONFLICT and exc.error_index == 1:
                    telemetry.record_conflict("begin_turn")
                    return await self._concurrent_duplicate(begin)
                if exc.status_code != HTTPStatus.PRECONDITION_FAILED:
                    raise _unavailable() from exc
                # The session or the stale turn changed since it was read: start over on fresh
                # reads.
                telemetry.record_conflict("begin_turn")
                await self._sleep(wait)
                continue
            except (AzureError, TimeoutError) as exc:
                raise _unavailable() from exc
            if stale is not None:
                telemetry.record_abandoned("takeover")
            return self._started(begin, turn, replacement, results, session_index=0, turn_index=1)
        # Attempts exhausted: a live active turn means another start won the session.
        item = await self._read_session(begin)
        if item is not None:
            active = SessionDocument.from_item(item).active_turn
            if active is not None and active.deadline_at > begin.now:
                raise _in_progress(begin, active, begin.now)
        raise _unavailable()

    def _checked_session(
        self, begin: _Begin, item: Mapping[str, Any] | None
    ) -> tuple[SessionDocument, str]:
        """Step 4: the session-to-project rule, the turn limit and the active-turn guard."""
        session = SessionDocument.from_item(item) if item is not None else None
        if session is None or session.project_id != begin.project_id:
            raise ConversationError("session_not_found")
        if session.turn_count >= begin.max_turns:
            raise ConversationError("session_turn_limit", session_id=begin.session_id)
        active = session.active_turn
        if active is not None and active.deadline_at > begin.now:
            raise _in_progress(begin, active, begin.now)
        return session, _etag(cast("Mapping[str, Any]", item))

    async def _stale_turn(self, begin: _Begin, active: ActiveTurn | None) -> BatchOperation | None:
        """Step 5: the replacement that marks an expired active turn `turn_abandoned`, when that
        turn is still `streaming`."""
        if active is None:
            return None
        try:
            item = await self._read(active.turn_id, begin.partition_key, begin.charges)
        except (AzureError, TimeoutError) as exc:
            raise _unavailable() from exc
        if item is None:
            return None
        stale = TurnDocument.from_item(item)
        if stale.status is not TurnStatus.STREAMING:
            return None
        try:
            abandoned = _abandoned(stale, begin.now).to_item()
        except (ValidationError, DocumentTooLargeError) as exc:
            raise _internal_error() from exc
        return ("replace", (str(active.turn_id), abandoned), _if_match(_etag(item)))

    async def _duplicate(self, begin: _Begin, item: Mapping[str, Any]) -> TurnStart:
        """Section 2.4: the stored turn of a duplicate `clientTurnId`."""
        stored = TurnDocument.from_item(item)
        if stored.question != begin.question or stored.project_id != begin.project_id:
            telemetry.record_duplicate("conflict")
            raise ConversationError(
                "client_turn_id_conflict", session_id=begin.session_id, turn_id=begin.turn_id
            )
        if stored.status is TurnStatus.STREAMING:
            if stored.deadline_at > begin.now:
                telemetry.record_duplicate("in_progress")
                raise ConversationError(
                    "turn_in_progress",
                    retry_after_seconds=_seconds_until(begin.now, stored.deadline_at),
                    session_id=begin.session_id,
                    turn_id=begin.turn_id,
                )
            stored = await self._abandon_duplicate(begin, stored, _etag(item))
        if stored.status is TurnStatus.COMPLETED:
            telemetry.record_duplicate("replay")
            return Replay(turn=stored)
        telemetry.record_duplicate("already_ended")
        raise ConversationError(
            "turn_already_ended", session_id=begin.session_id, turn_id=begin.turn_id
        )

    async def _abandon_duplicate(
        self, begin: _Begin, stored: TurnDocument, etag: str
    ) -> TurnDocument:
        """Store a duplicate's expired `streaming` turn as `failed` / `turn_abandoned` and free
        its session; a terminal status another writer stored meanwhile stands."""
        for _ in range(2):
            abandoned = _abandoned(stored, begin.now)
            try:
                await self._conversations.replace_item(
                    str(begin.turn_id),
                    abandoned.to_item(),
                    etag=etag,
                    match_condition=MatchConditions.IfNotModified,
                    timeout=WRITE_TIMEOUT_SECONDS,
                    response_hook=begin.charges.hook,
                )
            except CosmosAccessConditionFailedError:
                telemetry.record_conflict("begin_turn")
                item = await self._read_turn(begin)
                if item is None:
                    raise _unavailable() from None
                stored, etag = TurnDocument.from_item(item), _etag(item)
                if stored.status in _TERMINAL:
                    return stored
                continue
            except (AzureError, TimeoutError) as exc:
                raise _unavailable() from exc
            except (ValidationError, DocumentTooLargeError) as exc:
                raise _internal_error() from exc
            await self._release_session(begin)
            telemetry.record_abandoned("duplicate")
            return abandoned
        raise _unavailable()

    async def _release_session(self, begin: _Begin) -> None:
        """Clear the session's `activeTurn` when it names the abandoned turn; a `412` is ignored,
        because the next start takes the session over."""
        item = await self._read_session(begin)
        if item is None:
            return
        session = SessionDocument.from_item(item)
        if session.active_turn is None or session.active_turn.turn_id != begin.turn_id:
            return
        released = session.model_copy(update={"active_turn": None, "updated_at": begin.now})
        try:
            await self._conversations.replace_item(
                str(begin.session_id),
                released.to_item(),
                etag=_etag(item),
                match_condition=MatchConditions.IfNotModified,
                timeout=WRITE_TIMEOUT_SECONDS,
                response_hook=begin.charges.hook,
            )
        except CosmosAccessConditionFailedError:
            telemetry.record_conflict("begin_turn")
        except (AzureError, TimeoutError) as exc:
            raise _unavailable() from exc

    # read_recent_turns ---------------------------------------------------------------------

    async def read_recent_turns(self, handle: TurnHandle, max_tokens: int) -> list[HistoryTurn]:
        """Every unsummarised `completed` turn, oldest first, with no turn-count limit; the read
        stops after the turn at which the running `historyTokens` total, counted newest first,
        first exceeds `max_tokens` (section 3.5). Raises `conversation_unavailable`."""
        charges = Charges()
        with start_span(
            "docchat.conversations.read_recent_turns",
            **{"docchat.session_id": str(handle.session_id)},
        ) as span:
            try:
                rows, tokens, stopped = await self._recent_rows(handle, max_tokens, charges)
            except (AzureError, TimeoutError) as exc:
                raise ConversationError("conversation_unavailable") from exc
            finally:
                set_attributes(
                    span,
                    **{"docchat.cosmos.request_charge": charges.record("read_recent_turns")},
                )
            turns = [_history_turn(row) for row in reversed(rows)]
            set_attributes(
                span,
                **{
                    "docchat.history.turns_loaded": len(turns),
                    "docchat.history.tokens_loaded": tokens,
                    "docchat.history.stopped_at_budget": stopped,
                },
            )
        return turns

    async def _recent_rows(
        self, handle: TurnHandle, max_tokens: int, charges: Charges
    ) -> tuple[list[Mapping[str, Any]], int, bool]:
        pager = self._conversations.query_items(
            RECENT_TURNS_QUERY,
            parameters=[
                {"name": "@userId", "value": str(handle.oid)},
                {"name": "@sessionId", "value": str(handle.session_id)},
                {"name": "@type", "value": TURN_TYPE},
                {"name": "@status", "value": TurnStatus.COMPLETED.value},
                {"name": "@covered", "value": handle.session.summary.covers_through_seq},
            ],
            partition_key=handle.partition_key,
            max_item_count=HISTORY_PAGE_SIZE,
            timeout=QUERY_PAGE_TIMEOUT_SECONDS,
            response_hook=charges.hook,
        )
        pages = pager.by_page()
        rows: list[Mapping[str, Any]] = []
        total = 0
        async for page in pages:
            async for row in page:
                rows.append(row)
                total += _int(row.get("historyTokens")) or 0
                if total > max_tokens:
                    # The page is in memory already; only another page would cost a request.
                    more = await anext(page, None) is not None
                    more = more or getattr(pages, "continuation_token", None) is not None
                    return rows, total, more
        return rows, total, False

    # get_prompt_memory ---------------------------------------------------------------------

    async def get_prompt_memory(self, oid: UUID) -> list[str]:
        """The memory lines `{label}: {text}` for the preferences block (section 7.15): `[]` when
        `memory.enabled` is false or the memory is paused, missing or empty, and when the read
        fails, which is logged as `memory_unavailable`. It never raises."""
        with start_span("docchat.memory.prompt_read") as span:
            try:
                snapshot = await self._ops.snapshot()
                memory = snapshot.memory
                document = await self._memory_document(oid) if memory.enabled else None
            except (AzureError, TimeoutError, ValidationError, SqlError) as exc:
                telemetry.report_memory_unavailable(exc)
                set_attributes(span, **{"docchat.outcome": "unavailable"})
                return []
            active = memory.enabled and not (document is not None and document.paused)
            lines: list[str] = []
            if document is not None and active and document.facts:
                if len(_rendered(document.facts)) < INLINE_COUNT_MAX_CHARACTERS:
                    lines = fit_memory_lines(
                        document.facts, self._tokenizer, memory.prompt_max_tokens
                    )
                else:
                    lines = await asyncio.to_thread(
                        fit_memory_lines, document.facts, self._tokenizer, memory.prompt_max_tokens
                    )
            set_attributes(
                span,
                **{
                    "docchat.memory.active": active,
                    "docchat.memory.facts_rendered": len(lines),
                    "docchat.outcome": "ok" if lines else "empty",
                },
            )
            return lines

    async def _memory_document(self, oid: UUID) -> MemoryDocument | None:
        try:
            item = await self._memories.read_item(
                str(oid), str(oid), timeout=POINT_READ_TIMEOUT_SECONDS
            )
        except CosmosResourceNotFoundError:
            return None
        return MemoryDocument.from_item(item)

    # finalize_turn -------------------------------------------------------------------------

    async def finalize_turn(self, handle: TurnHandle, outcome: TurnOutcome) -> FinalizeResult:
        """The finalisation batch (section 2.6): replace the turn, create its trace and replace the
        session, at most 5 attempts inside 15 s. Raises `turn_save_failed` when every attempt
        failed and `internal_error` when a document breaks a limit."""
        started_at = self._clock.monotonic()
        charges = Charges()
        error_code = telemetry.known_code(outcome.error.code if outcome.error else None)
        span_attributes: dict[str, object] = {"docchat.turn.status": outcome.status.value}
        if error_code is not None:
            span_attributes["docchat.turn.error_code"] = error_code
        with start_span("docchat.conversations.finalize_turn", **span_attributes) as span:
            result: FinalizeResult | None = None
            attempts = 1
            trace_bytes = 0
            try:
                parts = await self._finalisation(handle, outcome)
                trace_bytes = parts.trace_bytes
                result, attempts = await self._commit(handle, outcome, parts, started_at, charges)
                return result
            finally:
                attributes: dict[str, object] = {
                    "docchat.attempts": attempts,
                    "docchat.trace.bytes": trace_bytes,
                    "docchat.cosmos.request_charge": charges.record("finalize_turn"),
                    "docchat.outcome": "failed"
                    if result is None
                    else "saved"
                    if result.saved
                    else "superseded",
                }
                set_attributes(span, **attributes)

    async def _finalisation(self, handle: TurnHandle, outcome: TurnOutcome) -> _Finalisation:
        """Steps 1 to 3: the final turn, the trace and the session replacement."""
        snapshot = await self._ops.snapshot()
        now = self._clock.now()
        history_tokens: int | None = None
        if outcome.status is TurnStatus.COMPLETED:
            history_tokens = await count_tokens(
                self._tokenizer, handle.turn.question, strip_citation_tokens(outcome.answer)
            )
        try:
            turn = _final_turn(handle.turn, outcome, history_tokens, snapshot.memory.enabled, now)
            turn_item = turn.to_item()
            trace = TurnTraceDocument.model_validate(
                {
                    **{name: getattr(outcome.trace, name) for name in TurnTraceData.model_fields},
                    "id": turn_trace_id(handle.turn_id),
                    "user_id": handle.oid,
                    "session_id": handle.session_id,
                    "turn_id": handle.turn_id,
                    "created_at": now,
                }
            )
            if trace.json_size() > TRACE_MAX_BYTES:
                trace = trace.with_model_input_omitted()
                telemetry.record_trace_truncated()
            trace_item = trace.to_item()
            session = _finalised_session(handle.session, handle.turn_id, history_tokens, now)
            session_item = session.to_item()
        except (ValidationError, DocumentTooLargeError) as exc:
            raise _internal_error() from exc
        return _Finalisation(
            turn=turn,
            turn_item=turn_item,
            trace_item=trace_item,
            trace_bytes=trace.json_size(),
            session=session,
            session_item=session_item,
            history_tokens=history_tokens,
            now=now,
        )

    async def _commit(
        self,
        handle: TurnHandle,
        outcome: TurnOutcome,
        parts: _Finalisation,
        started_at: float,
        charges: Charges,
    ) -> tuple[FinalizeResult, int]:
        """Steps 4 and 5: submit the batch and settle each conflict by re-reading."""
        attempts = 0
        for attempt in range(1, FINALIZE_ATTEMPTS + 1):
            if attempt > 1:
                wait = FINALIZE_WAITS[attempt - 2]
                if self._clock.monotonic() - started_at + wait >= FINALIZE_BUDGET_SECONDS:
                    break
                await self._sleep(wait)
            attempts = attempt
            try:
                results = await self._batch(
                    handle.partition_key,
                    [
                        (
                            "replace",
                            (str(handle.turn_id), parts.turn_item),
                            _if_match(handle.turn_etag),
                        ),
                        ("create", (parts.trace_item,)),
                        (
                            "replace",
                            (str(handle.session_id), parts.session_item),
                            _if_match(handle.session_etag),
                        ),
                    ],
                    charges,
                )
            except CosmosBatchOperationError as exc:
                settled = await self._settle(handle, parts, exc, charges)
                if settled is not None:
                    return settled, attempts
                continue
            except (AzureError, TimeoutError) as exc:
                if _retryable(exc):
                    continue
                raise ConversationError("turn_save_failed") from exc
            handle.turn, handle.turn_etag = parts.turn, _batch_etag(results[_TURN_OPERATION])
            handle.session = parts.session
            handle.session_etag = _batch_etag(results[_SESSION_OPERATION])
            telemetry.record_finalized(
                outcome.status.value, outcome.error.code if outcome.error else None
            )
            return FinalizeResult(saved=True, turn_etag=handle.turn_etag), attempts
        raise ConversationError("turn_save_failed")

    async def _settle(
        self,
        handle: TurnHandle,
        parts: _Finalisation,
        failure: CosmosBatchOperationError,
        charges: Charges,
    ) -> FinalizeResult | None:
        """One failed batch: the result when the conflict decides the outcome, `None` to
        resubmit (with the fresh session or turn ETag), or `turn_save_failed`."""
        status, index = failure.status_code, failure.error_index
        try:
            if status == HTTPStatus.PRECONDITION_FAILED and index == _SESSION_OPERATION:
                telemetry.record_conflict("finalize_turn")
                item = await self._read(handle.session_id, handle.partition_key, charges)
                if item is None:
                    raise ConversationError("turn_save_failed") from failure
                fresh = SessionDocument.from_item(item)
                parts.session = _finalised_session(
                    fresh, handle.turn_id, parts.history_tokens, parts.now
                )
                parts.session_item = parts.session.to_item()
                handle.session, handle.session_etag = fresh, _etag(item)
                return None
            if (status == HTTPStatus.PRECONDITION_FAILED and index == _TURN_OPERATION) or (
                status == HTTPStatus.CONFLICT and index == _TRACE_OPERATION
            ):
                telemetry.record_conflict("finalize_turn")
                return await self._settle_turn(handle, parts, failure, charges)
        except (AzureError, TimeoutError) as exc:
            if _retryable(exc):
                return None
            raise ConversationError("turn_save_failed") from exc
        except (ValidationError, DocumentTooLargeError) as exc:
            raise _internal_error() from exc
        if _retryable(failure):
            return None
        raise ConversationError("turn_save_failed") from failure

    async def _settle_turn(
        self,
        handle: TurnHandle,
        parts: _Finalisation,
        failure: CosmosBatchOperationError,
        charges: Charges,
    ) -> FinalizeResult | None:
        item = await self._read(handle.turn_id, handle.partition_key, charges)
        if item is None:
            raise ConversationError("turn_save_failed") from failure
        trace = await self._read(turn_trace_id(handle.turn_id), handle.partition_key, charges)
        if trace is not None:
            # An earlier attempt committed and only its response was lost.
            handle.turn, handle.turn_etag = parts.turn, _etag(item)
            return FinalizeResult(saved=True, turn_etag=handle.turn_etag)
        current = TurnDocument.from_item(item)
        if current.status in _TERMINAL:
            # A takeover or a duplicate stored the turn abandoned; a terminal status never
            # changes.
            telemetry.record_superseded()
            return FinalizeResult(saved=False, turn_etag=None)
        handle.turn_etag = _etag(item)
        return None

    # attach_followups ----------------------------------------------------------------------

    async def attach_followups(
        self, handle: TurnHandle, followups: Sequence[str], usage_record: ModelCallUsage
    ) -> bool:
        """Store the follow-ups and append their usage record (section 2.7), with a patch that
        has no ETag condition, at most 3 attempts. `True` only when the patch committed; it never
        raises."""
        now = self._clock.now()
        with start_span(
            "docchat.conversations.attach_followups",
            **{
                "docchat.session_id": str(handle.session_id),
                "docchat.turn_id": str(handle.turn_id),
                "docchat.followups.count": len(followups),
            },
        ) as span:
            saved = await self._patch_followups(handle, list(followups), usage_record, now)
            set_attributes(span, **{"docchat.outcome": "saved" if saved else "not_saved"})
        if not saved:
            telemetry.report_followups_not_saved(handle.session_id, handle.turn_id)
        return saved

    async def _patch_followups(
        self,
        handle: TurnHandle,
        followups: list[str],
        usage_record: ModelCallUsage,
        now: datetime,
    ) -> bool:
        charges = Charges()
        try:
            usage = handle.turn.usage.with_call(usage_record)
            updated = handle.turn.model_copy(
                update={
                    "followups": followups,
                    "followups_at": now,
                    "usage": usage,
                    "updated_at": now,
                }
            )
            updated.to_item()
        except (ValidationError, DocumentTooLargeError):
            return False
        operations: list[dict[str, Any]] = [
            {"op": "set", "path": "/followups", "value": followups},
            {"op": "set", "path": "/followupsAt", "value": format_utc(now)},
            {"op": "set", "path": "/usage", "value": usage.model_dump(mode="json")},
            {"op": "set", "path": "/updatedAt", "value": format_utc(now)},
        ]
        try:
            for _ in range(FOLLOWUPS_ATTEMPTS):
                try:
                    written = await self._conversations.patch_item(
                        str(handle.turn_id),
                        handle.partition_key,
                        operations,
                        timeout=WRITE_TIMEOUT_SECONDS,
                        response_hook=charges.hook,
                    )
                except (AzureError, TimeoutError) as exc:
                    if _retryable(exc):
                        continue
                    return False
                handle.turn = updated
                etag = written.get(ETAG_FIELD)
                if isinstance(etag, str):
                    handle.turn_etag = etag
                return True
            return False
        finally:
            charges.record("attach_followups")

    # get_turn ------------------------------------------------------------------------------

    async def get_turn(
        self, principal: Principal, session_id: UUID, turn_id: UUID
    ) -> TurnDocument | None:
        """One point read in `[oid, sessionId]` for the citation operations (section 3.6); `None`
        when the turn doesn't exist. Raises `dependency_unavailable`."""
        try:
            item = await self._read(turn_id, [principal.partition_key, str(session_id)], Charges())
        except (AzureError, TimeoutError) as exc:
            raise _unavailable() from exc
        return TurnDocument.from_item(item) if item is not None else None

    # The session, feedback and memory routes (sections 4, 7 and 8) ---------------------------

    @property
    def clock(self) -> Clock:
        """The clock the routes stamp their writes with."""
        return self._clock

    @property
    def tokenizer(self) -> Tokenizer:
        """The chat encoding's tokenizer, which counts a fact's `tokens`."""
        return self._tokenizer

    async def read_session(
        self, principal: Principal, session_id: UUID, charges: Charges
    ) -> SessionDocument | None:
        """One point read of the session in `[oid, sessionId]`; `None` when it doesn't exist.
        Raises `dependency_unavailable`."""
        try:
            item = await self._read(session_id, [principal.partition_key, str(session_id)], charges)
        except (AzureError, TimeoutError) as exc:
            raise _unavailable() from exc
        return SessionDocument.from_item(item) if item is not None else None

    async def list_session_rows(
        self,
        principal: Principal,
        *,
        project_ids: Sequence[UUID],
        take: int,
        after: tuple[datetime, UUID] | None,
        charges: Charges,
    ) -> list[Mapping[str, Any]]:
        """The session list's prefix query on `[oid]` (section 4.2): at most `take` sessions of
        `project_ids`, `lastTurnAt` descending then `sessionId` descending, after the keyset
        position `after` when given. Raises `dependency_unavailable`."""
        parameters: list[dict[str, Any]] = [
            {"name": "@take", "value": take},
            {"name": "@userId", "value": principal.partition_key},
            {"name": "@type", "value": SESSION_TYPE},
            {"name": "@projectIds", "value": [str(project_id) for project_id in project_ids]},
        ]
        query = SESSIONS_QUERY
        if after is not None:
            query = SESSIONS_AFTER_QUERY
            parameters += [
                {"name": "@t", "value": format_utc(after[0])},
                {"name": "@s", "value": str(after[1])},
            ]
        return await self._rows(query, parameters, [principal.partition_key], charges)

    async def list_turn_rows(
        self,
        principal: Principal,
        session_id: UUID,
        *,
        before: int,
        take: int,
        charges: Charges,
    ) -> list[Mapping[str, Any]]:
        """At most `take` turns of the session with `seq` below `before`, newest first, with the
        fields of the turn view (section 4.4). Raises `dependency_unavailable`."""
        parameters: list[dict[str, Any]] = [
            {"name": "@take", "value": take},
            {"name": "@userId", "value": principal.partition_key},
            {"name": "@sessionId", "value": str(session_id)},
            {"name": "@type", "value": TURN_TYPE},
            {"name": "@before", "value": before},
        ]
        return await self._rows(
            SESSION_TURNS_QUERY,
            parameters,
            [principal.partition_key, str(session_id)],
            charges,
        )

    async def _rows(
        self,
        query: str,
        parameters: list[dict[str, Any]],
        partition_key: PartitionKey,
        charges: Charges,
    ) -> list[Mapping[str, Any]]:
        try:
            pager = self._conversations.query_items(
                query,
                parameters=parameters,
                partition_key=partition_key,
                timeout=QUERY_PAGE_TIMEOUT_SECONDS,
                response_hook=charges.hook,
            )
            rows: list[Mapping[str, Any]] = []
            async for page in pager.by_page():
                rows.extend([row async for row in page])
        except (AzureError, TimeoutError) as exc:
            raise _unavailable() from exc
        return rows

    async def read_turn_entry(
        self, principal: Principal, session_id: UUID, turn_id: UUID, charges: Charges
    ) -> StoredTurn | None:
        """One point read of the turn in `[oid, sessionId]` with its ETag; `None` when it doesn't
        exist. Raises `dependency_unavailable`."""
        try:
            item = await self._read(turn_id, [principal.partition_key, str(session_id)], charges)
        except (AzureError, TimeoutError) as exc:
            raise _unavailable() from exc
        if item is None:
            return None
        return StoredTurn(turn=TurnDocument.from_item(item), etag=_etag(item))

    async def patch_feedback(
        self,
        principal: Principal,
        stored: StoredTurn,
        feedback: Feedback,
        now: datetime,
        charges: Charges,
    ) -> bool:
        """`set /feedback` and `set /updatedAt` on the turn, conditioned on the ETag it was read
        with (section 8.1 step 4). `False` when the turn changed or vanished since, so the route
        reads it again; `internal_error` when the turn would break a limit; otherwise
        `dependency_unavailable` on a failure."""
        turn = stored.turn
        try:
            turn.model_copy(update={"feedback": feedback, "updated_at": now}).to_item()
        except (ValidationError, DocumentTooLargeError) as exc:
            raise _internal_error() from exc
        operations: list[dict[str, Any]] = [
            {"op": "set", "path": "/feedback", "value": feedback.model_dump(mode="json")},
            {"op": "set", "path": "/updatedAt", "value": format_utc(now)},
        ]
        try:
            await self._conversations.patch_item(
                str(turn.id),
                [principal.partition_key, str(turn.session_id)],
                operations,
                etag=stored.etag,
                match_condition=MatchConditions.IfNotModified,
                timeout=WRITE_TIMEOUT_SECONDS,
                response_hook=charges.hook,
            )
        except (CosmosAccessConditionFailedError, CosmosResourceNotFoundError):
            telemetry.record_conflict("patch_feedback")
            return False
        except (AzureError, TimeoutError) as exc:
            raise _unavailable() from exc
        return True

    async def read_memory(self, oid: UUID, charges: Charges) -> StoredMemory | None:
        """One point read of the caller's memory document; `None` when it doesn't exist. Raises
        `dependency_unavailable`."""
        try:
            item = await self._memories.read_item(
                str(oid), str(oid), timeout=POINT_READ_TIMEOUT_SECONDS, response_hook=charges.hook
            )
        except CosmosResourceNotFoundError:
            return None
        except (AzureError, TimeoutError) as exc:
            raise _unavailable() from exc
        return StoredMemory(document=MemoryDocument.from_item(item), etag=_etag(item))

    async def ensure_memory(self, oid: UUID, charges: Charges) -> StoredMemory:
        """The memory document, created empty with `create_item` when it doesn't exist; a `409`
        means a concurrent request created it, so it is read (section 7.1). Raises
        `dependency_unavailable`."""
        stored = await self.read_memory(oid, charges)
        if stored is not None:
            return stored
        try:
            body = MemoryDocument.empty(oid, self._clock.now()).to_item()
        except (ValidationError, DocumentTooLargeError) as exc:
            raise _internal_error() from exc
        try:
            item = await self._memories.create_item(
                body, timeout=WRITE_TIMEOUT_SECONDS, response_hook=charges.hook
            )
        except CosmosResourceExistsError:
            telemetry.record_conflict("ensure_memory")
            stored = await self.read_memory(oid, charges)
            if stored is None:
                raise _unavailable() from None
            return stored
        except (AzureError, TimeoutError) as exc:
            raise _unavailable() from exc
        return StoredMemory(document=MemoryDocument.from_item(item), etag=_etag(item))

    async def replace_memory(
        self, document: MemoryDocument, etag: str, charges: Charges
    ) -> StoredMemory | None:
        """Replace the memory document conditioned on `etag` (section 7.3 step 5); `None` when
        it changed or vanished since it was read. Raises `internal_error` when the document
        breaks a limit and `dependency_unavailable` on a failure."""
        try:
            body = document.to_item()
        except (ValidationError, DocumentTooLargeError) as exc:
            raise _internal_error() from exc
        try:
            item = await self._memories.replace_item(
                str(document.id),
                body,
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
                timeout=WRITE_TIMEOUT_SECONDS,
                response_hook=charges.hook,
            )
        except (CosmosAccessConditionFailedError, CosmosResourceNotFoundError):
            return None
        except (AzureError, TimeoutError) as exc:
            raise _unavailable() from exc
        return StoredMemory(document=MemoryDocument.from_item(item), etag=_etag(item))


# --- Document builders --------------------------------------------------------------------------


@dataclass(slots=True, kw_only=True)
class _Finalisation:
    """One `finalize_turn` call's documents; the session part is rebuilt after a `412`."""

    turn: TurnDocument
    turn_item: dict[str, Any]
    trace_item: dict[str, Any]
    trace_bytes: int
    session: SessionDocument
    session_item: dict[str, Any]
    history_tokens: int | None
    now: datetime


def _if_match(etag: str) -> dict[str, Any]:
    return {"if_match_etag": etag}


def _in_progress(begin: _Begin, active: ActiveTurn, now: datetime) -> ConversationError:
    return ConversationError(
        "turn_in_progress",
        retry_after_seconds=_seconds_until(now, active.deadline_at),
        session_id=begin.session_id,
        turn_id=active.turn_id,
    )


def _abandoned(turn: TurnDocument, now: datetime) -> TurnDocument:
    return turn.model_copy(
        update={
            "status": TurnStatus.FAILED,
            "error": TurnError(code=TURN_ABANDONED, retryable=True),
            "ended_at": now,
            "updated_at": now,
        }
    )


def _extraction(
    status: TurnStatus, question_suggested: bool, memory_enabled: bool, now: datetime
) -> MemoryExtractionState:
    """The initial `memoryExtraction` (section 7.11): `not_applicable` with reason
    `suggested_question` for a completed suggestion, `pending` for another completed turn while
    memory is on, `not_applicable` otherwise; never any attempt."""
    reason: Literal["suggested_question"] | None = None
    state = ExtractionState.NOT_APPLICABLE
    if status is TurnStatus.COMPLETED:
        if question_suggested:
            reason = "suggested_question"
        elif memory_enabled:
            state = ExtractionState.PENDING
    return MemoryExtractionState(state=state, reason=reason, attempts=0, updated_at=now)


def _final_turn(
    turn: TurnDocument,
    outcome: TurnOutcome,
    history_tokens: int | None,
    memory_enabled: bool,
    now: datetime,
) -> TurnDocument:
    return turn.model_copy(
        update={
            "status": outcome.status,
            "answer": outcome.answer,
            "answer_truncated": outcome.answer_truncated,
            "finish_reason": outcome.finish_reason,
            "error": outcome.error,
            "citations": list(outcome.citations),
            "allowed_links": list(outcome.allowed_links),
            "usage": outcome.usage,
            "history_tokens": history_tokens,
            "memory_extraction": _extraction(
                outcome.status, outcome.question_suggested, memory_enabled, now
            ),
            "ended_at": now,
            "updated_at": now,
        }
    )


def _finalised_session(
    session: SessionDocument, finished_turn: UUID, history_tokens: int | None, now: datetime
) -> SessionDocument:
    """Step 3: `activeTurn` cleared when it names this turn, the unsummarised counters raised by
    a completed turn's `historyTokens`."""
    active = session.active_turn
    history = session.history
    if history_tokens is not None:
        history = SessionHistory(
            unsummarised_tokens=history.unsummarised_tokens + history_tokens,
            unsummarised_turns=history.unsummarised_turns + 1,
        )
    return session.model_copy(
        update={
            "active_turn": None
            if active is not None and active.turn_id == finished_turn
            else active,
            "history": history,
            "updated_at": now,
        }
    )
