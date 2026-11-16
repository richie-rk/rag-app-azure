"""The three chat-small calls of a turn: the question rewrite, the search passage and the
follow-ups (`12-chat-pipeline.md` sections 5, 7.2, 13 and 17.2 to 17.5).

Each builds its messages from constant text and defanged inserted text in blocks with a marker of
its own; a marker that still collides after 3 redraws means the call isn't made and its fallback
applies. Token cuts use the chat-small encoding and follow the 20,000-character rule, so a long
answer is cut in a worker thread.

- Rewrite: the summary, the newest `chat.rewrite_history_turns` earlier turns (each question and
  answer cut to 300 tokens) and the question; accepted when the trimmed `query` holds 1 to 1,200
  code points and no `[[BEGIN` or `[[END`, otherwise the question is searched as written.
- Search passage: the search query; accepted on the same rules for `passage`.
- Follow-ups: the question, the answer without citation tokens cut to 800 tokens, and for the top
  `followups.source_count` sources sent, their title, heading items and the first
  `followups.source_excerpt_tokens` tokens of their chunk text; the items are validated.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from docchat_api.chat.calls import (
    FOLLOWUP_QUESTIONS_FORMAT,
    SEARCH_PASSAGE_FORMAT,
    STANDALONE_QUERY_FORMAT,
    FollowupQuestions,
    ModelCalls,
    SearchPassage,
    StandaloneQuery,
    UsageLedger,
)
from docchat_api.chat.followups import validate_followups
from docchat_api.chat.prompts import (
    MarkerCollisionError,
    Message,
    TextTokenizer,
    block,
    blocks,
    defang,
    draw_marker,
    first_tokens,
    followups_system,
    has_boundary_text,
    inline_countable,
    rewrite_system,
    search_passage_system,
)
from docchat_api.chat.sources import HEADING_SEPARATOR, NumberedSource
from docchat_api.conversations.store import HistoryTurn
from docchat_core.chat import strip_citation_tokens
from docchat_core.cosmos.conversations import CallOutcome, ModelCall, ModelCallUsage
from docchat_core.opsconfig import ChatOps, FollowupsOps, RetrievalOps

REWRITE_TURN_TOKENS: Final = 300
FOLLOWUPS_ANSWER_TOKENS: Final = 800
ACCEPTED_MAX_CODE_POINTS: Final = 1_200


async def _cut_all(tokenizer: TextTokenizer, texts: Sequence[str], limit: int) -> list[str]:
    """The first `limit` tokens of each text, in one worker-thread call when they are long."""

    def cut() -> list[str]:
        return [first_tokens(tokenizer, text, limit) for text in texts]

    if inline_countable(texts):
        return cut()
    return await asyncio.to_thread(cut)


def accepted(text: str) -> str | None:
    """A rewrite or passage output, trimmed, when it holds 1 to 1,200 code points and no block
    boundary text; otherwise `None`."""
    trimmed = text.strip()
    if not 1 <= len(trimmed) <= ACCEPTED_MAX_CODE_POINTS or has_boundary_text(trimmed):
        return None
    return trimmed


@dataclass(frozen=True, slots=True)
class SmallOutcome:
    """A chat-small call's accepted text (or items), and its duration; `None` when the call
    took its fallback, `duration_ms` `None` when no call was made."""

    text: str | None
    duration_ms: int | None
    outcome: CallOutcome | None


async def rewrite(
    calls: ModelCalls,
    *,
    question: str,
    summary: str | None,
    history: Sequence[HistoryTurn],
    chat: ChatOps,
    ledger: UsageLedger,
) -> SmallOutcome:
    """The question rewrite (section 17.2)."""
    tokenizer = calls.small.tokenizer
    turns = list(history)[-chat.rewrite_history_turns :]
    cut = await _cut_all(
        tokenizer,
        [text for turn in turns for text in (turn.question, turn.answer)],
        REWRITE_TURN_TOKENS,
    )
    exchanges = [
        f"User: {defang(cut[2 * index])}\nAssistant: {defang(cut[2 * index + 1])}"
        for index in range(len(turns))
    ]
    inserted = [*exchanges, defang(question)]
    if summary is not None:
        inserted.append(defang(summary))
    try:
        marker = draw_marker(inserted)
    except MarkerCollisionError:
        return SmallOutcome(None, None, None)
    parts: list[str] = []
    if summary is not None:
        parts.append(block("CONVERSATION_SUMMARY", marker, defang(summary)))
    if exchanges:
        parts.append(block("EARLIER_EXCHANGE", marker, "\n\n".join(exchanges)))
    parts.append(block("LATEST_MESSAGE", marker, defang(question)))
    reply = await calls.complete_json(
        call=ModelCall.REWRITE,
        messages=[Message("system", rewrite_system(marker)), Message("user", blocks(*parts))],
        response_format=STANDALONE_QUERY_FORMAT,
        model_type=StandaloneQuery,
        max_completion_tokens=chat.small_call_max_completion_tokens,
        timeout_seconds=chat.rewrite_timeout_seconds,
        ledger=ledger,
    )
    query = accepted(reply.value.query) if reply.value is not None else None
    return SmallOutcome(query, reply.duration_ms, reply.outcome)


async def search_passage(
    calls: ModelCalls,
    *,
    query: str,
    chat: ChatOps,
    retrieval: RetrievalOps,
    ledger: UsageLedger,
) -> SmallOutcome:
    """The broadened search's passage (section 17.3)."""
    inserted = defang(query)
    try:
        marker = draw_marker([inserted])
    except MarkerCollisionError:
        return SmallOutcome(None, None, None)
    reply = await calls.complete_json(
        call=ModelCall.SEARCH_PASSAGE,
        messages=[
            Message("system", search_passage_system(marker)),
            Message("user", block("SEARCH_QUERY", marker, inserted)),
        ],
        response_format=SEARCH_PASSAGE_FORMAT,
        model_type=SearchPassage,
        max_completion_tokens=chat.small_call_max_completion_tokens,
        timeout_seconds=retrieval.broaden_timeout_seconds,
        ledger=ledger,
    )
    passage = accepted(reply.value.passage) if reply.value is not None else None
    return SmallOutcome(passage, reply.duration_ms, reply.outcome)


@dataclass(frozen=True, slots=True)
class FollowupsReply:
    """The validated follow-up questions (empty on any fallback) and the call's usage record,
    `None` when no call was made."""

    questions: list[str]
    record: asyncio.Task[ModelCallUsage] | None


async def followups(
    calls: ModelCalls,
    *,
    question: str,
    answer: str,
    sources: Sequence[NumberedSource],
    chat: ChatOps,
    options: FollowupsOps,
    ledger: UsageLedger,
) -> FollowupsReply:
    """The follow-up suggestions (section 17.5)."""
    tokenizer = calls.small.tokenizer
    top = list(sources)[: options.source_count]
    cut = await _cut_all(
        tokenizer,
        [strip_citation_tokens(answer)],
        FOLLOWUPS_ANSWER_TOKENS,
    )
    excerpts = await _cut_all(
        tokenizer,
        [source.candidate.hit.chunk_text for source in top],
        options.source_excerpt_tokens,
    )
    bodies: list[str] = []
    for source, excerpt in zip(top, excerpts, strict=True):
        lines = [f"Title: {source.candidate.title}"]
        headings = source.candidate.heading_items
        if headings:
            lines.append(f"Section: {HEADING_SEPARATOR.join(headings)}")
        lines.extend(("", defang(excerpt)))
        bodies.append("\n".join(lines))
    answer_excerpt = defang(cut[0])
    try:
        marker = draw_marker([defang(question), answer_excerpt, *bodies])
    except MarkerCollisionError:
        return FollowupsReply([], None)
    parts = [block("QUESTION", marker, defang(question)), block("ANSWER", marker, answer_excerpt)]
    parts.extend(
        block("SOURCE", marker, body, source_id=f"S{position}")
        for position, body in enumerate(bodies, start=1)
    )
    reply = await calls.complete_json(
        call=ModelCall.FOLLOWUPS,
        messages=[
            Message("system", followups_system(marker, options.max_count)),
            Message("user", blocks(*parts)),
        ],
        response_format=FOLLOWUP_QUESTIONS_FORMAT,
        model_type=FollowupQuestions,
        max_completion_tokens=chat.small_call_max_completion_tokens,
        timeout_seconds=options.timeout_seconds,
        ledger=ledger,
    )
    if reply.value is None:
        return FollowupsReply([], reply.record)
    return FollowupsReply(
        validate_followups(reply.value.questions, question, options.max_count), reply.record
    )
