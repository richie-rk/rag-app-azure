"""The OData filter builder, the only code that produces search filter text (`80-data-model.md`
section 6.8; ADR 0044 item 8).

Field names come from `FilterField`, whose values are read from `ChunkIndexFields`, never from
input. Values are typed (a `UUID`, an `int` or a `ContentType` member) and every string literal is
quoted with each embedded single quote doubled, UUIDs included although their text holds none, so
a later string-typed field stays safe. No untrusted value is ever concatenated into filter text.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, override
from uuid import UUID

from docchat_core.ingestion.models import ContentType
from docchat_core.search.schema import CHUNK_INDEX_SCHEMA_VERSION, ChunkIndexFields

_FIELDS: Final = ChunkIndexFields.for_version(CHUNK_INDEX_SCHEMA_VERSION)

ANY_OF_MAX_VALUES: Final = 100


class FilterField(StrEnum):
    """The filterable fields of the schema in use (section 6.3)."""

    DOCUMENT_ID = _FIELDS.document_id
    DOCUMENT_VERSION_ID = _FIELDS.document_version_id
    CHUNK_SET_ID = _FIELDS.chunk_set_id
    PAGE_NUMBER = _FIELDS.page_number
    CONTENT_TYPE = _FIELDS.content_type


_UUID_FIELDS: Final = frozenset(
    {FilterField.DOCUMENT_ID, FilterField.DOCUMENT_VERSION_ID, FilterField.CHUNK_SET_ID}
)


@dataclass(frozen=True, slots=True)
class ODataFilter:
    """A filter expression built by `eq`, `any_of` or `all_of`; pass `text` (or `str()` of it)
    as the search call's `filter=` argument."""

    text: str

    @override
    def __str__(self) -> str:
        return self.text


def quote(value: str) -> str:
    """An OData string literal: single quotes, each embedded single quote doubled."""
    return "'" + value.replace("'", "''") + "'"


def eq(field: FilterField, value: UUID | int | ContentType) -> ODataFilter:
    """`<field> eq <value>`: a UUID field takes a `UUID`, `pageNumber` an `int` and
    `contentType` a `ContentType` member."""
    field = FilterField(field)
    if field in _UUID_FIELDS:
        if not isinstance(value, UUID):
            raise TypeError("an ID field compares with a UUID")
        literal = quote(str(value))
    elif field is FilterField.PAGE_NUMBER:
        if type(value) is not int:
            raise TypeError("pageNumber compares with an integer")
        literal = str(value)
    else:
        if not isinstance(value, ContentType):
            raise TypeError("contentType compares with a ContentType member")
        literal = quote(value.value)
    return ODataFilter(f"{field.value} eq {literal}")


def any_of(field: FilterField, values: Sequence[UUID]) -> ODataFilter:
    """`search.in(<field>, '<v1>,<v2>', ',')` for 1 to 100 UUIDs of an ID field."""
    field = FilterField(field)
    if field not in _UUID_FIELDS:
        raise TypeError("any_of takes an ID field")
    if not 1 <= len(values) <= ANY_OF_MAX_VALUES:
        raise ValueError(f"any_of takes 1 to {ANY_OF_MAX_VALUES} values")
    if not all(isinstance(value, UUID) for value in values):
        raise TypeError("any_of takes UUIDs")
    joined = ",".join(str(value) for value in values)
    return ODataFilter(f"search.in({field.value}, {quote(joined)}, ',')")


def all_of(*filters: ODataFilter) -> ODataFilter:
    """Every operand, joined with ` and `."""
    if not filters:
        raise ValueError("all_of takes at least one filter")
    if not all(isinstance(item, ODataFilter) for item in filters):
        raise TypeError("all_of joins filters built by this module")
    return ODataFilter(" and ".join(item.text for item in filters))
