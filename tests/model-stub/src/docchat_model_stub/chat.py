"""`POST /openai/v1/chat/completions` (84-testing.md section 2.3).

The reasoning models behind every deployment refuse sampling parameters and accept only some
`reasoning_effort` values, and the stub answers those requests the same way, so a configuration
the real deployments would refuse fails in stub-backed tests too. Plain text, structured output
and streamed answers are built here; the order in which a request is checked is `app.py`'s.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import math
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, model_validator

if TYPE_CHECKING:
    import re
    from collections.abc import AsyncIterator, Awaitable, Callable

    from docchat_model_stub.directives import Kind
    from docchat_model_stub.recorder import DeploymentStats

UNSUPPORTED_PARAMETERS = (
    "temperature",
    "top_p",
    "max_tokens",
    "presence_penalty",
    "frequency_penalty",
)
REASONING_EFFORTS = ("none", "low", "medium", "high")

# The constant word list of every plain-text and streamed answer, repeated as needed. The first
# sentence is short, so citation markers follow early in a streamed answer.
_ANSWER_TEXT = (
    "The stub model answers from the sources it was given. "
    "It writes plain words at a steady pace so that tests can measure timing, "
    "cancellation and usage. Every word comes from this fixed list, and the list "
    "repeats from the start whenever an answer needs more words than it holds."
)
WORDS = tuple(_ANSWER_TEXT.split())

_CHARACTERS_PER_TOKEN = 4
_MAX_CITED_SOURCES = 2
_MS_PER_SECOND = 1000


class TextPart(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    type: StrictStr
    text: StrictStr | None = None


class Message(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    role: StrictStr
    content: StrictStr | list[TextPart] | None = None

    def text(self) -> str:
        if self.content is None:
            return ""
        if isinstance(self.content, str):
            return self.content
        return "".join(part.text or "" for part in self.content)


class StreamOptions(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    include_usage: StrictBool = False


class JsonSchemaFormat(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    name: StrictStr
    schema_: dict[str, Any] = Field(default_factory=dict, alias="schema")


class ResponseFormat(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    type: StrictStr
    json_schema: JsonSchemaFormat | None = None

    @model_validator(mode="after")
    def _schema_present(self) -> ResponseFormat:
        if self.type == "json_schema" and self.json_schema is None:
            raise ValueError("a json_schema response format needs its json_schema object")
        return self


class ChatRequest(BaseModel):
    """The fields the stub reads; any other parameter is accepted, recorded and ignored."""

    model_config = ConfigDict(extra="ignore", strict=True)

    model: StrictStr
    messages: list[Message] = Field(min_length=1)
    stream: StrictBool = False
    stream_options: StreamOptions | None = None
    response_format: ResponseFormat | None = None

    def kind(self) -> Kind:
        if self.stream:
            return "stream"
        if self.json_schema() is not None:
            return "json"
        return "text"

    def json_schema(self) -> JsonSchemaFormat | None:
        if self.response_format is not None and self.response_format.type == "json_schema":
            return self.response_format.json_schema
        return None

    def last_user_text(self) -> str:
        """The text of the last `user` message, the only place a chat request's directives sit."""
        for message in reversed(self.messages):
            if message.role == "user":
                return message.text()
        return ""

    def prompt_tokens(self) -> int:
        """The usage estimate: one token per four characters of all messages, rounded up."""
        characters = sum(len(message.text()) for message in self.messages)
        return math.ceil(characters / _CHARACTERS_PER_TOKEN)

    def include_usage(self) -> bool:
        return self.stream_options is not None and self.stream_options.include_usage


@dataclass(frozen=True)
class ParameterProblem:
    code: Literal["unsupported_parameter", "unsupported_value"]
    param: str
    message: str


def parameter_problem(body: dict[str, Any]) -> ParameterProblem | None:
    """The first parameter the reasoning models would refuse, or `None`."""
    for name in UNSUPPORTED_PARAMETERS:
        if name in body:
            return ParameterProblem(
                "unsupported_parameter", name, f"Unsupported parameter: '{name}'."
            )
    if "reasoning_effort" in body and body["reasoning_effort"] not in REASONING_EFFORTS:
        return ParameterProblem(
            "unsupported_value",
            "reasoning_effort",
            "Unsupported value: 'reasoning_effort' must be one of "
            + ", ".join(REASONING_EFFORTS)
            + ".",
        )
    return None


def words(count: int) -> list[str]:
    return list(itertools.islice(itertools.cycle(WORDS), count))


def cited_sources(request: ChatRequest, pattern: re.Pattern[str]) -> list[str]:
    """Up to two distinct source IDs found in the request messages, in order of appearance."""
    found: list[str] = []
    for message in request.messages:
        for match in pattern.finditer(message.text()):
            source_id = match.group(1)
            if source_id not in found:
                found.append(source_id)
                if len(found) == _MAX_CITED_SOURCES:
                    return found
    return found


def _completion_id() -> str:
    return f"chatcmpl-stub-{uuid.uuid4().hex}"


def _usage(prompt_tokens: int, completion_tokens: int) -> dict[str, Any]:
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "completion_tokens_details": {"reasoning_tokens": 0},
        "prompt_tokens_details": {"cached_tokens": 0},
    }


def completion_body(request: ChatRequest, content: str, completion_tokens: int) -> dict[str, Any]:
    """A non-streamed `chat.completion` holding `content`."""
    return {
        "id": _completion_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": request.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content, "refusal": None},
                "finish_reason": "stop",
                "logprobs": None,
            }
        ],
        "usage": _usage(request.prompt_tokens(), completion_tokens),
    }


def text_content(count: int) -> tuple[str, int]:
    """A plain-text answer of `count` words and its completion-token count (the words)."""
    return " ".join(words(count)), count


def json_content(value: Any) -> tuple[str, int]:
    """The content of a structured answer and its completion-token estimate.

    A scripted string is sent as it stands, so a test can script content that doesn't parse.
    """
    content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return content, math.ceil(len(content) / _CHARACTERS_PER_TOKEN)


@dataclass(frozen=True)
class StreamPlan:
    """Everything a streamed answer needs, resolved from the settings and the directives."""

    deployment: str
    prompt_tokens: int
    words: int
    words_per_chunk: int
    first_delay_ms: int
    word_delay_ms: int
    citations: tuple[str, ...]
    include_usage: bool
    finish_reason: Literal["stop", "content_filter"]
    cut_after: int | None


def _chunk_contents(plan: StreamPlan) -> list[tuple[str, int]]:
    """Each content chunk's text and word count, with the markers after the first sentence."""
    answer = words(plan.words)
    marker = "".join(f"[{source_id}]" for source_id in plan.citations)
    marker_pending = bool(marker)
    chunks: list[tuple[str, int]] = []
    for start in range(0, len(answer), plan.words_per_chunk):
        chunk_words = answer[start : start + plan.words_per_chunk]
        rendered: list[str] = []
        for word in chunk_words:
            rendered.append(word)
            if marker_pending and word.endswith("."):
                rendered.append(marker)
                marker_pending = False
        text = " ".join(rendered)
        chunks.append((text if start == 0 else f" {text}", len(chunk_words)))
    return chunks


def _event(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def _chunk(
    completion_id: str,
    created: int,
    plan: StreamPlan,
    delta: dict[str, Any],
    finish_reason: str | None = None,
) -> bytes:
    return _event(
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": plan.deployment,
            "choices": [
                {"index": 0, "delta": delta, "finish_reason": finish_reason, "logprobs": None}
            ],
        }
    )


async def stream_events(
    plan: StreamPlan,
    is_disconnected: Callable[[], Awaitable[bool]],
    stats: DeploymentStats,
) -> AsyncIterator[bytes]:
    """The server-sent events of a streamed answer.

    Before each chunk the generator asks the request whether the client has gone; Starlette
    may instead cancel the generator when it sees the disconnect first. Either way the
    deployment's `clientDisconnected` counter moves once and the stream stops.
    """
    completion_id = _completion_id()
    created = int(time.time())
    sent_words = 0

    async def gone() -> bool:
        if await is_disconnected():
            stats.client_disconnected += 1
            return True
        return False

    try:
        if await gone():
            return
        yield _chunk(completion_id, created, plan, {"role": "assistant", "content": ""})
        for index, (content, word_count) in enumerate(_chunk_contents(plan)):
            if index == plan.cut_after:
                return
            delay_ms = plan.first_delay_ms if index == 0 else plan.word_delay_ms * word_count
            await asyncio.sleep(delay_ms / _MS_PER_SECOND)
            if await gone():
                return
            yield _chunk(completion_id, created, plan, {"content": content})
            sent_words += word_count
        # A cut at or beyond the last content chunk still withholds the final chunk.
        if plan.cut_after is not None or await gone():
            return
        yield _chunk(completion_id, created, plan, {}, plan.finish_reason)
        if plan.include_usage:
            yield _event(
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": plan.deployment,
                    "choices": [],
                    "usage": _usage(plan.prompt_tokens, sent_words),
                }
            )
        yield b"data: [DONE]\n\n"
        stats.completed += 1
    except asyncio.CancelledError:
        stats.client_disconnected += 1
        raise
