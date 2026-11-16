"""The after-response work and the turn-start re-check (`13-conversations-and-memory.md` sections
6.4 and 6.5; `12-chat-pipeline.md` sections 1.2 and 12.7).

`PostTurnWorker` is the chat pipeline's `PostTurnWork` (`ChatPorts.post_turn`), one per API worker:

- `schedule_post_turn_work(oid, session_id, turn_id)`, called by the finaliser of every turn that
  ends `completed`, creates one tracked task (`register_background_task`) and returns at once;
- `ensure_post_turn_work(session)`, called when a turn starts, performs no I/O: it schedules the
  same task without a turn ID when the session's summary rebuild is due or when this worker has
  scheduled no re-check for the user in the last 60 s (a map of at most 10,000 users, cleared when
  full: an optimisation only).

The task takes the ops-config snapshot, extracts memory from its own turn when that turn is
`pending`, runs the pending re-check (the one `[userId]` prefix query of the after-response work:
up to 5 other `pending` or `running` turns of the user, oldest first, expiring those past
`memory.retry_window_hours` and extracting the claimable ones), then rebuilds the session's summary
when it is due. Each step runs in its own `try`: a failure writes one `post_turn_step_failed`
warning and the next step still runs; the task never raises, only cancellation propagates. Every
piece of state lives in Cosmos DB, so work cut short by the drain, a restart or a crash keeps its
claims until their leases expire and a later re-check recovers it; no turn request waits for it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from functools import partial
from typing import Any, Final, Literal, Protocol
from uuid import UUID

from azure.core.exceptions import AzureError
from azure.cosmos.exceptions import CosmosResourceNotFoundError
from openai import AsyncOpenAI

from docchat_api.chat.calls import ChatDeploymentSettings
from docchat_api.chat.prompts import TextTokenizer
from docchat_api.chat.tasks import register_background_task
from docchat_api.conversations import telemetry
from docchat_api.conversations.extraction import (
    MemoryContainer,
    MemoryExtractor,
    claimable,
    expired,
)
from docchat_api.conversations.model_calls import SmallModel
from docchat_api.conversations.store import (
    ETAG_FIELD,
    POINT_READ_TIMEOUT_SECONDS,
    QUERY_PAGE_TIMEOUT_SECONDS,
    TURN_TYPE,
    ConversationsContainer,
)
from docchat_api.conversations.summary import SummaryRebuilder, rebuild_due
from docchat_api.conversations.telemetry import Charges
from docchat_api.usage import UsageStoreUnavailable
from docchat_core.audit import write_audit_event_after_commit
from docchat_core.cosmos.conversations import (
    ExtractionState,
    SessionDocument,
    TurnDocument,
)
from docchat_core.models.clock import Clock
from docchat_core.opsconfig import OpsConfig
from docchat_core.sql.engine import SqlEngine, SqlError
from docchat_core.telemetry import LOGGER_NAME, log_event, set_attributes, start_span
from docchat_core.telemetry.instruments import histogram

type Step = Literal["memory_extraction", "summary_rebuild"]
type Register = Callable[[asyncio.Task[Any]], None]

THROTTLE_SECONDS: Final = 60.0
THROTTLE_USERS_MAX: Final = 10_000
RECHECK_BATCH: Final = 5
POST_TURN_STEP_FAILED: Final = "post_turn_step_failed"
EXTRACTION_CHARGE: Final = "extraction"
SUMMARY_CHARGE: Final = "summary"
UNAVAILABLE_CODE: Final = "dependency_unavailable"
INTERNAL_CODE: Final = "internal_error"

RECHECK_QUERY: Final = (
    "SELECT TOP 6 c.id, c.sessionId, c.endedAt, c.memoryExtraction.state AS state, "
    "c.memoryExtraction.leaseExpiresAt AS leaseExpiresAt "
    "FROM c "
    "WHERE c.userId = @userId AND c.type = @type "
    "AND ARRAY_CONTAINS(@states, c.memoryExtraction.state) "
    "ORDER BY c.endedAt ASC"
)
"""The pending re-check (section 6.4 step 2.3): a `[userId]` prefix query, constant text, every
literal a parameter."""
RECHECK_STATES: Final = (ExtractionState.PENDING.value, ExtractionState.RUNNING.value)

UNSUMMARISED_TOKENS: Final = histogram("docchat.summary.unsummarised_tokens")

_logger: Final = logging.getLogger(LOGGER_NAME)


class OpsSource(Protocol):
    """The worker's `OpsConfigStore`."""

    async def snapshot(self) -> OpsConfig: ...


def report_step_failed(
    step: Step, failure: BaseException, session_id: UUID, turn_id: UUID | None
) -> None:
    """One warning per handled failure of a step: the step, a stable code (`dependency_unavailable`
    for a store that couldn't serve, `internal_error` otherwise) and the IDs, never the failure's
    text."""
    unavailable = isinstance(failure, AzureError | TimeoutError | SqlError | UsageStoreUnavailable)
    attributes: dict[str, object] = {
        "docchat.maintenance.step": step,
        "docchat.error_code": UNAVAILABLE_CODE if unavailable else INTERNAL_CODE,
        "docchat.session_id": str(session_id),
    }
    if turn_id is not None:
        attributes["docchat.turn_id"] = str(turn_id)
    log_event(_logger, POST_TURN_STEP_FAILED, logging.WARNING, **attributes)


def _etag(item: Mapping[str, Any]) -> str:
    etag = item.get(ETAG_FIELD)
    if not isinstance(etag, str):
        raise TypeError("a stored item without an _etag")
    return etag


class PostTurnWorker:
    """The conversation store's after-response work of one API worker."""

    def __init__(
        self,
        conversations: ConversationsContainer,
        *,
        summary: SummaryRebuilder,
        extraction: MemoryExtractor,
        ops: OpsSource,
        snapshot: OpsConfig,
        clock: Clock,
        register: Register = register_background_task,
    ) -> None:
        self._conversations = conversations
        self._summary = summary
        self._extraction = extraction
        self._ops = ops
        self._snapshot = snapshot
        """The latest snapshot a task took: `ensure_post_turn_work` reads the trigger from it,
        because it performs no I/O."""
        self._clock = clock
        self._register = register
        self._throttle: dict[UUID, float] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    # The chat pipeline's entry points ---------------------------------------------------------

    def schedule_post_turn_work(self, oid: UUID, session_id: UUID, turn_id: UUID) -> None:
        """Register one after-response task for a `completed` turn and return at once."""
        self._spawn(oid, session_id, turn_id)

    def ensure_post_turn_work(self, session: SessionDocument) -> None:
        """The turn-start re-check: schedule the task without a turn ID when the session's
        rebuild is due or no re-check was scheduled for the user in the last 60 s."""
        oid = session.user_id
        history = session.history
        due = rebuild_due(session, self._snapshot.summary, self._clock.now())
        moment = self._clock.monotonic()
        last = self._throttle.get(oid)
        scheduled = due or last is None or moment - last >= THROTTLE_SECONDS
        with start_span(
            "docchat.conversations.ensure_post_turn_work",
            **{
                "docchat.session_id": str(session.session_id),
                "docchat.summary.due": due,
                "docchat.history.unsummarised_tokens": history.unsummarised_tokens,
                "docchat.history.unsummarised_turns": history.unsummarised_turns,
                "docchat.maintenance.scheduled": scheduled,
            },
        ):
            UNSUMMARISED_TOKENS.record(history.unsummarised_tokens)
            if scheduled:
                if oid not in self._throttle and len(self._throttle) >= THROTTLE_USERS_MAX:
                    self._throttle.clear()
                self._throttle[oid] = moment
                self._spawn(oid, session.session_id, None)

    def _spawn(self, oid: UUID, session_id: UUID, turn_id: UUID | None) -> None:
        if self._closed:
            return
        # Created inside the request's context, so the task's spans join the request's trace.
        task = asyncio.get_running_loop().create_task(self.run(oid, session_id, turn_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        self._register(task)

    async def wait_idle(self) -> None:
        """Wait for every task scheduled so far."""
        while pending := [task for task in self._tasks if not task.done()]:
            await asyncio.gather(*pending, return_exceptions=True)

    async def aclose(self) -> None:
        """Lifespan shutdown: schedule nothing more and cancel the running tasks, whose claims
        expire with their leases."""
        self._closed = True
        running = [task for task in self._tasks if not task.done()]
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)

    # The task --------------------------------------------------------------------------------

    async def run(self, oid: UUID, session_id: UUID, turn_id: UUID | None) -> None:
        """The after-response task (section 6.4 item 2); never raises."""
        attributes: dict[str, object] = {"docchat.session_id": str(session_id)}
        if turn_id is not None:
            attributes["docchat.turn_id"] = str(turn_id)
        with start_span("docchat.maintenance.post_turn", **attributes) as span:
            extractions = 0
            summary_ran = False
            try:
                try:
                    snapshot = await self._ops.snapshot()
                except Exception as failure:  # noqa: BLE001 - reported, the task never raises
                    report_step_failed("memory_extraction", failure, session_id, turn_id)
                    report_step_failed("summary_rebuild", failure, session_id, turn_id)
                    return
                self._snapshot = snapshot
                if turn_id is not None:
                    extractions += await self._own_turn(oid, session_id, turn_id, snapshot)
                extractions += await self._recheck(oid, session_id, turn_id, snapshot)
                summary_ran = await self._rebuild(oid, session_id, snapshot)
            finally:
                set_attributes(
                    span,
                    **{
                        "docchat.maintenance.extractions": extractions,
                        "docchat.maintenance.summary_ran": summary_ran,
                    },
                )

    async def _read_turn(
        self, oid: UUID, session_id: UUID, turn_id: str, charges: Charges
    ) -> tuple[TurnDocument, str] | None:
        try:
            item = await self._conversations.read_item(
                turn_id,
                [str(oid), str(session_id)],
                timeout=POINT_READ_TIMEOUT_SECONDS,
                response_hook=charges.hook,
            )
        except CosmosResourceNotFoundError:
            return None
        return TurnDocument.from_item(item), _etag(item)

    async def _own_turn(
        self, oid: UUID, session_id: UUID, turn_id: UUID, snapshot: OpsConfig
    ) -> int:
        """Steps 2.1 and 2.2: the task's own turn, extracted when it is `pending`."""
        charges = Charges()
        try:
            read = await self._read_turn(oid, session_id, str(turn_id), charges)
            if read is None or read[0].memory_extraction.state is not ExtractionState.PENDING:
                return 0
            await self._extraction.extract(read[0], read[1], snapshot)
        except Exception as failure:  # noqa: BLE001 - reported, the next step still runs
            report_step_failed("memory_extraction", failure, session_id, turn_id)
            return 0
        finally:
            telemetry.REQUEST_CHARGE.record(charges.total, operation=EXTRACTION_CHARGE)
        return 1

    async def _pending(self, oid: UUID, charges: Charges) -> list[Mapping[str, Any]]:
        pager = self._conversations.query_items(
            RECHECK_QUERY,
            parameters=[
                {"name": "@userId", "value": str(oid)},
                {"name": "@type", "value": TURN_TYPE},
                {"name": "@states", "value": list(RECHECK_STATES)},
            ],
            partition_key=[str(oid)],
            timeout=QUERY_PAGE_TIMEOUT_SECONDS,
            response_hook=charges.hook,
        )
        rows: list[Mapping[str, Any]] = []
        async for page in pager.by_page():
            rows.extend([row async for row in page])
        return rows

    async def _recheck(
        self, oid: UUID, session_id: UUID, turn_id: UUID | None, snapshot: OpsConfig
    ) -> int:
        """Step 2.3: up to 5 other `pending` or `running` turns of the user, oldest first."""
        charges = Charges()
        try:
            rows = await self._pending(oid, charges)
        except Exception as failure:  # noqa: BLE001 - reported, the next step still runs
            report_step_failed("memory_extraction", failure, session_id, turn_id)
            return 0
        finally:
            telemetry.REQUEST_CHARGE.record(charges.total, operation=EXTRACTION_CHARGE)
        others = [row for row in rows if row.get("id") != (str(turn_id) if turn_id else None)]
        extractions = 0
        for row in others[:RECHECK_BATCH]:
            item_id, other_session = row.get("id"), row.get("sessionId")
            if not isinstance(item_id, str) or not isinstance(other_session, str):
                continue
            try:
                extractions += await self._recheck_turn(oid, UUID(other_session), item_id, snapshot)
            except Exception as failure:  # noqa: BLE001 - reported, the next turn still runs
                report_step_failed("memory_extraction", failure, UUID(other_session), UUID(item_id))
        return extractions

    async def _recheck_turn(
        self, oid: UUID, session_id: UUID, turn_id: str, snapshot: OpsConfig
    ) -> int:
        charges = Charges()
        try:
            read = await self._read_turn(oid, session_id, turn_id, charges)
        finally:
            telemetry.REQUEST_CHARGE.record(charges.total, operation=EXTRACTION_CHARGE)
        if read is None:
            return 0
        turn, etag = read
        state = turn.memory_extraction
        if state.state not in {ExtractionState.PENDING, ExtractionState.RUNNING}:
            return 0
        now = self._clock.now()
        if expired(turn, snapshot.memory, now):
            await self._extraction.expire(turn, etag)
            return 0
        if not claimable(state, now):
            return 0
        await self._extraction.extract(turn, etag, snapshot)
        return 1

    async def _rebuild(self, oid: UUID, session_id: UUID, snapshot: OpsConfig) -> bool:
        """Step 2.4: the session read, and the rebuild when it is due."""
        charges = Charges()
        try:
            try:
                item = await self._conversations.read_item(
                    str(session_id),
                    [str(oid), str(session_id)],
                    timeout=POINT_READ_TIMEOUT_SECONDS,
                    response_hook=charges.hook,
                )
            except CosmosResourceNotFoundError:
                return False
            finally:
                telemetry.REQUEST_CHARGE.record(charges.total, operation=SUMMARY_CHARGE)
            session = SessionDocument.from_item(item)
            if not rebuild_due(session, snapshot.summary, self._clock.now()):
                return False
            await self._summary.rebuild(session, _etag(item), snapshot)
        except Exception as failure:  # noqa: BLE001 - reported, the task never raises
            report_step_failed("summary_rebuild", failure, session_id, None)
        return True


def build_post_turn_work(
    *,
    conversations: ConversationsContainer,
    memories: MemoryContainer,
    ops: OpsSource,
    snapshot: OpsConfig,
    openai: AsyncOpenAI,
    small: ChatDeploymentSettings,
    tokenizer: TextTokenizer,
    sql: SqlEngine,
    tenant_id: UUID,
    workload_principal_id: UUID,
    clock: Clock,
) -> PostTurnWorker:
    """The worker's after-response work over its containers, the chat-small deployment, the
    chat encoding (`tokenizer`, the unit of `historyTokens`, the summary and the facts) and the
    data-access layer that writes the audit rows."""
    model = SmallModel(openai, small, clock=clock)
    return PostTurnWorker(
        conversations,
        summary=SummaryRebuilder(
            conversations, model=model, tokenizer=tokenizer, tenant_id=tenant_id, clock=clock
        ),
        extraction=MemoryExtractor(
            conversations,
            memories,
            model=model,
            tokenizer=tokenizer,
            tenant_id=tenant_id,
            workload_principal_id=workload_principal_id,
            audit=partial(write_audit_event_after_commit, sql),
            clock=clock,
        ),
        ops=ops,
        snapshot=snapshot,
        clock=clock,
    )
