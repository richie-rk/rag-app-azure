from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

import pytest
from pydantic import BaseModel, ValidationError

from docchat_core.cosmos import DOCUMENT_MODELS, CosmosDocument, CosmosModel
from docchat_core.cosmos.base import DocumentTooLargeError
from docchat_core.cosmos.conversations import SessionDocument, TurnDocument, TurnTraceDocument
from docchat_core.cosmos.memories import MemoryDocument
from docchat_core.cosmos.usage import (
    DownloadLease,
    DownloadRateWindow,
    RateWindow,
    StreamLease,
    UsageDayCounter,
    UsageDocument,
    UsageMonthCounter,
    WriteRateWindow,
)
from docchat_testing.builders import cosmos as builders


class _Nested(CosmosModel):
    page_number: int


class _Document(CosmosDocument):
    max_bytes: ClassVar[int] = 120

    id: str
    nested_item: _Nested
    note_text: str = ""


def _document() -> _Document:
    return _Document(id="d1", nested_item=_Nested(page_number=3))


def test_names_are_camel_case_on_the_wire_and_snake_case_in_python() -> None:
    document = _document()

    assert document.to_item() == {"id": "d1", "nestedItem": {"pageNumber": 3}, "noteText": ""}
    assert _Document.model_validate({"id": "d1", "nestedItem": {"pageNumber": 3}}) == document


def test_building_a_write_forbids_unknown_fields_at_every_level() -> None:
    with pytest.raises(ValidationError):
        _Document.model_validate({"id": "d1", "nestedItem": {"pageNumber": 3}, "extra": 1})
    with pytest.raises(ValidationError):
        _Document.model_validate({"id": "d1", "nestedItem": {"pageNumber": 3, "extra": 1}})


def test_reading_a_stored_item_ignores_unknown_and_system_fields_at_every_level() -> None:
    stored = {
        "id": "d1",
        "nestedItem": {"pageNumber": 3, "fieldOfANewerRelease": True},
        "fieldOfANewerRelease": [1, 2],
        "_rid": "r",
        "_self": "s",
        "_etag": '"0000"',
        "_attachments": "a",
        "_ts": 1_761_816_600,
    }

    assert _Document.from_item(stored) == _document()


def test_to_item_validates_again_so_a_copy_cannot_skip_a_rule() -> None:
    changed = _document().model_copy(update={"nested_item": {"pageNumber": "not a number"}})

    with pytest.raises(ValidationError):
        changed.to_item()


def test_to_item_refuses_a_document_above_its_size_limit() -> None:
    fits = _document().model_copy(update={"note_text": "x" * 60})
    assert fits.json_size() <= _Document.max_bytes
    fits.to_item()

    too_large = _document().model_copy(update={"note_text": "é" * 60})
    with pytest.raises(DocumentTooLargeError) as caught:
        too_large.to_item()

    assert caught.value.document_type == "_Document"
    assert caught.value.size == too_large.json_size() > caught.value.limit == 120
    assert "é" not in str(caught.value)


def test_json_size_counts_utf8_bytes() -> None:
    ascii_size = _document().model_copy(update={"note_text": "a"}).json_size()

    assert _document().model_copy(update={"note_text": "😀"}).json_size() == ascii_size + 3


def _models_below(model: type[BaseModel], seen: set[type[BaseModel]]) -> None:
    if model in seen:
        return
    seen.add(model)
    for field in model.model_fields.values():
        stack: list[object] = [field.annotation]
        while stack:
            annotation = stack.pop()
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                _models_below(annotation, seen)
            stack.extend(getattr(annotation, "__args__", ()))


def test_every_document_model_and_every_nested_model_forbids_unknown_fields() -> None:
    models: set[type[BaseModel]] = set()
    for document in DOCUMENT_MODELS:
        _models_below(document, models)

    assert len(models) > len(DOCUMENT_MODELS)
    assert sorted(m.__name__ for m in models if m.model_config.get("extra") != "forbid") == []


def test_the_registered_document_models() -> None:
    assert [model.__name__ for model in DOCUMENT_MODELS] == [
        "SessionDocument",
        "TurnDocument",
        "TurnTraceDocument",
        "MemoryDocument",
        "UsageDayCounter",
        "UsageMonthCounter",
        "RateWindow",
        "StreamLease",
        "DownloadRateWindow",
        "DownloadLease",
        "WriteRateWindow",
    ]


# T-092: every registered document model, built with valid values as the stores build it.

_BUILT: dict[type[CosmosDocument], Callable[[], CosmosDocument]] = {
    SessionDocument: builders.session_document,
    TurnDocument: builders.turn_document,
    TurnTraceDocument: builders.turn_trace_document,
    MemoryDocument: lambda: builders.memory_document(facts=[builders.fact()]),
    UsageDayCounter: builders.usage_day_counter,
    UsageMonthCounter: builders.usage_month_counter,
    RateWindow: builders.rate_window,
    StreamLease: builders.stream_lease,
    DownloadRateWindow: builders.download_rate_window,
    DownloadLease: builders.download_lease,
    WriteRateWindow: builders.write_rate_window,
}
_VERSIONED = (SessionDocument, TurnDocument, TurnTraceDocument, MemoryDocument)


def test_t092_every_registered_document_model_is_covered() -> None:
    assert set(_BUILT) == set(DOCUMENT_MODELS)


@pytest.mark.parametrize("model", DOCUMENT_MODELS, ids=lambda model: model.__name__)
def test_t092_a_write_refuses_an_unknown_field_and_a_read_ignores_it(
    model: type[CosmosDocument],
) -> None:
    item = _BUILT[model]().to_item()
    with_unknown = {**item, "fieldOfANewerRelease": {"nested": True}}

    with pytest.raises(ValidationError) as refused:
        model.model_validate(with_unknown)

    assert {error["type"] for error in refused.value.errors()} == {"extra_forbidden"}
    assert model.from_item(with_unknown).to_item() == item


@pytest.mark.parametrize("model", DOCUMENT_MODELS, ids=lambda model: model.__name__)
def test_t092_conversation_documents_carry_schema_version_1_and_usage_documents_none(
    model: type[CosmosDocument],
) -> None:
    item = _BUILT[model]().to_item()

    if issubclass(model, _VERSIONED):
        assert item["schemaVersion"] == 1
    else:
        assert issubclass(model, UsageDocument)
        assert "schemaVersion" not in item
