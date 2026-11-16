"""In-memory doubles of the chat pipeline's services without an emulator (`84-testing.md` sections
1.1 principle 3, 1.4 and 2.4).

They plug into the composition root through `docchat_api.chat.service.ChatPorts`, so the pipeline
runs unchanged over them:

- `FakeModelClient`, an `AsyncOpenAI` double: `chat.completions.create` answers a streamed call
  from its queue of scripted streams (a default answer citing `[S1]` otherwise), a structured
  call from its queue per schema name (a default object otherwise: an empty list of memory
  operations for `memory_operations`) and a plain-text call (the conversation store's `summary`)
  from the queue `PLAIN_TEXT` (a default summary otherwise); `embeddings.create` returns a unit
  vector of the requested length. Every request is recorded, and every stream remembers whether
  it was closed.
- `FakeSearchGateway`, the search client factory: each physical index name gets a client whose
  `search` records its arguments and yields the next scripted result list, else the chunks
  uploaded to that index (filtered by `documentId` as the request asks; a deleted chunk is gone),
  else the default list, so domain flows see served, unserved and partial responses.
- `FakePromptShields`, the Prompt Shields endpoint behind an `httpx.MockTransport`: like the model
  stub's, it refuses more than 5 documents or 10,000 characters in one call with
  `400 InvalidRequestBody` and flags a text holding `[[stub:shield=attack]]`; queued responses or
  exceptions answer first.

The model stub (a separate process) stays the service the integration tests use; these doubles
serve unit tests and the in-process authorisation matrix.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Final, cast
from uuid import UUID

import httpx

from docchat_api.chat.service import ChatPorts
from docchat_core.clients.tls import TLS_CONTEXT
from docchat_core.ingestion.settings import TokenEncoding
from docchat_core.search.schema import ChunkIndexFields

if TYPE_CHECKING:
    from azure.search.documents.aio import SearchClient
    from openai import AsyncOpenAI

SHIELD_ATTACK: Final = "[[stub:shield=attack]]"
SHIELD_MAX_DOCUMENTS: Final = 5
SHIELD_MAX_CHARACTERS: Final = 10_000
CONTENTSAFETY_BASE_URL: Final = "https://contentsafety.docchat-test.invalid"
DEFAULT_ANSWER: Final = ("The answer is in the first source", " [S1].")
PLAIN_TEXT: Final = "plain_text"
"""The reply queue of non-streamed calls without a response format."""
DEFAULT_SUMMARY: Final = "The user asked about the policy and the assistant explained it."
FIELDS: Final = ChunkIndexFields.for_version(1)


# --- The model client ---------------------------------------------------------------------------


def chunk(
    content: str | None = None,
    *,
    finish_reason: str | None = None,
    usage: Mapping[str, Any] | None = None,
    filter_error: bool = False,
) -> SimpleNamespace:
    """One streamed chunk; with `usage` and no content, the final usage chunk (no choices)."""
    if usage is not None and content is None and finish_reason is None:
        return SimpleNamespace(choices=[], usage=_namespace(usage))
    choice = SimpleNamespace(
        delta=SimpleNamespace(content=content),
        finish_reason=finish_reason,
        content_filter_results={"error": {"code": "unavailable"}} if filter_error else None,
    )
    return SimpleNamespace(choices=[choice], usage=None)


def _namespace(value: Any) -> Any:
    if isinstance(value, Mapping):
        return SimpleNamespace(**{key: _namespace(item) for key, item in value.items()})
    return value


def usage_of(
    prompt: int, completion: int, *, reasoning: int = 0, cached: int = 0, cache_write: int = 0
) -> dict[str, Any]:
    """A usage object as the service reports it."""
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "prompt_tokens_details": {"cached_tokens": cached, "cache_write_tokens": cache_write},
        "completion_tokens_details": {"reasoning_tokens": reasoning},
    }


def text_stream(
    texts: Sequence[str],
    *,
    finish_reason: str | None = "stop",
    usage: Mapping[str, Any] | None = None,
) -> list[SimpleNamespace]:
    """The chunks of an answer: one per text, the finish reason on the last, then the usage."""
    chunks = [chunk(text) for text in texts]
    if finish_reason is not None:
        chunks.append(chunk(finish_reason=finish_reason))
    if usage is not None:
        chunks.append(chunk(usage=usage))
    return chunks


class FakeStream:
    """A streamed response: its chunks, then an optional failure; `closed` once closed."""

    def __init__(self, chunks: Sequence[Any], failure: BaseException | None = None) -> None:
        self._chunks = list(chunks)
        self._failure = failure
        self.closed = False
        self.gate: Any = None
        """An `asyncio.Event` a test sets to let the stream continue past its chunks."""

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[Any]:
        for item in self._chunks:
            yield item
        if self.gate is not None:
            await self.gate.wait()
        if self._failure is not None:
            raise self._failure

    async def close(self) -> None:
        self.closed = True


def completion(
    content: str | None,
    *,
    finish_reason: str = "stop",
    refusal: str | None = None,
    usage: Mapping[str, Any] | None = None,
) -> SimpleNamespace:
    """A non-streamed completion with one choice."""
    message = SimpleNamespace(content=content, refusal=refusal)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=_namespace(usage) if usage is not None else None,
    )


type Scripted = Any
"""A scripted answer: a response object, a `FakeStream`, a list of chunks, or an exception."""


@dataclass
class FakeModelClient:
    """An `AsyncOpenAI` double that records every request."""

    streams: deque[Scripted] = field(default_factory=deque)
    replies: defaultdict[str, deque[Scripted]] = field(default_factory=lambda: defaultdict(deque))
    embeddings_script: deque[Scripted] = field(default_factory=deque)
    requests: list[dict[str, Any]] = field(default_factory=list)
    opened: list[FakeStream] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.embeddings = SimpleNamespace(create=self._embed)

    def client(self) -> AsyncOpenAI:
        return cast("AsyncOpenAI", self)

    def chat_requests(self) -> list[dict[str, Any]]:
        return [request for request in self.requests if request["kind"] == "chat"]

    def embedding_requests(self) -> list[dict[str, Any]]:
        return [request for request in self.requests if request["kind"] == "embed"]

    def answer_requests(self) -> list[dict[str, Any]]:
        return [request for request in self.chat_requests() if request.get("stream")]

    def json_requests(self, name: str) -> list[dict[str, Any]]:
        return [
            request
            for request in self.chat_requests()
            if not request.get("stream")
            and "response_format" in request
            and request["response_format"]["json_schema"]["name"] == name
        ]

    def text_requests(self) -> list[dict[str, Any]]:
        """The non-streamed requests without a response format (the `summary` calls)."""
        return [
            request
            for request in self.chat_requests()
            if not request.get("stream") and "response_format" not in request
        ]

    async def _create(self, **arguments: Any) -> Any:
        self.requests.append({"kind": "chat", **arguments})
        if arguments.get("stream"):
            scripted = self.streams.popleft() if self.streams else None
            if isinstance(scripted, BaseException):
                raise scripted
            if scripted is None:
                scripted = FakeStream(text_stream(DEFAULT_ANSWER, usage=usage_of(100, 12)))
            elif not isinstance(scripted, FakeStream):
                scripted = FakeStream(scripted)
            self.opened.append(scripted)
            return scripted
        response_format = arguments.get("response_format")
        name = PLAIN_TEXT if response_format is None else response_format["json_schema"]["name"]
        queue = self.replies[name]
        scripted = queue.popleft() if queue else None
        if isinstance(scripted, BaseException):
            raise scripted
        if scripted is None and name == PLAIN_TEXT:
            return completion(DEFAULT_SUMMARY, usage=usage_of(50, 10))
        if scripted is None:
            return completion(json.dumps(_default_reply(name)), usage=usage_of(50, 10))
        if isinstance(scripted, str):
            return completion(scripted, usage=usage_of(50, 10))
        return scripted

    async def _embed(self, **arguments: Any) -> Any:
        self.requests.append({"kind": "embed", **arguments})
        scripted = self.embeddings_script.popleft() if self.embeddings_script else None
        if isinstance(scripted, BaseException):
            raise scripted
        if scripted is not None:
            return scripted
        dimensions = int(arguments.get("dimensions") or 1024)
        vector = [0.0] * dimensions
        vector[0] = 1.0
        return SimpleNamespace(
            data=[SimpleNamespace(embedding=vector)], usage=_namespace({"prompt_tokens": 7})
        )


def _default_reply(name: str) -> dict[str, Any]:
    if name == "standalone_query":
        return {"query": "standalone search query"}
    if name == "search_passage":
        return {"passage": "A passage that reads like a document extract."}
    if name == "memory_operations":
        return {"operations": []}
    return {"questions": ["What else does the policy cover?"]}


# --- Search ---------------------------------------------------------------------------------------


def search_result(
    *,
    document_id: UUID,
    chunk_set_id: UUID,
    version_id: UUID,
    ordinal: int = 0,
    title: str = "A document",
    headings: Sequence[str] = ("Section",),
    text: str = "Chunk text.",
    page: int | None = 1,
    content_type: str = "application/pdf",
    score: float = 1.0,
    reranker_score: float | None = 3.0,
) -> dict[str, Any]:
    """One `docs/search` result of schema version 1, as the async pager yields it."""
    return {
        FIELDS.key: f"{document_id}_{chunk_set_id}_{ordinal}",
        FIELDS.document_id: str(document_id),
        FIELDS.document_version_id: str(version_id),
        FIELDS.chunk_set_id: str(chunk_set_id),
        FIELDS.chunk_ordinal: ordinal,
        FIELDS.title: title,
        FIELDS.heading_path: list(headings),
        FIELDS.page_number: page,
        FIELDS.content_type: content_type,
        FIELDS.chunk_text: text,
        "@search.score": score,
        "@search.reranker_score": reranker_score,
    }


class _Results:
    def __init__(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self._rows = list(rows)

    def __aiter__(self) -> AsyncIterator[Mapping[str, Any]]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[Mapping[str, Any]]:
        for row in self._rows:
            yield row


_ID_FILTER: Final = re.compile(
    r"^(?:search\.in\((?P<in_field>\w+), '(?P<values>[^']*)', ','\)"
    r"|(?P<eq_field>\w+) eq '(?P<value>[^']*)')$"
)
STORED_RERANKER_SCORE: Final = 3.0
"""The reranker score a stored chunk gets: inside the semantic ranker's 0 to 4 scale."""


def matches_filter(document: Mapping[str, Any], expression: str | None) -> bool:
    """Whether a stored chunk passes the two filter forms the code sends: `search.in(<field>,
    '<v1>,<v2>', ',')` and `<field> eq '<v>'`; any other expression is refused, so a new form
    is written into the contract before the fake accepts it."""
    if expression is None:
        return True
    found = _ID_FILTER.match(expression)
    if found is None:
        raise ValueError(f"the search fake doesn't evaluate the filter {expression!r}")
    if found.group("in_field") is not None:
        return str(document.get(found.group("in_field"))) in found.group("values").split(",")
    return str(document.get(found.group("eq_field"))) == found.group("value")


@dataclass
class _FakeSearchClient:
    gateway: FakeSearchGateway
    name: str

    async def search(self, search_text: str | None = None, **arguments: Any) -> _Results:
        self.gateway.requests.append({"index": self.name, "search_text": search_text, **arguments})
        scripted = self.gateway.script.popleft() if self.gateway.script else None
        if isinstance(scripted, BaseException):
            raise scripted
        if scripted is not None:
            return _Results(scripted)
        stored = self.gateway.indexes.get(self.name)
        if stored is not None:
            top = int(arguments.get("top") or len(stored) or 1)
            rows = [
                {"@search.score": 1.0, "@search.reranker_score": STORED_RERANKER_SCORE, **document}
                for document in stored.values()
                if matches_filter(document, arguments.get("filter"))
            ]
            return _Results(rows[:top])
        return _Results(self.gateway.results)

    async def upload_documents(self, documents: list[dict[str, Any]], **_: Any) -> list[Any]:
        """Store each chunk under its key (an upload replaces a chunk of the same key)."""
        stored = self.gateway.indexes.setdefault(self.name, {})
        for document in documents:
            stored[str(document[FIELDS.key])] = dict(document)
        return [
            SimpleNamespace(key=str(document[FIELDS.key]), succeeded=True, status_code=201)
            for document in documents
        ]

    async def delete_documents(self, documents: list[dict[str, Any]], **_: Any) -> list[Any]:
        """Remove each chunk by its key; an absent key counts as deleted, as in the service."""
        stored = self.gateway.indexes.setdefault(self.name, {})
        for document in documents:
            stored.pop(str(document[FIELDS.key]), None)
        return [
            SimpleNamespace(key=str(document[FIELDS.key]), succeeded=True, status_code=200)
            for document in documents
        ]

    async def close(self) -> None:
        return None


@dataclass
class FakeSearchGateway:
    """The search client factory of the pipeline: `gateway(physical_index_name)`.

    A search answers, in order: the next scripted result list (or error); the chunks uploaded to
    that physical index, filtered as the request's `filter` says, each with a reranker score
    inside the 0 to 4 scale; otherwise the default `results`."""

    results: list[dict[str, Any]] = field(default_factory=list)
    script: deque[Scripted] = field(default_factory=deque)
    requests: list[dict[str, Any]] = field(default_factory=list)
    indexes: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    """The chunks stored per physical index name, by key."""

    def __call__(self, physical_index_name: str) -> SearchClient:
        return cast("SearchClient", _FakeSearchClient(self, physical_index_name))


# --- Prompt Shields -------------------------------------------------------------------------------


@dataclass
class FakePromptShields:
    """The `text:shieldPrompt` endpoint behind a mock transport."""

    queued: deque[httpx.Response | BaseException] = field(default_factory=deque)
    requests: list[dict[str, Any]] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        if self.queued:
            answer = self.queued.popleft()
            if isinstance(answer, BaseException):
                raise answer
            return answer
        documents: list[str] = body.get("documents", [])
        if len(documents) > SHIELD_MAX_DOCUMENTS or (
            sum(len(text) for text in documents) > SHIELD_MAX_CHARACTERS
        ):
            return httpx.Response(
                400, json={"error": {"code": "InvalidRequestBody", "message": "too much"}}
            )
        return httpx.Response(
            200,
            json={
                "userPromptAnalysis": {
                    "attackDetected": SHIELD_ATTACK in body.get("userPrompt", "")
                },
                "documentsAnalysis": [
                    {"attackDetected": SHIELD_ATTACK in text} for text in documents
                ],
            },
        )

    def client(self) -> httpx.AsyncClient:
        """A client of the fake endpoint; build it outside the test's event loop."""
        return httpx.AsyncClient(
            base_url=CONTENTSAFETY_BASE_URL,
            transport=httpx.MockTransport(self.handle),
            timeout=httpx.Timeout(5.0),
            follow_redirects=False,
            trust_env=False,
            verify=TLS_CONTEXT,
        )


class CharacterTokenizer:
    """One token per code point, reversible: a stand-in for an encoding where none can be
    loaded (the `tiktoken` files come from the image's cache, which tests don't have)."""

    def encode(self, text: str) -> Sequence[int]:
        return [ord(character) for character in text]

    def decode(self, tokens: Sequence[int]) -> str:
        return "".join(chr(token) for token in tokens)


def character_tokenizers(_encoding: TokenEncoding) -> CharacterTokenizer:
    return CharacterTokenizer()


@dataclass
class ChatFakes:
    """The three doubles together, and the ports that plug them in with character tokenizers."""

    model: FakeModelClient = field(default_factory=FakeModelClient)
    search: FakeSearchGateway = field(default_factory=FakeSearchGateway)
    shields: FakePromptShields = field(default_factory=FakePromptShields)

    def ports(self) -> ChatPorts:
        return ChatPorts(
            openai=self.model.client(),
            search_clients=self.search,
            contentsafety=self.shields.client(),
            tokenizers=character_tokenizers,
        )
