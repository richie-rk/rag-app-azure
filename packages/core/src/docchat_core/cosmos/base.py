"""The base of every Cosmos DB document model (`80-data-model.md` section 3.2; ADR 0071 item 6).

A document model forbids unknown fields when it builds a write and ignores them when it reads a
stored item, so during a rolling deployment the release being replaced reads what the next one
writes. Python fields are snake_case; the stored names are their camelCase aliases. Every write
goes through `to_item`, which validates the whole document again and checks its size, so a
document changed with `model_copy(update=...)` still meets every limit before it is stored.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar, Self

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CosmosModel(BaseModel):
    """A document or a type nested in one: camelCase names, unknown fields forbidden."""

    model_config = ConfigDict(
        extra="forbid",
        alias_generator=to_camel,
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
    )


class DocumentTooLargeError(ValueError):
    """A document's UTF-8 JSON is above its type's limit (`80-data-model.md` section 3.12).

    A breach is a programming defect and is never truncated silently; the message holds only
    the document type and the two sizes.
    """

    def __init__(self, document_type: str, size: int, limit: int) -> None:
        super().__init__(f"a {document_type} document of {size} bytes is above its {limit}")
        self.document_type = document_type
        self.size = size
        self.limit = limit


class CosmosDocument(CosmosModel):
    """A stored item: its type's byte limit, the read path and the write path."""

    max_bytes: ClassVar[int]
    """The largest UTF-8 JSON the type may store."""

    @classmethod
    def from_item(cls, item: Mapping[str, Any]) -> Self:
        """Read a stored item. Unknown fields, the Cosmos DB system fields among them, are
        dropped at every level, so a document a newer release wrote still reads."""
        return cls.model_validate(item, extra="ignore")

    def json_size(self) -> int:
        """The bytes of the document's compact UTF-8 JSON."""
        return len(self.model_dump_json().encode("utf-8"))

    def to_item(self) -> dict[str, Any]:
        """The item to write: every field and limit validated again, unknown fields forbidden,
        and the size checked against `max_bytes`.

        Raises `pydantic.ValidationError` for a broken limit and `DocumentTooLargeError` for an
        oversized document.
        """
        # Values set without validation are passed through as they are, so the validation below
        # reports them; the serializer's own warning about them would say less.
        document = type(self).model_validate(self.model_dump(warnings=False))
        size = document.json_size()
        if size > self.max_bytes:
            raise DocumentTooLargeError(type(self).__name__, size, self.max_bytes)
        return document.model_dump(mode="json")
