"""Prompt assembly for the answer call (`12-chat-pipeline.md` sections 9.1 to 9.10 and 17.4;
`82-security.md` SEC-32 to SEC-34 and SEC-38).

The messages, in order: `system` (the platform rules, the block marker line and, when the
project's instructions are non-empty, the `PROJECT_GUIDANCE` block); `user`, only when memory lines
or a summary exist (the context line, `USER_PREFERENCES` and `CONVERSATION_SUMMARY`); the earlier
turns, oldest first, as `user` and `assistant` messages with no block; `user` with the sources
(`SOURCE` blocks in rank order, or the no-sources line); and `user` holding only the `QUESTION`
block. Sources, memory and the summary sit in user-role messages, so only the service's rules and
the administrators' guidance carry system authority, and the question is the latest user message,
the only one the platform filters judge.

The budget is `chat.context_token_budget` - `chat.answer_max_completion_tokens`, counted with the
chat deployment's encoding by the rule of section 9.9. While the count is over it, the oldest
earlier turn goes first (its question and answer together), then the lowest-ranked source; the
system, context and question messages are never dropped. A prompt still over the budget with
neither left is `PromptTooLargeError`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from docchat_api.chat.markers import allowed_links
from docchat_api.chat.prompts import (
    ANSWER_MARKER_LINE,
    CONTEXT_LINE,
    NO_SOURCES_LINE,
    PLATFORM_RULES,
    SOURCES_HEADING,
    Message,
    TextTokenizer,
    TokenHex,
    block,
    blocks,
    count_messages,
    defang,
    draw_marker,
    render_memory_block,
)
from docchat_api.chat.sources import NumberedSource
from docchat_api.conversations.store import HistoryTurn

PROJECT_GUIDANCE: Final = "PROJECT_GUIDANCE"
USER_PREFERENCES: Final = "USER_PREFERENCES"
CONVERSATION_SUMMARY: Final = "CONVERSATION_SUMMARY"
SOURCE: Final = "SOURCE"
QUESTION: Final = "QUESTION"


class PromptTooLargeError(Exception):
    """The prompt is over the budget with no earlier turn and no source left to drop."""


@dataclass(frozen=True, slots=True, kw_only=True)
class PromptInputs:
    """Everything the answer prompt is made of, as the turn gathered it."""

    question: str
    instructions: str
    memory_lines: Sequence[str]
    summary: str | None
    history: Sequence[HistoryTurn]
    sources: Sequence[NumberedSource]


@dataclass(slots=True, kw_only=True)
class AssembledPrompt:
    """The answer call's messages as sent, and what assembly records."""

    messages: list[Message]
    marker: str
    prompt_tokens: int
    dropped_turns: int
    dropped_source_ids: list[str]
    sent: list[NumberedSource]
    memory_facts_included: int
    summary_included: bool
    allowed_links: list[str] = field(default_factory=list)


def _system(marker: str, instructions: str) -> Message:
    content = PLATFORM_RULES + "\n\n" + ANSWER_MARKER_LINE + marker
    if instructions:
        content += "\n\n" + block(PROJECT_GUIDANCE, marker, defang(instructions))
    return Message("system", content)


def _context(marker: str, memory_lines: Sequence[str], summary: str | None) -> Message | None:
    if not memory_lines and summary is None:
        return None
    content = CONTEXT_LINE
    if memory_lines:
        content += "\n\n" + block(USER_PREFERENCES, marker, render_memory_block(memory_lines))
    if summary is not None:
        content += "\n\n" + block(CONVERSATION_SUMMARY, marker, defang(summary))
    return Message("user", content)


def _turn_messages(turn: HistoryTurn) -> list[Message]:
    return [Message("user", defang(turn.question)), Message("assistant", defang(turn.answer))]


def _sources_message(marker: str, sources: Sequence[NumberedSource]) -> Message:
    if not sources:
        return Message("user", NO_SOURCES_LINE)
    source_blocks = [
        block(SOURCE, marker, source.body, source_id=source.source_id) for source in sources
    ]
    return Message("user", SOURCES_HEADING + "\n\n" + blocks(*source_blocks))


def _question(marker: str, question: str) -> Message:
    return Message("user", block(QUESTION, marker, defang(question)))


def inserted_texts(inputs: PromptInputs) -> list[str]:
    """Every defanged text assembly inserts, which the marker must not appear in."""
    texts = [defang(inputs.instructions), defang(inputs.question)]
    texts.extend(render_memory_block(inputs.memory_lines).splitlines())
    if inputs.summary is not None:
        texts.append(defang(inputs.summary))
    for turn in inputs.history:
        texts.extend((defang(turn.question), defang(turn.answer)))
    texts.extend(source.body for source in inputs.sources)
    return texts


def _messages(
    marker: str,
    inputs: PromptInputs,
    instructions: str,
    history: Sequence[HistoryTurn],
    sources: Sequence[NumberedSource],
) -> list[Message]:
    messages = [_system(marker, instructions)]
    context = _context(marker, inputs.memory_lines, inputs.summary)
    if context is not None:
        messages.append(context)
    for turn in history:
        messages.extend(_turn_messages(turn))
    messages.append(_sources_message(marker, sources))
    messages.append(_question(marker, inputs.question))
    return messages


async def assemble(
    inputs: PromptInputs,
    *,
    tokenizer: TextTokenizer,
    budget_tokens: int,
    token_hex: TokenHex | None = None,
) -> AssembledPrompt:
    """The answer prompt within `budget_tokens`; raises `MarkerCollisionError` or
    `PromptTooLargeError`."""
    instructions = inputs.instructions.strip()
    texts = inserted_texts(inputs)
    marker = draw_marker(texts) if token_hex is None else draw_marker(texts, token_hex)
    history = list(inputs.history)
    sources = list(inputs.sources)
    dropped_turns = 0
    dropped_sources: list[str] = []
    messages = _messages(marker, inputs, instructions, history, sources)
    count = await count_messages(tokenizer, messages)
    while count > budget_tokens:
        if history:
            history.pop(0)
            dropped_turns += 1
        elif sources:
            dropped_sources.append(sources.pop().source_id)
        else:
            raise PromptTooLargeError("the prompt is over its budget with nothing left to drop")
        messages = _messages(marker, inputs, instructions, history, sources)
        count = await count_messages(tokenizer, messages)
    return AssembledPrompt(
        messages=messages,
        marker=marker,
        prompt_tokens=count,
        dropped_turns=dropped_turns,
        dropped_source_ids=list(reversed(dropped_sources)),
        sent=sources,
        memory_facts_included=len(inputs.memory_lines),
        summary_included=inputs.summary is not None,
        allowed_links=allowed_links(source.body for source in sources),
    )
