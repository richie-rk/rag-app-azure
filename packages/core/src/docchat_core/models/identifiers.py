"""Identifier types shared by several `docchat_core` subpackages (ADR 0015).

Entity IDs are UUIDs whose text form outside SQL is canonical lower-case hyphenated. Inbound
identifiers (paths, queries, bodies) are accepted in either case and lower-cased, while every
other shape (braces, URN, 32 hex digits) is refused, so a malformed ID never reaches a lookup and a
`404` always means "well formed, and absent or unreachable" (`11-api.md` section 1.7).
"""

from __future__ import annotations

import re
from typing import Annotated, Final
from uuid import UUID

from pydantic import AfterValidator, BeforeValidator, StringConstraints, WithJsonSchema
from pydantic_core import PydanticCustomError

UUID_PATTERN: Final = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
LOWER_CASE_UUID_PATTERN: Final = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
CORRELATION_ID_PATTERN: Final = r"^[0-9a-f]{32}$"

_UUID_RE: Final = re.compile(UUID_PATTERN)
_LOWER_CASE_UUID_RE: Final = re.compile(LOWER_CASE_UUID_PATTERN)

# The error type is Pydantic's own `uuid_parsing`, which the API's error mapping turns into the
# reason `invalid_uuid` (`11-api.md` section 1.7) without a special case for this type.
_UUID_ERROR: Final = "uuid_parsing"


def parse_uuid(text: str) -> UUID:
    """Parse a canonical hyphenated UUID in either case; refuse every other spelling."""
    if not _UUID_RE.fullmatch(text):
        raise ValueError("not a canonical hyphenated UUID")
    return UUID(text)


def _canonical_uuid(value: object) -> object:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str) and _UUID_RE.fullmatch(value):
        return UUID(value)
    raise PydanticCustomError(_UUID_ERROR, "Input should be a canonical hyphenated UUID")


def _lower_case_uuid(value: object) -> object:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str) and _LOWER_CASE_UUID_RE.fullmatch(value):
        return UUID(value)
    raise PydanticCustomError(_UUID_ERROR, "Input should be a lower-case hyphenated UUID")


def _not_all_zero(value: str) -> str:
    # An all-zero trace ID is invalid in W3C Trace Context, so it can't name a trace.
    if value == "0" * 32:
        raise ValueError("an all-zero trace ID is invalid")
    return value


CanonicalUuid = Annotated[
    UUID,
    BeforeValidator(_canonical_uuid),
    WithJsonSchema({"type": "string", "format": "uuid", "pattern": UUID_PATTERN}),
]
"""A UUID given in canonical hyphenated form, either case; it serialises lower-case."""

LowerCaseUuid = Annotated[
    UUID,
    BeforeValidator(_lower_case_uuid),
    WithJsonSchema({"type": "string", "format": "uuid", "pattern": LOWER_CASE_UUID_PATTERN}),
]
"""A UUID that must already be canonical lower-case text, as identity settings are."""

CorrelationId = Annotated[
    str,
    StringConstraints(pattern=CORRELATION_ID_PATTERN),
    AfterValidator(_not_all_zero),
]
"""The 32-character lower-case hexadecimal W3C trace ID of a request or an operator run."""
