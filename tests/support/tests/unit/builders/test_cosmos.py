from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from docchat_core.cosmos import DOCUMENT_MODELS, CosmosDocument
from docchat_core.cosmos.conversations.ids import turn_id, turn_trace_id
from docchat_testing.builders import cosmos as builders
from docchat_testing.builders.canaries import is_canary

BUILDERS: tuple[Callable[[], CosmosDocument], ...] = (
    builders.session_document,
    builders.turn_document,
    builders.turn_trace_document,
    builders.memory_document,
    builders.usage_day_counter,
    builders.usage_month_counter,
    builders.rate_window,
    builders.download_rate_window,
    builders.write_rate_window,
    builders.stream_lease,
    builders.download_lease,
)


def test_one_builder_per_document_type() -> None:
    assert {type(build()) for build in BUILDERS} == set(DOCUMENT_MODELS)


@pytest.mark.parametrize("build", BUILDERS)
def test_every_builder_writes_a_valid_item(build: Callable[[], CosmosDocument]) -> None:
    document = build()
    item = document.to_item()
    assert type(document).from_item(item) == document
    assert item["userId"] != build().to_item()["userId"]


def test_conversation_ids_derive_as_production_derives_them() -> None:
    user, session, client_turn = uuid4(), uuid4(), uuid4()
    turn = builders.turn_document(user_id=user, session_id=session, client_turn_id=client_turn)
    assert turn.id == turn_id(session, client_turn)
    assert is_canary(turn.question)
    assert is_canary(turn.answer)
    trace = builders.turn_trace_document(user_id=user, session_id=session, turn=turn.id)
    assert trace.id == turn_trace_id(turn.id)
    session_document = builders.session_document(user_id=user, session_id=session)
    assert session_document.id == session
    assert is_canary(session_document.title)


def test_memory_and_usage_documents() -> None:
    memory = builders.memory_document()
    assert memory.id == memory.user_id
    assert is_canary(memory.facts[0].text)
    lease = builders.stream_lease(slot=7, ttl=300)
    assert lease.id == "lease:7"
    assert lease.expires_at - lease.acquired_at == timedelta(seconds=300)
    assert builders.download_lease(slot=10).id == "dllease:10"
    day = builders.usage_day_counter()
    assert day.id == f"day:{day.period}"
    month = builders.usage_month_counter()
    assert month.id == f"month:{month.period}"


def test_overrides_pass_through_validation() -> None:
    assert builders.session_document(turn_count=3).turn_count == 3
    with pytest.raises(ValidationError):
        builders.session_document(turn_count=0)
    with pytest.raises(ValidationError):
        builders.turn_document(id=uuid4())


def test_times_keep_milliseconds() -> None:
    assert builders.cosmos_now().microsecond % 1000 == 0
    assert len(builders.new_trace_id()) == 32


def test_a_stored_citation_names_the_rows_it_is_given() -> None:
    document, version, chunk_set = uuid4(), uuid4(), uuid4()

    citation = builders.stored_citation(
        number=3, document_id=document, document_version_id=version, chunk_set_id=chunk_set
    )

    assert (citation.number, citation.source_id, citation.page_number) == (3, "S3", 1)
    assert (citation.document_id, citation.document_version_id) == (document, version)
    assert citation.chunk_key == f"{document}_{chunk_set}_0"
    assert is_canary(citation.title)
    turn = builders.turn_document(citations=[citation])
    assert turn.to_item()["citations"][0]["chunkSetId"] == str(chunk_set)
