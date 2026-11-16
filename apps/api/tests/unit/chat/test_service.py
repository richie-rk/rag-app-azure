"""The worker's chat service and the composition root's chat ports (`12-chat-pipeline.md`
sections 1.3, 1.4 and 10.6)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

from docchat_api.chat.service import (
    ChatPorts,
    EncodingCache,
    build_service,
    deployments,
    served_lookup,
)
from docchat_api.chat.tasks import TaskTracker, tracker
from docchat_api.conversations.store import ConversationStore
from docchat_api.tokens import TiktokenTokenizer
from docchat_api.usage import UsageService
from docchat_core.cosmos.conversations import ReasoningEffort
from docchat_core.ingestion.settings import EmbeddingModelCatalog, TokenEncoding
from docchat_core.sql.engine import SqlEngine
from docchat_testing.clock import FakeClock
from docchat_testing.fakes.chat import CharacterTokenizer, ChatFakes, character_tokenizers
from docchat_testing.fixtures.api import api_settings

API = api_settings(
    api={"openai_small_reasoning_effort": "low", "openai_chat_prompt_cache": "automatic"}
).api


def test_the_encoding_cache_keeps_one_tokenizer_per_encoding() -> None:
    cache = EncodingCache()

    first = cache(TokenEncoding.O200K_BASE)

    assert isinstance(first, TiktokenTokenizer)
    assert cache(TokenEncoding.O200K_BASE) is first
    assert cache(TokenEncoding.CL100K_BASE) is not first


def test_deployments_come_from_the_app_settings() -> None:
    chat, small = deployments(API, character_tokenizers)

    assert (chat.deployment, chat.model, chat.reasoning_effort, chat.prompt_cache) == (
        "chat",
        "gpt-5.6-sol",
        ReasoningEffort.LOW,
        "automatic",
    )
    assert (small.deployment, small.model, small.reasoning_effort, small.prompt_cache) == (
        "chat-small",
        "gpt-5.6-terra",
        ReasoningEffort.LOW,
        "disabled",
    )


async def test_the_served_lookup_runs_the_repository_statement() -> None:
    rows = [
        SimpleNamespace(
            document_id=uuid4(), chunk_set_id=uuid4(), document_version_id=uuid4(), source_name="a"
        )
    ]

    class Engine:
        async def fetch_all(self, *_: Any, **__: Any) -> list[Any]:
            return rows

    lookup = served_lookup(cast("SqlEngine", Engine()))

    assert set(await lookup(uuid4(), [rows[0].document_id])) == {rows[0].document_id}


def test_build_service_wires_the_ports_and_the_process_tracker() -> None:
    fakes = ChatFakes()
    ports = fakes.ports()
    assert isinstance(ports, ChatPorts)

    service = build_service(
        api=API,
        embeddings=EmbeddingModelCatalog.model_validate({}),
        openai=fakes.model.client(),
        search_clients=fakes.search,
        contentsafety=fakes.shields.client(),
        sql=cast("SqlEngine", None),
        conversations=cast("ConversationStore", None),
        usage=cast("UsageService", SimpleNamespace(record=lambda *a, **k: None)),
        clock=FakeClock(),
        tokenizers=character_tokenizers,
    )

    assert service.tracker is tracker()
    assert service.calls.small.deployment == "chat-small"
    assert service.answer.chat.deployment == "chat"
    assert isinstance(service.embedding_tokenizer("text-embedding-3-large"), CharacterTokenizer)
    assert isinstance(service.embedding_tokenizer("retired-model"), CharacterTokenizer)
    own = build_service(
        api=API,
        embeddings=EmbeddingModelCatalog.model_validate({}),
        openai=fakes.model.client(),
        search_clients=fakes.search,
        contentsafety=fakes.shields.client(),
        sql=cast("SqlEngine", None),
        conversations=cast("ConversationStore", None),
        usage=cast("UsageService", SimpleNamespace(record=lambda *a, **k: None)),
        clock=FakeClock(),
        tracker=TaskTracker(),
    )
    assert own.tracker is not tracker()
    assert isinstance(own.tokenizers(TokenEncoding.O200K_BASE), TiktokenTokenizer)
