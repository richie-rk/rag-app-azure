"""Keyset pagination cursors (`11-api.md` section 1.8; `82-security.md` SEC-29).

A cursor is the base64url encoding, without `=` padding, of compact UTF-8 JSON `{"v": 1, ...}`
holding the sort values of the last item returned and the filter values it was issued for. Each
list operation defines its cursor model (a `CursorModel` with its own members), encodes with
`encode_cursor` and decodes with `decode_cursor`, passing the request's filters after its own
normalisation. Any failure (length or alphabet, base64, JSON, the model with `extra="forbid"` and
`v` 1, a filter mismatch) is `422 validation_failed` with the reason `invalid_cursor` at the
cursor's parameter. Cursors aren't signed: they hold only positions inside the caller's own
authorised view, so tampering only skips or repeats items the caller may see anyway.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, ValidationError, WithJsonSchema
from pydantic_core import PydanticCustomError

from docchat_api.http.problems import ValidationFailed
from docchat_core.errors import FieldIssue

CURSOR_MAX_LENGTH: Final = 512
CURSOR_PATTERN: Final = r"^[A-Za-z0-9_-]+$"
CURSOR_PARAMETER: Final = "query.cursor"
_CURSOR_RE: Final = re.compile(CURSOR_PATTERN)


class CursorModel(BaseModel):
    """The base of every cursor's JSON: unknown members refused, `v` exactly 1."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    v: Literal[1] = 1


def _invalid_cursor() -> PydanticCustomError:
    return PydanticCustomError("invalid_cursor", "The cursor isn't valid")


def _cursor_text(value: object) -> object:
    if not isinstance(value, str) or not 1 <= len(value) <= CURSOR_MAX_LENGTH:
        raise _invalid_cursor()
    if not _CURSOR_RE.fullmatch(value):
        raise _invalid_cursor()
    return value


CursorText = Annotated[
    str,
    BeforeValidator(_cursor_text),
    WithJsonSchema(
        {
            "type": "string",
            "minLength": 1,
            "maxLength": CURSOR_MAX_LENGTH,
            "pattern": CURSOR_PATTERN,
        }
    ),
]
"""The `cursor` parameter and every `nextCursor`: 1 to 512 base64url characters."""


def encode_cursor(cursor: CursorModel) -> str:
    """`json.dumps(..., separators=(",", ":"), ensure_ascii=False)` as UTF-8, then URL-safe
    base64 with the padding removed."""
    text = json.dumps(cursor.model_dump(mode="json"), separators=(",", ":"), ensure_ascii=False)
    encoded = base64.urlsafe_b64encode(text.encode("utf-8")).rstrip(b"=").decode("ascii")
    if len(encoded) > CURSOR_MAX_LENGTH:
        raise ValueError("a cursor encodes to at most 512 characters")
    return encoded


def decode_cursor[C: CursorModel](
    text: str,
    model: type[C],
    *,
    filters: Mapping[str, Any] | None = None,
    parameter: str = CURSOR_PARAMETER,
) -> C:
    """The cursor `text` read as `model`, its filter members equal to `filters`; otherwise
    `ValidationFailed` with `invalid_cursor` at `parameter`."""
    invalid = ValidationFailed(errors=[FieldIssue(parameter, "invalid_cursor")])
    if not 1 <= len(text) <= CURSOR_MAX_LENGTH or not _CURSOR_RE.fullmatch(text):
        raise invalid
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        # JSON mode, so a strict cursor model still reads UUIDs and times from their text.
        cursor = model.model_validate_json(raw)
    except (binascii.Error, ValueError, ValidationError):
        raise invalid from None
    for name, expected in (filters or {}).items():
        if getattr(cursor, name) != expected:
            raise invalid
    return cursor
