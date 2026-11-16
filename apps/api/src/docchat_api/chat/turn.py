"""The turn task: steps T1 to T14 and the shielded finaliser F of one accepted request, and the
replay of a completed duplicate (`12-chat-pipeline.md` sections 1.2, 4 to 13, 14.6 to 14.8, 15
and 19).

The task never writes to the connection: it puts events on the queue the stream writer drains.
Steps T2 to T10, everything before the model stream ends, run as one inner task, so a disconnect,
the turn deadline or the drain point ends them at once by cancelling it: every in-flight call
(retrieval, screening, the chat-small calls, the answer's HTTP stream) is cancelled with it, and the
calls record their usage, estimated where the response hadn't reported it. Once the model stream
has ended with a complete answer, finalisation (T11) runs to its end under a shield, and an
interruption only cancels the follow-ups call: a disconnect writes nothing more, the deadline and
the drain go on to `done`.

Every path ends in one finaliser F under `asyncio.shield` with a 15-second budget: `finalize_turn`
when T11 wasn't attempted, the usage records' counter writes, the stream lease's release when it
is still held, the after-response trigger for a `completed` turn, and the record
`chat_turn_finished`. A failure inside F is logged and never alters the stream.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, StrEnum
from typing import Final

from opentelemetry.trace import Span

from docchat_api.chat import small, telemetry
from docchat_api.chat.assembly import AssembledPrompt, PromptInputs, PromptTooLargeError, assemble
from docchat_api.chat.calls import CallContext, UsageLedger, elapsed_ms, safety_identifier
from docchat_api.chat.generation import Ending, Generation
from docchat_api.chat.markers import (
    REPLAY_DELTA_MAX_CODE_POINTS,
    CitationMinter,
    has_visible_text,
    split_deltas,
)
from docchat_api.chat.models import Citation, TurnUsageSummary
from docchat_api.chat.prompts import MarkerCollisionError
from docchat_api.chat.retrieval import (
    FilterFailedError,
    PassResult,
    Retriever,
    SearchFailedError,
    SearchPassName,
    confident,
)
from docchat_api.chat.screening import ScreeningStats, ScreeningUnavailable, record_failure
from docchat_api.chat.service import ChatService
from docchat_api.chat.sources import Candidate, NumberedSource
from docchat_api.chat.stream import (
    AnyEvent,
    StreamCitationsEvent,
    StreamDeltaEvent,
    StreamDoneEvent,
    StreamErrorCode,
    StreamErrorEvent,
    StreamFollowupsEvent,
    StreamMetaEvent,
    StreamStatusEvent,
    StreamWriter,
)
from docchat_api.conversations.store import (
    ConversationError,
    HistoryTurn,
    Started,
    TurnHandle,
    TurnOutcome,
)
from docchat_api.usage import Lease
from docchat_core.access import AccessibleProject, ResolvedIndex
from docchat_core.auth import Principal
from docchat_core.cosmos.conversations import (
    BillableCounts,
    BroadenOutcome,
    CallOutcome,
    EmbeddingDeployment,
    FinishReason,
    IndexSource,
    KeptOutcome,
    KeptSource,
    ModelCall,
    ModelCallUsage,
    ModelInput,
    PromptBudget,
    PromptMessage,
    RetrievalRecord,
    RewriteOutcome,
    ScreeningOutcome,
    ScreeningRecord,
    StoredCitation,
    TraceDeployments,
    TraceTimings,
    TurnDocument,
    TurnError,
    TurnStatus,
    TurnTraceData,
    TurnUsage,
)
from docchat_core.errors import CODES
from docchat_core.memory import is_suggested_question
from docchat_core.opsconfig import OpsConfig
from docchat_core.search.schema import ChunkIndexFields
from docchat_core.telemetry import set_attributes, start_span

FINALISER_BUDGET_SECONDS: Final = 15.0
RULES_VERSION: Final = "rules-v1"
SEARCH_QUERY_MAX_CODE_POINTS: Final = 4_000


class Interrupt(StrEnum):
    DISCONNECT = "disconnect"
    DEADLINE = "deadline"
    DRAIN = "drain"


class Phase(Enum):
    GENERATING = "generating"
    """T2 to T10: the model stream hasn't ended."""
    FINALISING = "finalising"
    """T11 runs under its shield."""
    FINALISED = "finalised"
    """T11 has returned; the citations, follow-ups and `done` follow."""


class TurnEndedError(Exception):
    """A failure that ends the turn with an `error` event before T11."""

    def __init__(
        self,
        code: StreamErrorCode,
        *,
        retry_after_seconds: int | None = None,
        finish_reason: FinishReason | None = None,
    ) -> None:
        super().__init__(code)
        self.code: StreamErrorCode = code
        self.retry_after_seconds = retry_after_seconds
        self.finish_reason = finish_reason


@dataclass(frozen=True, slots=True, kw_only=True)
class TurnRequest:
    """What the route established before `200`."""

    principal: Principal
    project: AccessibleProject
    index: ResolvedIndex
    question: str
    received_at: datetime
    received_monotonic: float
    correlation_id: str
    ops: OpsConfig
    lease: Lease


@dataclass(slots=True)
class TurnRecords:
    """What the turn has done so far, for the trace, the usage and the logs."""

    rewrite_outcome: RewriteOutcome = RewriteOutcome.SKIPPED
    standalone_query: str | None = None
    search_query: str | None = None
    first_pass: PassResult | None = None
    broadened_pass: PassResult | None = None
    rerank_partial: bool = False
    broaden_attempted: bool = False
    broadened: bool = False
    broaden_outcome: BroadenOutcome = BroadenOutcome.NOT_NEEDED
    search_passage: str | None = None
    kept: list[Candidate] = field(default_factory=list)
    flagged: frozenset[int] = frozenset()
    screened: bool = False
    screening: ScreeningRecord | None = None
    numbered: list[NumberedSource] = field(default_factory=list)
    prompt: AssembledPrompt | None = None
    timings: dict[str, int | None] = field(default_factory=dict)
    content_filter_degraded: bool = False
    semantic_queries: int = 0
    shields: ScreeningStats = field(default_factory=ScreeningStats)
    answer_started: bool = False
    question_suggested: bool = False


def _remaining(deadline: float, now: float) -> float:
    return max(0.0, deadline - now)


class ChatTurn:
    """One accepted turn: its queue, its task, its interruptions and its finaliser."""

    def __init__(self, service: ChatService, request: TurnRequest, started: Started) -> None:
        self.service = service
        self.request = request
        self.handle: TurnHandle = started.handle
        self.session_created = started.is_new_session
        self.queue: asyncio.Queue[AnyEvent] = asyncio.Queue()
        principal = request.principal
        self.context = CallContext(
            oid=principal.oid,
            project_id=request.project.id,
            session_id=self.handle.session_id,
            turn_id=self.handle.turn_id,
            safety_identifier=safety_identifier(principal.tid, principal.oid),
        )
        self.ledger = UsageLedger(
            context=self.context, pricing=request.ops.pricing, recorder=service.recorder
        )
        self.records = TurnRecords()
        self.phase = Phase.GENERATING
        self.interrupt: Interrupt | None = None
        self.client_gone = False
        self.minter: CitationMinter | None = None
        self.task: asyncio.Task[None] | None = None
        self._work: asyncio.Task[Generation] | None = None
        self._followups: asyncio.Task[small.FollowupsReply] | None = None
        self._stop_followups = False
        self._deadline_handle: asyncio.TimerHandle | None = None
        self._t11_attempted = False
        self._saved = False
        self._status: TurnStatus | None = None
        self._finish_reason: FinishReason | None = None
        self._error: TurnError | None = None
        self._ttft_ms: int | None = None
        self._total_ms: int | None = None
        self._span: Span | None = None
        self._writer = StreamWriter(
            self.queue,
            correlation_id=request.correlation_id,
            clock=service.clock,
            written=self._written,
            disconnected=self.client_disconnected,
        )

    # --- Start, the stream and the interruptions -----------------------------------------------

    @property
    def writer(self) -> StreamWriter:
        return self._writer

    def start(self) -> None:
        """P9: create the turn task, add it to the tracker and arm the turn deadline."""
        loop = asyncio.get_running_loop()
        self.task = loop.create_task(self._run())
        self.service.tracker.add_turn(self.task, self)
        deadline = self.request.received_monotonic + self.request.ops.chat.max_turn_seconds
        self._deadline_handle = loop.call_later(
            _remaining(deadline, self.service.clock.monotonic()), self.deadline_passed
        )

    def _put(self, event: AnyEvent) -> None:
        if not self.client_gone:
            self.queue.put_nowait(event)

    def _written(self, event: AnyEvent) -> None:
        if event.type == "delta" and self._ttft_ms is None:
            self._ttft_ms = elapsed_ms(self.service.clock, self.request.received_monotonic)

    def _interrupt(self, interrupt: Interrupt) -> None:
        if interrupt is Interrupt.DISCONNECT:
            self.client_gone = True
        work = self._work
        if self.phase is Phase.GENERATING and (work is None or not work.done()):
            if self.interrupt is None:
                self.interrupt = interrupt
                if interrupt is Interrupt.DEADLINE:
                    self.ledger.cancel_outcome = CallOutcome.TIMED_OUT
            if work is not None:
                work.cancel()
            return
        # The model stream has ended: the answer stands, only the follow-ups call goes.
        self._stop_followups = True
        if self._followups is not None:
            self._followups.cancel()

    def client_disconnected(self) -> None:
        """The stream writer ended without a terminal event: the client went away."""
        if self.client_gone or self.task is None or self.task.done():
            return
        before = self.phase is Phase.GENERATING and (self._work is None or not self._work.done())
        telemetry.record_disconnect("before_finalisation" if before else "after_finalisation")
        self._interrupt(Interrupt.DISCONNECT)

    def deadline_passed(self) -> None:
        """`chat.max_turn_seconds` after the receipt."""
        if self.task is not None and not self.task.done():
            self._interrupt(Interrupt.DEADLINE)

    def drain(self) -> None:
        """The drain point (section 14.8)."""
        if self.task is not None and not self.task.done():
            self._interrupt(Interrupt.DRAIN)

    # --- The task -------------------------------------------------------------------------------

    async def _run(self) -> None:
        handle = self.handle
        with start_span(
            "docchat.chat.turn",
            **{
                "docchat.project_id": str(self.request.project.id),
                "docchat.session_id": str(handle.session_id),
                "docchat.turn_id": str(handle.turn_id),
            },
        ) as span:
            self._span = span
            self._put(
                StreamMetaEvent(
                    session_id=handle.session_id,
                    turn_id=handle.turn_id,
                    session_created=self.session_created,
                    replayed=False,
                    correlation_id=self.request.correlation_id,
                )
            )
            try:
                await self._steps()
            finally:
                if self._deadline_handle is not None:
                    self._deadline_handle.cancel()
                finaliser = asyncio.get_running_loop().create_task(self._finalise())
                self.service.tracker.hold(finaliser)
                await asyncio.shield(finaliser)

    async def _steps(self) -> None:
        self._work = asyncio.get_running_loop().create_task(self._generate())
        if self.interrupt is not None:
            self._work.cancel()
        try:
            generation = await self._work
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                self._end_interrupted(Interrupt.DRAIN)
                raise
            self._end_interrupted(self.interrupt or Interrupt.DRAIN)
            return
        except TurnEndedError as failure:
            self._fail(failure.code, failure.retry_after_seconds, failure.finish_reason)
            return
        except Exception as failure:  # noqa: BLE001 - the stream's own failure boundary
            telemetry.report_unexpected(failure)
            self._fail("internal_error", None, self._finish_if_started())
            return
        outcome = generation.outcome
        if not outcome.completed:
            self._fail(
                _stream_code(outcome.error_code),
                generation.retry_after_seconds,
                outcome.finish_reason,
            )
            return
        try:
            await self._complete(generation)
        except Exception as failure:  # noqa: BLE001 - the stream's own failure boundary
            telemetry.report_unexpected(failure)
            self._put(StreamErrorEvent.of("internal_error", self.request.correlation_id))

    def _finish_if_started(self) -> FinishReason | None:
        return FinishReason.ERROR if self.records.answer_started else None

    def _end_interrupted(self, interrupt: Interrupt) -> None:
        if interrupt is Interrupt.DISCONNECT:
            self._status = TurnStatus.CANCELLED
            self._finish_reason = FinishReason.CANCELLED
            return
        code: StreamErrorCode = (
            "turn_timeout" if interrupt is Interrupt.DEADLINE else "service_restarting"
        )
        self._fail(code, None, self._finish_if_started())

    def _fail(
        self,
        code: StreamErrorCode,
        retry_after_seconds: int | None,
        finish_reason: FinishReason | None,
    ) -> None:
        self._status = TurnStatus.FAILED
        self._finish_reason = finish_reason
        self._error = TurnError(code=code, retryable=_retryable(code))
        self._put(StreamErrorEvent.of(code, self.request.correlation_id, retry_after_seconds))

    # --- T2 to T10 ------------------------------------------------------------------------------

    async def _generate(self) -> Generation:
        history, memory = await self._history_and_memory()
        records = self.records
        project = self.request.project
        records.question_suggested = is_suggested_question(
            self.request.question,
            [*(item for turn in history for item in turn.followups), *project.example_questions],
        )
        summary = self.handle.session.summary.text
        search_query = await self._rewrite(history, summary)
        candidates = await self._retrieve(search_query)
        sources = await self._screen(candidates)
        prompt = await self._assemble(history, memory, summary, sources)
        return await self._answer(prompt)

    async def _history_and_memory(self) -> tuple[list[HistoryTurn], list[str]]:
        """T2: the earlier turns, the memory lines and the store's pending-work re-check."""
        chat = self.request.ops.chat
        store = self.service.conversations
        budget = chat.context_token_budget - chat.answer_max_completion_tokens
        if self.service.post_turn is not None:
            self.service.post_turn.ensure_post_turn_work(self.handle.session)
        try:
            async with asyncio.TaskGroup() as group:
                history = group.create_task(store.read_recent_turns(self.handle, budget))
                memory = group.create_task(store.get_prompt_memory(self.context.oid))
        except* ConversationError:
            raise TurnEndedError("conversation_unavailable") from None
        return history.result(), memory.result()

    async def _rewrite(self, history: Sequence[HistoryTurn], summary: str | None) -> str:
        """T3: the standalone search query, when the session has a completed turn."""
        records = self.records
        question = self.request.question
        if summary is None and not history:
            records.search_query = question
            return question
        reply = await small.rewrite(
            self.service.calls,
            question=question,
            summary=summary,
            history=history,
            chat=self.request.ops.chat,
            ledger=self.ledger,
        )
        records.timings["rewrite_ms"] = reply.duration_ms
        if reply.text is None:
            records.rewrite_outcome = RewriteOutcome.FALLBACK
            records.search_query = question
            return question
        records.rewrite_outcome = RewriteOutcome.REWRITTEN
        records.standalone_query = reply.text
        records.search_query = reply.text
        return reply.text

    async def _embed(
        self, text: str, call: ModelCall, search_pass: SearchPassName
    ) -> list[float] | None:
        physical = self.request.index.physical_index
        return await self.service.calls.embed(
            call=call,
            deployment=physical.query_embedding_deployment,
            model=physical.embedding_model,
            dimensions=physical.embedding_dimensions,
            text=text,
            tokenizer=self.service.embedding_tokenizer(physical.embedding_model),
            ledger=self.ledger,
            search_pass=search_pass,
        )

    async def _retrieve(self, query: str) -> list[Candidate]:
        """T4 to T7: the query vector, the first pass, the filter, and the broadened search when
        confidence is low."""
        records = self.records
        clock = self.service.clock
        index = self.request.index
        try:
            fields = ChunkIndexFields.for_version(index.physical_index.schema_version)
        except KeyError:
            raise TurnEndedError("retrieval_unavailable") from None
        retrieval = self.request.ops.retrieval
        retriever = Retriever(
            clients=self.service.search_clients,
            served=self.service.served,
            index=index,
            fields=fields,
            retrieval=retrieval,
            session_id=self.handle.session_id,
            clock=clock,
        )
        started = clock.monotonic()
        query_vector = await self._embed(query, ModelCall.QUERY_EMBEDDING, "first")
        records.timings["query_embedding_ms"] = elapsed_ms(clock, started)
        if query_vector is None:
            raise TurnEndedError("retrieval_unavailable")
        records.semantic_queries += 1
        try:
            first = await retriever.run(query, [query_vector], "first")
        except (SearchFailedError, FilterFailedError):
            raise TurnEndedError("retrieval_unavailable") from None
        records.first_pass = first
        records.timings["search_ms"] = first.search_ms
        records.timings["served_filter_ms"] = first.filter_ms if first.hits else None
        records.kept = list(first.kept)
        if first.partial:
            records.rerank_partial = True
            records.broaden_outcome = BroadenOutcome.SKIPPED_PARTIAL
            telemetry.record_rerank_partial()
        elif not confident(first.kept, retrieval):
            await self._broaden(retriever, query, query_vector)
        return records.kept

    async def _broaden(self, retriever: Retriever, query: str, query_vector: list[float]) -> None:
        """T7's broadened search; any failure keeps the first pass's set."""
        records = self.records
        clock = self.service.clock
        records.broaden_attempted = True
        self._put(StreamStatusEvent())
        passage = await small.search_passage(
            self.service.calls,
            query=query,
            chat=self.request.ops.chat,
            retrieval=self.request.ops.retrieval,
            ledger=self.ledger,
        )
        records.timings["search_passage_ms"] = passage.duration_ms
        after_passage = clock.monotonic()
        replaced = False
        if passage.text is not None:
            records.search_passage = passage.text
            passage_vector = await self._embed(
                passage.text, ModelCall.PASSAGE_EMBEDDING, "broadened"
            )
            if passage_vector is not None:
                records.semantic_queries += 1
                try:
                    second = await retriever.run(query, [query_vector, passage_vector], "broadened")
                except (SearchFailedError, FilterFailedError):
                    second = None
                if second is not None:
                    records.broadened_pass = second
                    records.kept = list(second.kept)
                    replaced = True
        records.timings["broadened_search_ms"] = elapsed_ms(clock, after_passage)
        records.broadened = replaced
        records.broaden_outcome = BroadenOutcome.REPLACED if replaced else BroadenOutcome.FAILED
        telemetry.record_broadened_search("replaced" if replaced else "failed")

    async def _screen(self, candidates: Sequence[Candidate]) -> list[NumberedSource]:
        """T8: Prompt Shields over the kept sources; the unflagged ones become `S1` onwards."""
        records = self.records
        clock = self.service.clock
        started = clock.monotonic()
        bodies = [candidate.body for candidate in candidates]
        stats = records.shields
        with start_span("docchat.chat.screen") as span:
            try:
                if bodies:
                    result = await self.service.shields.screen(bodies, stats)
                    flagged = result.flagged
                else:
                    flagged = frozenset()
            except ScreeningUnavailable as failure:
                record_failure(failure)
                records.screening = ScreeningRecord(
                    outcome=ScreeningOutcome.UNAVAILABLE,
                    calls=stats.calls,
                    segments=stats.segments,
                    characters=stats.characters,
                    flagged=0,
                )
                set_attributes(span, **_screen_attributes(records.screening))
                raise TurnEndedError("source_screening_unavailable") from None
            finally:
                records.timings["screening_ms"] = elapsed_ms(clock, started)
            records.flagged = flagged
            records.screened = True
            records.screening = ScreeningRecord(
                outcome=ScreeningOutcome.FLAGGED if flagged else ScreeningOutcome.PASSED,
                calls=stats.calls,
                segments=stats.segments,
                characters=stats.characters,
                flagged=len(flagged),
            )
            set_attributes(span, **_screen_attributes(records.screening))
        index = self.request.index
        numbered: list[NumberedSource] = []
        for position, (candidate, body) in enumerate(zip(candidates, bodies, strict=True)):
            if position in flagged:
                telemetry.report_flagged_source(
                    document_id=candidate.hit.document_id,
                    chunk_set_id=candidate.hit.chunk_set_id,
                    index_id=index.search_index_id,
                    project_id=self.context.project_id,
                    session_id=self.context.session_id,
                    turn_id=self.context.turn_id,
                )
                continue
            numbered.append(
                NumberedSource(number=len(numbered) + 1, candidate=candidate, body=body)
            )
        records.numbered = numbered
        return numbered

    async def _assemble(
        self,
        history: Sequence[HistoryTurn],
        memory: Sequence[str],
        summary: str | None,
        sources: Sequence[NumberedSource],
    ) -> AssembledPrompt:
        """T9: the answer prompt within the budget, and `allowedLinks`."""
        chat = self.request.ops.chat
        clock = self.service.clock
        started = clock.monotonic()
        with start_span("docchat.chat.assemble") as span:
            try:
                prompt = await assemble(
                    PromptInputs(
                        question=self.request.question,
                        instructions=self.request.project.instructions,
                        memory_lines=memory,
                        summary=summary,
                        history=history,
                        sources=sources,
                    ),
                    tokenizer=self.service.answer.chat.tokenizer,
                    budget_tokens=chat.context_token_budget - chat.answer_max_completion_tokens,
                )
            except PromptTooLargeError:
                raise TurnEndedError("prompt_too_large") from None
            except MarkerCollisionError:
                raise TurnEndedError("internal_error") from None
            finally:
                self.records.timings["assembly_ms"] = elapsed_ms(clock, started)
            set_attributes(
                span,
                **{
                    "docchat.prompt_tokens": prompt.prompt_tokens,
                    "docchat.dropped_turns": prompt.dropped_turns,
                    "docchat.dropped_sources": len(prompt.dropped_source_ids),
                },
            )
        telemetry.record_prompt(
            prompt.prompt_tokens,
            dropped_turns=prompt.dropped_turns,
            dropped_sources=len(prompt.dropped_source_ids),
        )
        self.records.prompt = prompt
        return prompt

    async def _answer(self, prompt: AssembledPrompt) -> Generation:
        """T10: the streamed answer, released as `delta` events."""
        self.minter = CitationMinter(prompt.sent)
        self.records.answer_started = True
        clock = self.service.clock
        started = clock.monotonic()

        def emit(text: str) -> None:
            for piece in split_deltas(text):
                self._put(StreamDeltaEvent(text=piece))

        def degraded() -> None:
            self.records.content_filter_degraded = True
            telemetry.report_content_filter_degraded(
                deployment=self.service.answer.chat.deployment,
                project_id=self.context.project_id,
                session_id=self.context.session_id,
                turn_id=self.context.turn_id,
            )

        try:
            return await self.service.answer.run(
                messages=prompt.messages,
                prompt_tokens=prompt.prompt_tokens,
                max_completion_tokens=self.request.ops.chat.answer_max_completion_tokens,
                minter=self.minter,
                emit=emit,
                ledger=self.ledger,
                on_degraded=degraded,
            )
        finally:
            self.records.timings["generation_ms"] = elapsed_ms(clock, started)

    # --- T11 to T14 -----------------------------------------------------------------------------

    def _start_followups(self, answer: str) -> None:
        options = self.request.ops.followups
        prompt = self.records.prompt
        if (
            not options.enabled
            or prompt is None
            or not prompt.sent
            or not has_visible_text(answer)
            or self.client_gone
            or self._stop_followups
        ):
            return
        self._followups = asyncio.get_running_loop().create_task(
            small.followups(
                self.service.calls,
                question=self.request.question,
                answer=answer,
                sources=prompt.sent,
                chat=self.request.ops.chat,
                options=options,
                ledger=self.ledger,
            )
        )

    def _cancel_followups(self) -> None:
        self._stop_followups = True
        if self._followups is not None:
            self._followups.cancel()

    async def _complete(self, generation: Generation) -> None:
        """T11 to T14 for a complete answer."""
        self.phase = Phase.FINALISING
        minter = generation.minter
        self._start_followups(minter.answer)
        self._status = TurnStatus.COMPLETED
        self._finish_reason = generation.outcome.finish_reason
        self._t11_attempted = True
        finalising = asyncio.get_running_loop().create_task(self._save_completed(generation))
        self.service.tracker.hold(finalising)
        saved = await asyncio.shield(finalising)
        self.phase = Phase.FINALISED
        if not saved:
            self._cancel_followups()
            self._status = TurnStatus.FAILED
            self._put(StreamErrorEvent.of("turn_save_failed", self.request.correlation_id))
            return
        prompt = self.records.prompt
        self._put(
            StreamCitationsEvent(
                citations=[Citation.of(citation.view()) for citation in self._citations()],
                allowed_links=list(prompt.allowed_links) if prompt is not None else [],
            )
        )
        await self.request.lease.release()
        await self._attach_followups()
        records = await self.ledger.records()
        self._put(
            StreamDoneEvent(
                turn_id=self.handle.turn_id,
                truncated=generation.ending is Ending.LENGTH,
                usage=TurnUsageSummary.of(TurnUsage.of(records).totals),
            )
        )

    async def _save_completed(self, generation: Generation) -> bool:
        """T11: `finalize_turn` with status `completed`; `False` when it wasn't saved."""
        records = [
            record
            for record in await self.ledger.records()
            if record.call is not ModelCall.FOLLOWUPS
        ]
        outcome = self._outcome(records, completed_answer=generation)
        with start_span("docchat.chat.persist") as span:
            try:
                result = await self.service.conversations.finalize_turn(self.handle, outcome)
            except ConversationError:
                set_attributes(span, **{"docchat.outcome": "failed"})
                return False
            set_attributes(span, **{"docchat.outcome": "saved" if result.saved else "superseded"})
        self._saved = result.saved
        return result.saved

    async def _attach_followups(self) -> None:
        """T13: wait for the follow-ups call, store its questions, then show them."""
        task = self._followups
        if task is None:
            return
        try:
            reply = await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            return
        if not reply.questions or reply.record is None or self._stop_followups:
            return
        record = await asyncio.shield(reply.record)
        attaching = asyncio.get_running_loop().create_task(
            self.service.conversations.attach_followups(self.handle, reply.questions, record)
        )
        self.service.tracker.hold(attaching)
        attached = await asyncio.shield(attaching)
        if attached and not self._stop_followups:
            self._put(StreamFollowupsEvent(questions=list(reply.questions)))

    def _citations(self) -> list[StoredCitation]:
        if self.minter is None:
            return []
        index = self.request.index
        return self.minter.citations(
            search_index_id=index.search_index_id,
            physical_index_id=index.physical_index.id,
        )

    # --- Outcome and trace ----------------------------------------------------------------------

    def _outcome(
        self, records: Sequence[ModelCallUsage], *, completed_answer: Generation | None = None
    ) -> TurnOutcome:
        self._total_ms = elapsed_ms(self.service.clock, self.request.received_monotonic)
        status = self._status or TurnStatus.FAILED
        answer = self.minter.answer if self.minter is not None else ""
        prompt = self.records.prompt
        usage = TurnUsage.of(
            records,
            BillableCounts(
                semantic_queries=self.records.semantic_queries,
                shield_calls=self.records.shields.calls,
                shield_characters=self.records.shields.characters,
            ),
        )
        return TurnOutcome(
            status=status,
            trace=self._trace(),
            answer=answer,
            answer_truncated=completed_answer is not None
            and completed_answer.ending is Ending.LENGTH,
            finish_reason=self._finish_reason,
            error=self._error,
            citations=self._citations(),
            allowed_links=list(prompt.allowed_links) if prompt is not None else [],
            usage=usage,
            question_suggested=self.records.question_suggested,
        )

    def _kept(self) -> list[KeptSource]:
        records = self.records
        if not records.screened:
            return []
        prompt = records.prompt
        sent = {source.candidate.rank: source for source in prompt.sent} if prompt else {}
        numbered = {source.candidate.rank: source for source in records.numbered}
        kept: list[KeptSource] = []
        for position, candidate in enumerate(records.kept):
            hit = candidate.hit
            source = numbered.get(candidate.rank)
            if position in records.flagged:
                outcome = KeptOutcome.FLAGGED
            elif prompt is not None and candidate.rank not in sent:
                outcome = KeptOutcome.BUDGET_DROPPED
            else:
                outcome = KeptOutcome.SENT
            kept.append(
                KeptSource(
                    rank=candidate.rank,
                    source_id=source.source_id if source is not None else None,
                    chunk_key=hit.chunk_key,
                    document_id=hit.document_id,
                    document_version_id=hit.document_version_id,
                    chunk_set_id=hit.chunk_set_id,
                    reranker_score=hit.reranker_score,
                    outcome=outcome,
                )
            )
        return kept

    def _retrieval_record(self) -> RetrievalRecord | None:
        records = self.records
        first = records.first_pass
        if first is None:
            return None
        index = self.request.index
        physical = index.physical_index
        retrieval = self.request.ops.retrieval
        return RetrievalRecord(
            search_index_id=index.search_index_id,
            physical_index_id=physical.id,
            physical_index_name=physical.name,
            index_source=IndexSource(index.source),
            query_embedding_deployment=physical.query_embedding_deployment,
            embedding_model=physical.embedding_model,
            embedding_dimensions=physical.embedding_dimensions,
            candidate_count=retrieval.candidate_count,
            keep_count=retrieval.keep_count,
            reranker_threshold=retrieval.reranker_threshold,
            min_confident_results=retrieval.min_confident_results,
            rerank_partial=records.rerank_partial,
            broaden_attempted=records.broaden_attempted,
            broadened=records.broadened,
            broaden_outcome=records.broaden_outcome,
            search_passage=records.search_passage,
            first_pass=first.records(),
            broadened_pass=records.broadened_pass.records()
            if records.broadened_pass is not None
            else None,
            kept=self._kept(),
            screening=records.screening,
        )

    def _trace(self) -> TurnTraceData:
        records = self.records
        prompt = records.prompt
        answer = self.service.answer.chat
        physical = self.request.index.physical_index
        chat = self.request.ops.chat
        model_input = None
        budget = None
        if prompt is not None:
            budget = PromptBudget(
                budget_tokens=chat.context_token_budget,
                completion_reserve_tokens=chat.answer_max_completion_tokens,
                memory_facts_included=prompt.memory_facts_included,
                summary_included=prompt.summary_included,
            )
            if records.answer_started:
                model_input = ModelInput(
                    rules_version=RULES_VERSION,
                    marker=prompt.marker,
                    deployment=answer.deployment,
                    model=answer.model,
                    max_completion_tokens=chat.answer_max_completion_tokens,
                    reasoning_effort=answer.reasoning_effort,
                    messages=[
                        PromptMessage(role=message.role, content=message.content)
                        for message in prompt.messages
                    ],
                    prompt_tokens=prompt.prompt_tokens,
                    dropped_turns=prompt.dropped_turns,
                    dropped_source_ids=list(prompt.dropped_source_ids),
                )
        timings = dict(records.timings)
        timings["time_to_first_token_ms"] = self._ttft_ms
        timings["total_ms"] = self._total_ms
        search_query = records.search_query
        return TurnTraceData(
            rewrite_outcome=records.rewrite_outcome,
            standalone_query=records.standalone_query,
            search_query=search_query[:SEARCH_QUERY_MAX_CODE_POINTS] if search_query else None,
            retrieval=self._retrieval_record(),
            model_input=model_input,
            prompt_budget=budget,
            deployments=TraceDeployments(
                chat=answer.trace(),
                small=self.service.calls.small.trace(),
                query_embedding=EmbeddingDeployment(
                    deployment=physical.query_embedding_deployment,
                    model=physical.embedding_model,
                    dimensions=physical.embedding_dimensions,
                ),
            ),
            timings=TraceTimings(**timings),
            content_filter_degraded=records.content_filter_degraded,
        )

    # --- F ----------------------------------------------------------------------------------------

    async def _finalise(self) -> None:
        """The finaliser: under its own task and a 15-second budget, so cancellation can't
        interrupt persistence or usage accounting; a failure is logged and changes nothing."""
        try:
            async with asyncio.timeout(FINALISER_BUDGET_SECONDS):
                if self._followups is not None and not self._followups.done():
                    self._followups.cancel()
                if not self._t11_attempted:
                    records = await self.ledger.records()
                    outcome = self._outcome(records)
                    with start_span("docchat.chat.persist") as span:
                        result = await self.service.conversations.finalize_turn(
                            self.handle, outcome
                        )
                        set_attributes(
                            span,
                            **{"docchat.outcome": "saved" if result.saved else "superseded"},
                        )
                await self.ledger.settle()
                if not self.request.lease.released:
                    await self.request.lease.release()
                post_turn = self.service.post_turn
                if self._saved and post_turn is not None:
                    post_turn.schedule_post_turn_work(
                        self.context.oid, self.handle.session_id, self.handle.turn_id
                    )
        except Exception as failure:  # noqa: BLE001 - logged, never alters the stream
            telemetry.report_unexpected(failure)
        finally:
            await self._report()

    async def _report(self) -> None:
        records = await self.ledger.records()
        totals = TurnUsage.of(records).totals
        status = self._status or TurnStatus.FAILED
        outcome = status.value
        error_code = self._error.code if self._error is not None else None
        duration = elapsed_ms(self.service.clock, self.request.received_monotonic)
        prompt = self.records.prompt
        sent = len(prompt.sent) if prompt is not None else 0
        attributes: dict[str, object] = {
            "docchat.project_id": str(self.context.project_id),
            "docchat.session_id": str(self.context.session_id),
            "docchat.turn_id": str(self.context.turn_id),
            "docchat.outcome": outcome,
            "docchat.sources_sent": sent,
            "docchat.sources_flagged": len(self.records.flagged),
            "docchat.dropped_turns": prompt.dropped_turns if prompt is not None else 0,
            "docchat.dropped_sources": len(prompt.dropped_source_ids) if prompt else 0,
            "docchat.chat.duration_ms": duration,
            "docchat.prompt_tokens": totals.prompt_tokens,
            "docchat.completion_tokens": totals.completion_tokens,
            "docchat.reasoning_tokens": totals.reasoning_tokens,
            "docchat.chat.usage_estimated": totals.estimated,
        }
        if totals.estimated_cost_micros is not None:
            attributes["docchat.chat.estimated_cost_micros"] = totals.estimated_cost_micros
        if telemetry.known_code(error_code) is not None:
            attributes["docchat.error_code"] = error_code
        if self._finish_reason is not None:
            attributes["docchat.finish_reason"] = self._finish_reason.value
        if self._ttft_ms is not None:
            attributes["docchat.ttft_ms"] = self._ttft_ms
            telemetry.record_time_to_first_token(
                self._ttft_ms,
                broadened=self.records.broadened,
                deployment=self.service.answer.chat.deployment,
            )
        telemetry.report_turn_finished(attributes)
        telemetry.record_turn(outcome, error_code, duration)
        if self._span is not None:
            span_attributes = {
                key: value
                for key, value in attributes.items()
                if key
                not in {
                    "docchat.dropped_turns",
                    "docchat.dropped_sources",
                    "docchat.chat.duration_ms",
                    "docchat.completion_tokens",
                    "docchat.reasoning_tokens",
                    "docchat.chat.estimated_cost_micros",
                    "docchat.chat.usage_estimated",
                }
            }
            span_attributes["docchat.broadened"] = self.records.broadened
            span_attributes["docchat.rerank_partial"] = self.records.rerank_partial
            if prompt is not None:
                span_attributes["docchat.prompt_tokens"] = prompt.prompt_tokens
            else:
                span_attributes.pop("docchat.prompt_tokens", None)
            set_attributes(self._span, **span_attributes)


def _screen_attributes(record: ScreeningRecord) -> dict[str, object]:
    return {
        "docchat.calls": record.calls,
        "docchat.segments": record.segments,
        "docchat.characters": record.characters,
        "docchat.flagged": record.flagged,
        "docchat.outcome": record.outcome.value,
    }


def _retryable(code: str) -> bool:
    return CODES[code].retryable


def _stream_code(code: str | None) -> StreamErrorCode:
    known: dict[str | None, StreamErrorCode] = {
        "answer_incomplete": "answer_incomplete",
        "answer_filtered": "answer_filtered",
        "question_filtered": "question_filtered",
        "model_request_rejected": "model_request_rejected",
        "model_busy": "model_busy",
        "model_unavailable": "model_unavailable",
    }
    return known.get(code, "internal_error")


# --- The replay ---------------------------------------------------------------------------------


class ReplayTurn:
    """A duplicate of a `completed` turn streamed from the stored turn: no model, search or
    screening call, no usage and no write; the lease is released after `citations`."""

    def __init__(
        self,
        service: ChatService,
        *,
        turn: TurnDocument,
        lease: Lease,
        correlation_id: str,
        received_monotonic: float,
    ) -> None:
        self.service = service
        self.turn = turn
        self.lease = lease
        self.correlation_id = correlation_id
        self.received_monotonic = received_monotonic
        self.queue: asyncio.Queue[AnyEvent] = asyncio.Queue()
        self.task: asyncio.Task[None] | None = None
        self.writer = StreamWriter(
            self.queue,
            correlation_id=correlation_id,
            clock=service.clock,
            written=lambda _event: None,
            disconnected=self.client_disconnected,
        )

    def start(self) -> None:
        self.task = asyncio.get_running_loop().create_task(self._run())
        self.service.tracker.add_turn(self.task, self)

    def client_disconnected(self) -> None:
        """Nothing changes: a replay writes nothing; its finaliser releases the lease."""

    def drain(self) -> None:
        """A replay ends in moments and writes nothing, so the drain lets it finish."""

    async def _run(self) -> None:
        turn = self.turn
        with start_span(
            "docchat.chat.turn",
            **{
                "docchat.project_id": str(turn.project_id),
                "docchat.session_id": str(turn.session_id),
                "docchat.turn_id": str(turn.id),
            },
        ) as span:
            try:
                self.queue.put_nowait(
                    StreamMetaEvent(
                        session_id=turn.session_id,
                        turn_id=turn.id,
                        session_created=False,
                        replayed=True,
                        correlation_id=self.correlation_id,
                    )
                )
                for piece in split_deltas(turn.answer, REPLAY_DELTA_MAX_CODE_POINTS):
                    self.queue.put_nowait(StreamDeltaEvent(text=piece))
                self.queue.put_nowait(
                    StreamCitationsEvent(
                        citations=[Citation.of(citation.view()) for citation in turn.citations],
                        allowed_links=list(turn.allowed_links),
                    )
                )
                await self.lease.release()
                if turn.followups:
                    self.queue.put_nowait(StreamFollowupsEvent(questions=list(turn.followups)))
                self.queue.put_nowait(
                    StreamDoneEvent(
                        turn_id=turn.id,
                        truncated=turn.answer_truncated,
                        usage=TurnUsageSummary.of(turn.usage.totals),
                    )
                )
            finally:
                releasing = asyncio.get_running_loop().create_task(self.lease.release())
                self.service.tracker.hold(releasing)
                await asyncio.shield(releasing)
                set_attributes(span, **{"docchat.outcome": "replayed"})
                duration = elapsed_ms(self.service.clock, self.received_monotonic)
                telemetry.record_turn("replayed", None, duration)
