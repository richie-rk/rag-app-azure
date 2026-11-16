from __future__ import annotations

import base64
import json
from uuid import UUID

import pytest
from pydantic import BaseModel, ValidationError

from docchat_api.http.paging import (
    CURSOR_MAX_LENGTH,
    CursorModel,
    CursorText,
    decode_cursor,
    encode_cursor,
)
from docchat_api.http.problems import ValidationFailed
from docchat_core.errors import FieldIssue


class _NameCursor(CursorModel):
    n: str


class _FilteredCursor(CursorModel):
    n: str
    i: UUID
    s: str | None
    q: str | None


def _raw(payload: object) -> str:
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    return base64.urlsafe_b64encode(text).rstrip(b"=").decode()


def _invalid(text: str, model: type[CursorModel] = _NameCursor, **filters: object) -> FieldIssue:
    with pytest.raises(ValidationFailed) as caught:
        decode_cursor(text, model, filters=filters or None)
    (issue,) = caught.value.errors
    return issue


def test_t053_a_cursor_round_trips() -> None:
    cursor = _FilteredCursor(n="Ünïcode name", i=UUID(int=7), s="active", q=None)

    text = encode_cursor(cursor)

    assert "=" not in text
    assert decode_cursor(text, _FilteredCursor, filters={"s": "active", "q": None}) == cursor


def test_the_encoding_is_compact_utf8_json() -> None:
    text = encode_cursor(_NameCursor(n="é"))

    assert text == _raw({"v": 1, "n": "é"})


@pytest.mark.parametrize(
    "text",
    [
        "!!!!",
        "a" * (CURSOR_MAX_LENGTH + 1),
        _raw({"v": 2, "n": "x"}),
        _raw({"v": 1, "n": "x", "extra": 1}),
        _raw({"v": 1}),
        _raw(["v", 1]),
        base64.urlsafe_b64encode(b"\xff\xfe").rstrip(b"=").decode(),
        "abc",
    ],
    ids=["alphabet", "513", "version", "extra", "missing", "array", "utf8", "base64"],
)
def test_t053_a_broken_cursor_is_invalid_cursor(text: str) -> None:
    assert _invalid(text) == FieldIssue("query.cursor", "invalid_cursor")


def test_t053_a_filter_mismatch_is_invalid_cursor() -> None:
    text = encode_cursor(_FilteredCursor(n="a", i=UUID(int=1), s="active", q="x"))

    assert _invalid(text, _FilteredCursor, s="archived", q="x").reason == "invalid_cursor"


def test_the_parameter_can_be_named() -> None:
    with pytest.raises(ValidationFailed) as caught:
        decode_cursor("!", _NameCursor, parameter="query.itemsCursor")

    assert caught.value.errors[0].path == "query.itemsCursor"


def test_an_oversized_cursor_is_never_issued() -> None:
    with pytest.raises(ValueError, match="512"):
        encode_cursor(_NameCursor(n="x" * 400))


class _Query(BaseModel):
    cursor: CursorText | None = None


@pytest.mark.parametrize("text", ["", "a" * 513, "a=b", "a b"])
def test_the_cursor_parameter_refuses_its_shape_with_invalid_cursor(text: str) -> None:
    with pytest.raises(ValidationError) as caught:
        _Query.model_validate({"cursor": text})

    assert caught.value.errors()[0]["type"] == "invalid_cursor"


def test_the_cursor_parameter_schema() -> None:
    schema = _Query.model_json_schema()["properties"]["cursor"]["anyOf"][0]

    assert schema == {
        "type": "string",
        "minLength": 1,
        "maxLength": 512,
        "pattern": "^[A-Za-z0-9_-]+$",
    }


def test_a_tampered_cursor_only_moves_the_position() -> None:
    # A cursor holds positions, never access: a forged one decodes to another position only.
    forged = _raw({"v": 1, "n": "zzz"})

    assert decode_cursor(forged, _NameCursor).n == "zzz"
