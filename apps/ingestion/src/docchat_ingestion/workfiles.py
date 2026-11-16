"""The work-file formats of `ingestion-work` and the rendition marker (`14-ingestion.md` sections
6.14 and 14.5; ADR 0031).

Document content moves between activities only through these files, so orchestration history
holds paths and counts, never text. This module is their one definition: the methods write the
extract, table-summary and chunks files, and the activities read the chunks file and write the
embedding batches and the marker. Every file other than `convert/converted.pdf` is UTF-8 JSON
or JSON Lines holding `formatVersion: 1`, written with `upload_blob(overwrite=True)` and read
whole; each model refuses unknown fields and a file is read by its camelCase keys only, so a file
of another format version or a damaged file fails to decode instead of feeding a later step. The
page-wise extract part is the validated `partition.json` of the parsing child
(`docchat_ingestion.isolation.results.ExtractPartV1`), uploaded byte for byte. CPU-bound
decoding and encoding run in worker threads at the callers (section 4.4 rule 5).

A work file that an earlier step of the item wrote and that is missing or doesn't decode is
`WorkFileError`: no item code names it, so it reaches the failure boundary, which records it and
returns the retryable `internal_error` (sections 4.5 and 12.9 item 3).

The camelCase keys come from the alias generator: several share their spelling with chunk index
fields, whose names appear only in `docchat_core.search.schema` (T-107).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from enum import StrEnum
from typing import Final, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    model_validator,
)
from pydantic.alias_generators import to_camel

from docchat_core.ingestion.names import HEADING_MAX_LENGTH, TITLE_MAX_LENGTH
from docchat_core.ingestion.settings import TokenEncoding
from docchat_core.models import LowerCaseUuid, Sha256Hex
from docchat_core.search.schema import MAX_HEADINGS
from docchat_ingestion.isolation.results import ExtractPartV1, parse_partition

FORMAT_VERSION: Final = 1
MAX_CHUNKS: Final = 100_000

__all__ = [
    "FORMAT_VERSION",
    "MAX_CHUNKS",
    "ChunkLine",
    "ChunksFile",
    "ChunksHeader",
    "EmbeddingBatch",
    "ExtractDocument",
    "ExtractPartV1",
    "PageOffset",
    "RenditionMarker",
    "SummaryOutcome",
    "TableSummaryLine",
    "WorkFileError",
    "decode_chunks",
    "decode_extract_document",
    "decode_marker",
    "decode_table_summaries",
    "encode_chunks",
    "encode_table_summaries",
    "json_size",
    "parse_partition",
]


class WorkFileError(Exception):
    """A work file an earlier step of this item wrote is missing or isn't valid; the failure
    boundary records it as `internal_error`. The message is constant."""

    def __init__(self) -> None:
        super().__init__("work file missing or invalid")


_FROM_FILE: Final = "work_file"
"""The validation-context key `_decode` sets: the model is read from a stored file."""


class _WorkFile(BaseModel):
    """Built by field name in code; a file is read by its camelCase keys only (`_decode`)."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        alias_generator=to_camel,
        validate_by_alias=True,
        validate_by_name=True,
        serialize_by_alias=True,
    )

    @model_validator(mode="before")
    @classmethod
    def _file_names_its_format_version(cls, data: object, info: ValidationInfo) -> object:
        # Code builds a model without `format_version` and gets the default, but a stored file
        # that doesn't name its version is of another format (T-098).
        context = info.context
        if (
            isinstance(context, dict)
            and context.get(_FROM_FILE)
            and "format_version" in cls.model_fields
            and isinstance(data, dict)
            and "formatVersion" not in data
        ):
            raise ValueError("a work file names its formatVersion")
        return data

    def encode(self) -> bytes:
        """Compact UTF-8 JSON, non-ASCII text kept as it is."""
        return self.model_dump_json(by_alias=True).encode("utf-8")


def _decode[M: BaseModel](model: type[M], data: bytes | str) -> M:
    return model.model_validate_json(data, by_alias=True, by_name=False, context={_FROM_FILE: True})


# --- Extract document (recursive) -----------------------------------------------------------------


class PageOffset(_WorkFile):
    """One span of `pages[].spans`: offsets count Unicode code points (section 11.1 item 3)."""

    page_number: int = Field(ge=1)
    offset: int = Field(ge=0)
    length: int = Field(ge=0)


class ExtractDocument(_WorkFile):
    """`extract/document.json` (section 6.14 item 2)."""

    format_version: Literal[1] = FORMAT_VERSION
    method: Literal["recursive"] = "recursive"
    page_count: int = Field(ge=1)
    markdown: str
    page_offsets: list[PageOffset]
    doc_intel_pages: int = Field(ge=0)


def decode_extract_document(data: bytes, page_count: int) -> ExtractDocument:
    """The recursive extract file of a version of `page_count` pages; `WorkFileError` for one
    that doesn't decode or belongs to another pagination."""
    try:
        document = _decode(ExtractDocument, data)
    except (ValidationError, UnicodeDecodeError):
        raise WorkFileError from None
    if document.page_count != page_count:
        raise WorkFileError
    return document


# --- Table summaries ------------------------------------------------------------------------------


class SummaryOutcome(StrEnum):
    """A finished table's outcome (section 12.7)."""

    SUMMARISED = "summarised"
    FILTERED = "filtered"
    DISCARDED = "discarded"
    SKIPPED = "skipped"


class TableSummaryLine(_WorkFile):
    """One line of `enrich/table-summaries.jsonl` (section 6.14 item 3): a finished table, keyed
    by the SHA-256 of its sanitised HTML, so identical tables share one summary."""

    table_sha256: Sha256Hex
    page_number: int = Field(ge=1)
    outcome: SummaryOutcome
    summary: str | None
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def _summary_only_when_summarised(self) -> Self:
        if (self.summary is not None) != (self.outcome is SummaryOutcome.SUMMARISED):
            raise ValueError("only a summarised table carries a summary")
        return self


def encode_table_summaries(lines: Sequence[TableSummaryLine]) -> bytes:
    return b"".join(line.encode() + b"\n" for line in lines)


def decode_table_summaries(data: bytes | None) -> list[TableSummaryLine]:
    """Every line of the file; an absent or empty file has none. `WorkFileError` for a line
    that isn't a finished table."""
    if data is None:
        return []
    try:
        text = data.decode("utf-8")
        return [_decode(TableSummaryLine, line) for line in text.splitlines() if line.strip()]
    except (ValidationError, UnicodeDecodeError):
        raise WorkFileError from None


# --- Chunks ---------------------------------------------------------------------------------------


class ChunksHeader(_WorkFile):
    """The first line of `chunks/{chunkSetId}.jsonl` (section 6.14 item 4)."""

    format_version: Literal[1] = FORMAT_VERSION
    chunk_set_id: LowerCaseUuid
    document_id: LowerCaseUuid
    document_version_id: LowerCaseUuid
    chunk_count: int = Field(ge=0, le=MAX_CHUNKS)
    encoding: TokenEncoding
    title: str = Field(max_length=TITLE_MAX_LENGTH)


class ChunkLine(_WorkFile):
    """One chunk: its 0-based ordinal, the text (exactly the embedding input), the heading path,
    the page of the version's PDF where it starts and its token count."""

    ordinal: int = Field(ge=0, lt=MAX_CHUNKS)
    chunk_text: str = Field(min_length=1)
    heading_path: list[str] = Field(max_length=MAX_HEADINGS)
    page_number: int = Field(ge=1)
    token_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _headings(self) -> Self:
        if any(not 1 <= len(heading) <= HEADING_MAX_LENGTH for heading in self.heading_path):
            raise ValueError("a heading holds 1 to 200 characters")
        return self


class ChunksFile(BaseModel):
    """A decoded chunks file: the header and its chunks in ordinal order."""

    model_config = ConfigDict(frozen=True)

    header: ChunksHeader
    chunks: tuple[ChunkLine, ...]

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if len(self.chunks) != self.header.chunk_count:
            raise ValueError("the header counts every chunk")
        if any(chunk.ordinal != index for index, chunk in enumerate(self.chunks)):
            raise ValueError("ordinals run 0, 1, 2 and on")
        return self


def encode_chunks(header: ChunksHeader, chunks: Sequence[ChunkLine]) -> bytes:
    return b"".join(model.encode() + b"\n" for model in (header, *chunks))


def decode_chunks(data: bytes) -> ChunksFile:
    """A chunks file decoded and checked: the header's count equals the lines, ordinals run from
    0 without a gap. Raises `pydantic.ValidationError` or `ValueError`."""
    lines = [line for line in data.decode("utf-8").splitlines() if line]
    if not lines:
        raise ValueError("a chunks file starts with its header")
    header = _decode(ChunksHeader, lines[0])
    chunks = tuple(_decode(ChunkLine, line) for line in lines[1:])
    return ChunksFile(header=header, chunks=chunks)


# --- Embeddings -----------------------------------------------------------------------------------


class EmbeddingBatch(_WorkFile):
    """`embeddings/{chunkSetId}/batch-{n:05d}.json` (section 6.14 item 5): the vectors of one
    embedding batch, in ordinal order from `firstOrdinal`."""

    format_version: Literal[1] = FORMAT_VERSION
    first_ordinal: int = Field(ge=0)
    vectors: list[list[float]]
    prompt_tokens: int = Field(ge=0)

    def matches(self, first_ordinal: int, count: int, dimensions: int) -> bool:
        """Whether the file holds this batch: its ordinal range and dimension count, so a retry
        reuses it (section 6.6 step 2)."""
        return (
            self.first_ordinal == first_ordinal
            and len(self.vectors) == count
            and all(len(vector) == dimensions for vector in self.vectors)
        )


def decode_embedding_batch(data: bytes) -> EmbeddingBatch | None:
    """A stored batch file; `None` for one that doesn't decode, which Embed writes again."""
    try:
        return _decode(EmbeddingBatch, data)
    except ValidationError:
        return None


# --- The rendition marker -------------------------------------------------------------------------


class RenditionMarker(_WorkFile):
    """`renditions/{indexId}/{documentId}/{v}/complete.json` (section 14.5): its presence means
    the rendition set of that PDF is complete under that rules version."""

    page_count: int = Field(ge=1)
    pdf_sha256: Sha256Hex
    rendition_rules_version: int = Field(ge=1)
    document_pdf: Literal["built", "too_large"]

    def current(self, pdf_sha256: str | None, rules_version: int) -> bool:
        """The marker Render skips on: the chunk set's PDF under the deployed rules (a marker of
        another rules version counts as absent)."""
        return self.pdf_sha256 == pdf_sha256 and self.rendition_rules_version == rules_version


def decode_marker(data: bytes) -> RenditionMarker | None:
    """A stored marker; one without `renditionRulesVersion`, or otherwise unreadable, counts as
    absent (section 6.8 step 1)."""
    try:
        return _decode(RenditionMarker, data)
    except ValueError:
        return None


def json_size(value: object) -> int:
    """The bytes of a value's compact UTF-8 JSON serialisation."""
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
