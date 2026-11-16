from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from docchat_core.cosmos.memories import (
    Fact,
    FactSource,
    FactSourceKind,
    MemoryDocument,
)
from docchat_core.memory import FactCategory

_OID = UUID("0b8f3c1e-6a2d-4f7b-9c55-1d2e3f405162")
_SESSION_ID = UUID("b6061e08-59bc-576e-a78d-b2d375c588e7")
_TURN_ID = UUID("445c1c2a-be4c-5d17-a92d-c4efcb094740")
_NOW = datetime(2026, 10, 14, 9, 30, tzinfo=UTC)
_FACT_ID = UUID("1b4e28ba-2fa1-41d2-883f-0016d3cca427")


def _fact(**changes: Any) -> Fact:
    values: dict[str, Any] = {
        "id": _FACT_ID,
        "text": "Works on supplier contract renewals",
        "category": FactCategory.TOPIC,
        "tokens": 6,
        "source": FactSource(kind=FactSourceKind.USER),
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return Fact(**(values | changes))


def test_the_first_read_creates_an_empty_document() -> None:
    assert MemoryDocument.empty(_OID, _NOW).to_item() == {
        "id": str(_OID),
        "userId": str(_OID),
        "type": "memory",
        "schemaVersion": 1,
        "paused": False,
        "pausedAt": None,
        "facts": [],
        "userEditedAt": None,
        "clearedAt": None,
        "createdAt": "2026-10-14T09:30:00.000Z",
        "updatedAt": "2026-10-14T09:30:00.000Z",
    }


def test_the_id_is_the_user_id() -> None:
    with pytest.raises(ValidationError, match="userId"):
        MemoryDocument(id=_SESSION_ID, user_id=_OID, created_at=_NOW, updated_at=_NOW)


def test_a_stored_document_reads_back_equal_and_drops_the_etag() -> None:
    extracted = FactSource(kind=FactSourceKind.EXTRACTED, session_id=_SESSION_ID, turn_id=_TURN_ID)
    document = MemoryDocument.empty(_OID, _NOW).model_copy(
        update={"facts": [_fact(source=extracted)], "paused": True, "paused_at": _NOW}
    )

    stored = {**document.to_item(), "_etag": '"00000000-0000-0000-0000-000000000000"'}

    assert MemoryDocument.from_item(stored) == document
    assert stored["facts"][0]["source"] == {
        "kind": "extracted",
        "sessionId": str(_SESSION_ID),
        "turnId": str(_TURN_ID),
    }


def test_the_five_categories() -> None:
    assert [category.value for category in FactCategory] == [
        "role",
        "team",
        "expertise",
        "topic",
        "answer_style",
    ]


@pytest.mark.parametrize(
    "changes",
    [
        {"text": ""},
        {"text": "t" * 201},
        {"tokens": 0},
        {"category": "hobby"},
        {"id": UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")},
    ],
)
def test_fact_limits_are_validated(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _fact(**changes)


def test_a_fact_text_counts_code_points() -> None:
    assert len(_fact(text="😀" * 200).text) == 200


@pytest.mark.parametrize(
    ("kind", "session_id", "turn_id"),
    [
        (FactSourceKind.USER, _SESSION_ID, _TURN_ID),
        (FactSourceKind.USER, _SESSION_ID, None),
        (FactSourceKind.EXTRACTED, None, None),
        (FactSourceKind.EXTRACTED, _SESSION_ID, None),
    ],
)
def test_only_an_extracted_fact_names_its_turn(
    kind: FactSourceKind, session_id: UUID | None, turn_id: UUID | None
) -> None:
    with pytest.raises(ValidationError, match="names"):
        FactSource(kind=kind, session_id=session_id, turn_id=turn_id)


def test_at_most_200_facts_each_with_its_own_id() -> None:
    def document(facts: list[Fact]) -> MemoryDocument:
        return MemoryDocument(id=_OID, user_id=_OID, facts=facts, created_at=_NOW, updated_at=_NOW)

    document([_fact(id=uuid4()) for _ in range(200)])
    with pytest.raises(ValidationError):
        document([_fact(id=uuid4()) for _ in range(201)])
    with pytest.raises(ValidationError, match="each fact ID appears once"):
        document([_fact(), _fact()])


def test_t093_a_memory_at_its_field_maximums_fits_its_size_limit() -> None:
    source = FactSource(kind=FactSourceKind.EXTRACTED, session_id=_SESSION_ID, turn_id=_TURN_ID)
    facts = [
        _fact(id=uuid4(), text="t" * 200, category=FactCategory.ANSWER_STYLE, source=source)
        for _ in range(200)
    ]
    document = MemoryDocument(
        id=_OID,
        user_id=_OID,
        paused=True,
        paused_at=_NOW,
        facts=facts,
        user_edited_at=_NOW,
        cleared_at=_NOW,
        created_at=_NOW,
        updated_at=_NOW,
    )

    assert document.json_size() <= MemoryDocument.max_bytes == 200_000
    document.to_item()
