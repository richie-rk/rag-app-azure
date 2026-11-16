"""The worker's chat service: the clients, deployments, encodings and stores a turn uses
(`12-chat-pipeline.md` sections 1.3, 1.4 and 10.6; `84-testing.md` section 1.1 principle 3).

The lifespan builds one `ChatService` per worker from the client registry (the one `AsyncOpenAI`,
the per-index search clients and the Prompt Shields client) unless the caller passes
`ChatPorts`, the composition root's seam for the services without an emulator: a test gives a
search client factory over `ReplayTransport`, a Prompt Shields client over a mock transport, or a
model client double, and the pipeline runs unchanged over them.

`PostTurnWork` is the conversation store's after-response entry points (`ensure_post_turn_work`
at the start of T2, `schedule_post_turn_work` in the finaliser of a `completed` turn,
`13-conversations-and-memory.md` sections 6.4 and 6.5). The conversations-and-memory stage
supplies them; until it does the service holds none and a turn schedules no after-response work.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

import httpx
from openai import AsyncOpenAI

from docchat_api.chat.calls import ChatDeploymentSettings, ModelCalls, UsageRecorder
from docchat_api.chat.generation import AnswerCall
from docchat_api.chat.prompts import TextTokenizer
from docchat_api.chat.retrieval import SearchClients, ServedLookup
from docchat_api.chat.screening import PromptShields
from docchat_api.chat.tasks import TaskTracker
from docchat_api.chat.tasks import tracker as process_tracker
from docchat_api.tokens import TiktokenTokenizer
from docchat_core.cosmos.conversations import ReasoningEffort, SessionDocument
from docchat_core.ingestion.settings import EmbeddingModelCatalog, TokenEncoding
from docchat_core.models.clock import Clock
from docchat_core.sql.engine import SqlEngine
from docchat_core.sql.repositories.retrieval import ServedChunkSet, read_served_chunk_sets

if TYPE_CHECKING:
    from docchat_api.conversations.store import ConversationStore
    from docchat_api.settings import ApiSettings
    from docchat_api.usage import UsageService


class PostTurnWork(Protocol):
    """The conversation store's after-response work."""

    def ensure_post_turn_work(self, session: SessionDocument) -> None: ...

    def schedule_post_turn_work(self, oid: UUID, session_id: UUID, turn_id: UUID) -> None: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatPorts:
    """What a caller of `create_app` may pass instead of the registry's clients; `None` keeps
    the registry's."""

    openai: AsyncOpenAI | None = None
    search_clients: SearchClients | None = None
    contentsafety: httpx.AsyncClient | None = None
    post_turn: PostTurnWork | None = None
    tokenizers: Tokenizers | None = None
    """The encodings by name; the image's `tiktoken` cache otherwise."""


type Tokenizers = Callable[[TokenEncoding], TextTokenizer]


class EncodingCache:
    """One tokenizer per encoding, shared by every turn of the worker."""

    def __init__(self) -> None:
        self._tokenizers: dict[TokenEncoding, TextTokenizer] = {}

    def __call__(self, encoding: TokenEncoding) -> TextTokenizer:
        tokenizer = self._tokenizers.get(encoding)
        if tokenizer is None:
            tokenizer = self._tokenizers[encoding] = TiktokenTokenizer(encoding)
        return tokenizer


def deployments(
    api: ApiSettings, tokenizers: Tokenizers
) -> tuple[ChatDeploymentSettings, ChatDeploymentSettings]:
    """The chat and chat-small deployments as the app settings name them."""
    chat = ChatDeploymentSettings(
        deployment=api.openai_chat_deployment,
        model=api.openai_chat_model,
        model_version=api.openai_chat_model_version,
        reasoning_effort=ReasoningEffort(api.openai_chat_reasoning_effort),
        prompt_cache=api.openai_chat_prompt_cache,
        tokenizer=tokenizers(api.openai_chat_encoding),
    )
    small = ChatDeploymentSettings(
        deployment=api.openai_small_deployment,
        model=api.openai_small_model,
        model_version=api.openai_small_model_version,
        reasoning_effort=ReasoningEffort(api.openai_small_reasoning_effort),
        prompt_cache=api.openai_small_prompt_cache,
        tokenizer=tokenizers(api.openai_small_encoding),
    )
    return chat, small


def served_lookup(engine: SqlEngine) -> ServedLookup:
    """The served-chunk-set statement over the worker's data-access layer."""

    async def lookup(
        physical_index_id: UUID, document_ids: Sequence[UUID]
    ) -> Mapping[UUID, ServedChunkSet]:
        return await read_served_chunk_sets(
            engine, physical_index_id=physical_index_id, document_ids=document_ids
        )

    return lookup


@dataclass(slots=True, kw_only=True)
class ChatService:
    """Everything a turn needs beyond its request."""

    calls: ModelCalls
    answer: AnswerCall
    search_clients: SearchClients
    served: ServedLookup
    shields: PromptShields
    conversations: ConversationStore
    usage: UsageService
    embeddings: EmbeddingModelCatalog
    tokenizers: Tokenizers
    clock: Clock
    tracker: TaskTracker
    post_turn: PostTurnWork | None = None

    @property
    def recorder(self) -> UsageRecorder:
        return self.usage.record

    def embedding_tokenizer(self, model: str) -> TextTokenizer:
        """The encoding of an embedding model, from the catalogue; `cl100k_base` for a model the
        catalogue no longer lists, the encoding of every model it has held."""
        entry = self.embeddings.find(model)
        return self.tokenizers(entry.encoding if entry is not None else TokenEncoding.CL100K_BASE)

    async def aclose(self) -> None:
        """Wait for the turns' finalisers still running."""
        await self.tracker.wait_held()


def build_service(
    *,
    api: ApiSettings,
    embeddings: EmbeddingModelCatalog,
    openai: AsyncOpenAI,
    search_clients: SearchClients,
    contentsafety: httpx.AsyncClient,
    sql: SqlEngine,
    conversations: ConversationStore,
    usage: UsageService,
    clock: Clock,
    post_turn: PostTurnWork | None = None,
    tracker: TaskTracker | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    tokenizers: Tokenizers | None = None,
) -> ChatService:
    """The worker's service over its clients and stores."""
    tokenizers = EncodingCache() if tokenizers is None else tokenizers
    chat, small = deployments(api, tokenizers)
    sleeping = {} if sleep is None else {"sleep": sleep}
    return ChatService(
        calls=ModelCalls(openai, small=small, clock=clock, **sleeping),
        answer=AnswerCall(openai, chat=chat, clock=clock, **sleeping),
        search_clients=search_clients,
        served=served_lookup(sql),
        shields=PromptShields(
            contentsafety, timeout_seconds=api.contentsafety_timeout_seconds, **sleeping
        ),
        conversations=conversations,
        usage=usage,
        embeddings=embeddings,
        tokenizers=tokenizers,
        clock=clock,
        tracker=process_tracker() if tracker is None else tracker,
        post_turn=post_turn,
    )
