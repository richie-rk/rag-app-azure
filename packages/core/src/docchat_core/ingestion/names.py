"""Document names, displayed text, blob paths and physical index names (`14-ingestion.md` sections
2.2, 6.14, 8.2 and 9.1 item 5; `80-data-model.md` sections 4.3 and 6.2; `01-repository.md`
section 6.4 rule 1; ADRs 0031, 0040 and 0045).

Names never become paths: every blob path and every Azure index name is built here from generated
UUIDs and integers only, and is checked against its exact shape before it is returned, so a
builder bug fails loudly instead of misplacing a file. A path that comes back from data (a stored
`blob_path`, a plan's work prefix) is re-validated here before any call uses it. Admin-supplied
text (a file name, a title) reaches only display fields, cleaned by `display_text` or refused by
the name rules.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal
from uuid import UUID

from docchat_core.ingestion.models import CONTENT_TYPE_BY_EXTENSION, ContentType

# --- Document names -------------------------------------------------------------------------------

NAME_MAX_LENGTH: Final = 255
"""Code points of a name after NFC and trimming (`documents.source_name` is `NVARCHAR(255)`)."""

TITLE_MAX_LENGTH: Final = 300
HEADING_MAX_LENGTH: Final = 200

REFUSED_CATEGORIES: Final = frozenset({"Cc", "Cf", "Co", "Cs", "Cn"})
"""Control, format, private-use, surrogate and unassigned code points: none may reorder a name's
text, hide characters or fake its extension, or reach a citation title or heading."""

_WHITESPACE_RE: Final = re.compile(r"\s+")

type NameCode = Literal["invalid_file_name", "unsupported_type"]
type NameReason = Literal["invalid_characters", "too_short", "too_long", "invalid_format"]


class InvalidDocumentNameError(ValueError):
    """A name the rules of section 2.2 refuse: `code` is the problem code (`invalid_file_name`
    or `unsupported_type`) and `reason` the `errors[]` reason at `body.name`. The message never
    holds the name."""

    def __init__(self, code: NameCode, reason: NameReason) -> None:
        super().__init__(f"{code}: {reason}")
        self.code: NameCode = code
        self.reason: NameReason = reason


@dataclass(frozen=True, slots=True)
class DocumentName:
    """A name that passed every rule: the display name, the identity within the index and the
    media type its extension gives."""

    source_name: str
    normalized_name: str
    content_type: ContentType


def _display_name(name: str) -> str:
    return unicodedata.normalize("NFC", name).strip()


def _check_name_text(source_name: str) -> None:
    if not source_name:
        raise InvalidDocumentNameError("invalid_file_name", "too_short")
    if len(source_name) > NAME_MAX_LENGTH:
        raise InvalidDocumentNameError("invalid_file_name", "too_long")
    for character in source_name:
        if character in "/\\" or unicodedata.category(character) in REFUSED_CATEGORIES:
            raise InvalidDocumentNameError("invalid_file_name", "invalid_characters")


def normalize_document_name(name: str) -> str:
    """The name that identifies a document within its logical index (`UQ_documents_index_name`):
    Unicode NFC, leading and trailing whitespace stripped, every whitespace run replaced by one
    space, then `str.casefold()`. A name that breaks the character or length rules is refused
    with `invalid_file_name`; the extension is checked by `document_name`."""
    source_name = _display_name(name)
    _check_name_text(source_name)
    return _WHITESPACE_RE.sub(" ", source_name).casefold()


def document_name(name: str) -> DocumentName:
    """Apply every rule of section 2.2 in its order: the characters and length
    (`invalid_file_name`), then the extension (`unsupported_type`)."""
    normalized = normalize_document_name(name)
    source_name = _display_name(name)
    content_type = content_type_of(source_name)
    if content_type is None:
        raise InvalidDocumentNameError("unsupported_type", "invalid_format")
    return DocumentName(source_name, normalized, content_type)


def content_type_of(name: str) -> ContentType | None:
    """The media type a name's extension gives, compared case-insensitively; `None` for any
    other extension."""
    lowered = name.casefold()
    for extension, content_type in CONTENT_TYPE_BY_EXTENSION.items():
        if lowered.endswith(extension):
            return content_type
    return None


def document_title(source_name: str) -> str:
    """A chunk's `title`: the version's `source_name` without its extension, cut to 300
    characters. The name rules already keep it free of the refused categories."""
    stem = source_name
    if content_type_of(source_name) is not None:
        stem = source_name.rsplit(".", 1)[0]
    return stem[:TITLE_MAX_LENGTH]


def display_text(text: str, limit: int = HEADING_MAX_LENGTH) -> str:
    """Text a reader sees beside an answer (each `headingPath` entry): Unicode NFC, every
    code point of a refused category removed, each whitespace run collapsed to one space, the
    ends trimmed, the result cut to `limit` code points. Pure and never failing: a heading full
    of format characters gives an empty string, which the caller leaves out of the path.

    Control characters that are whitespace (tab, line breaks, U+0085) count as whitespace, so
    a heading the parser wrapped over two lines keeps its words apart instead of joining them.
    """
    if limit < 1:
        raise ValueError("a display limit is at least one code point")
    normalized = unicodedata.normalize("NFC", text)
    kept = "".join(
        " "
        if character.isspace()
        else ""
        if unicodedata.category(character) in REFUSED_CATEGORIES
        else character
        for character in normalized
    )
    return _WHITESPACE_RE.sub(" ", kept).strip()[:limit]


# --- Containers and queues ------------------------------------------------------------------------


class BlobContainer(StrEnum):
    """The three containers of the data account (`80-data-model.md` section 4.2)."""

    DOCUMENTS = "documents"
    RENDITIONS = "renditions"
    INGESTION_WORK = "ingestion-work"


INGESTION_QUEUE: Final = "ingestion-requests"
POISON_QUEUE: Final = "ingestion-requests-poison"

# --- Blob paths -----------------------------------------------------------------------------------

PADDED_NUMBER_MAX: Final = 99_999
"""Part and batch numbers are zero-padded to 5 digits, so a larger one would break the width."""

_UUID: Final = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_NUMBER: Final = r"[1-9][0-9]*"
_PADDED: Final = r"[0-9]{5}"
_VERSION_PREFIX: Final = rf"{_UUID}/{_UUID}/{_NUMBER}/"

PATH_PATTERNS: Final[dict[BlobContainer, tuple[re.Pattern[str], ...]]] = {
    BlobContainer.DOCUMENTS: (
        re.compile(rf"^{_VERSION_PREFIX}source$"),
        re.compile(rf"^{_VERSION_PREFIX}converted\.pdf$"),
        re.compile(rf"^_scanner-canary/{_UUID}\.pdf$"),
    ),
    BlobContainer.RENDITIONS: (
        re.compile(rf"^{_VERSION_PREFIX}pages/{_NUMBER}\.pdf$"),
        re.compile(rf"^{_VERSION_PREFIX}document\.pdf$"),
        re.compile(rf"^{_VERSION_PREFIX}complete\.json$"),
    ),
    BlobContainer.INGESTION_WORK: (
        re.compile(rf"^{_VERSION_PREFIX}convert/converted\.pdf$"),
        re.compile(rf"^{_VERSION_PREFIX}extract/part-{_PADDED}-{_PADDED}\.json$"),
        re.compile(rf"^{_VERSION_PREFIX}extract/document\.json$"),
        re.compile(rf"^{_VERSION_PREFIX}enrich/table-summaries\.jsonl$"),
        re.compile(rf"^{_VERSION_PREFIX}chunks/{_UUID}\.jsonl$"),
        re.compile(rf"^{_VERSION_PREFIX}embeddings/{_UUID}/batch-{_PADDED}\.json$"),
    ),
}
"""Every path shape of `14-ingestion.md` section 6.14, by container."""

_PREFIX_PATTERNS: Final[dict[BlobContainer, re.Pattern[str]]] = {
    BlobContainer.DOCUMENTS: re.compile(rf"^{_UUID}/(?:{_UUID}/(?:{_NUMBER}/)?)?$"),
    BlobContainer.RENDITIONS: re.compile(rf"^{_UUID}/(?:{_UUID}/(?:{_NUMBER}/)?)?$"),
    BlobContainer.INGESTION_WORK: re.compile(rf"^{_VERSION_PREFIX}$"),
}
_WORK_PREFIX_RE: Final = _PREFIX_PATTERNS[BlobContainer.INGESTION_WORK]


def _id(value: UUID) -> str:
    if not isinstance(value, UUID):
        raise TypeError("a path segment is built from a UUID")
    return str(value)


def _positive(value: int) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("a version or page number is a positive integer")
    return value


def _padded(value: int, *, first: int) -> str:
    if type(value) is not int or not first <= value <= PADDED_NUMBER_MAX:
        raise ValueError(f"a part or batch number lies from {first} to {PADDED_NUMBER_MAX}")
    return f"{value:05d}"


def validate_blob_path(container: BlobContainer, path: str) -> str:
    """Return `path` when it has one of `container`'s shapes; raise `ValueError` otherwise. Every
    builder checks its result here, and a path read back from data is checked before use."""
    if not any(pattern.fullmatch(path) for pattern in PATH_PATTERNS[BlobContainer(container)]):
        raise ValueError("not a blob path this container holds")
    return path


def validate_blob_prefix(container: BlobContainer, prefix: str) -> str:
    """Return a listing prefix (`{indexId}/`, `{indexId}/{documentId}/`, a version's or a work
    prefix) when it has a shape of `container`; raise `ValueError` otherwise."""
    if not _PREFIX_PATTERNS[BlobContainer(container)].fullmatch(prefix):
        raise ValueError("not a blob prefix this container holds")
    return prefix


def _version_prefix(first: UUID, second: UUID, version: int) -> str:
    return f"{_id(first)}/{_id(second)}/{_positive(version)}/"


def source_path(index_id: UUID, document_id: UUID, version: int) -> str:
    """`{indexId}/{documentId}/{v}/source` in `documents`: the uploaded bytes, written once."""
    path = f"{_version_prefix(index_id, document_id, version)}source"
    return validate_blob_path(BlobContainer.DOCUMENTS, path)


def converted_path(index_id: UUID, document_id: UUID, version: int) -> str:
    """`{indexId}/{documentId}/{v}/converted.pdf` in `documents`: a DOCX or PPTX version's PDF."""
    path = f"{_version_prefix(index_id, document_id, version)}converted.pdf"
    return validate_blob_path(BlobContainer.DOCUMENTS, path)


def scanner_canary_path(canary_id: UUID) -> str:
    """`_scanner-canary/{id}.pdf` in `documents`: a canary proving that scanning works."""
    return validate_blob_path(BlobContainer.DOCUMENTS, f"_scanner-canary/{_id(canary_id)}.pdf")


def index_prefix(index_id: UUID) -> str:
    """`{indexId}/`: everything a logical index holds in `documents` and `renditions`, which
    `purge_index_blobs` lists."""
    return validate_blob_prefix(BlobContainer.DOCUMENTS, f"{_id(index_id)}/")


def rendition_page_path(index_id: UUID, document_id: UUID, version: int, page: int) -> str:
    """`{indexId}/{documentId}/{v}/pages/{n}.pdf` in `renditions`: page `n`, 1-based."""
    prefix = _version_prefix(index_id, document_id, version)
    return validate_blob_path(BlobContainer.RENDITIONS, f"{prefix}pages/{_positive(page)}.pdf")


def rendition_document_path(index_id: UUID, document_id: UUID, version: int) -> str:
    """`{indexId}/{documentId}/{v}/document.pdf` in `renditions`: the whole-document rendition."""
    path = f"{_version_prefix(index_id, document_id, version)}document.pdf"
    return validate_blob_path(BlobContainer.RENDITIONS, path)


def rendition_marker_path(index_id: UUID, document_id: UUID, version: int) -> str:
    """`{indexId}/{documentId}/{v}/complete.json` in `renditions`: the completion marker."""
    path = f"{_version_prefix(index_id, document_id, version)}complete.json"
    return validate_blob_path(BlobContainer.RENDITIONS, path)


def work_prefix(run_id: UUID, document_id: UUID, version: int) -> str:
    """`{runId}/{documentId}/{v}/` in `ingestion-work`, so two runs never share a work file."""
    prefix = _version_prefix(run_id, document_id, version)
    return validate_blob_prefix(BlobContainer.INGESTION_WORK, prefix)


def _work_path(prefix: str, rest: str) -> str:
    if not _WORK_PREFIX_RE.fullmatch(prefix):
        raise ValueError("not a work prefix")
    return validate_blob_path(BlobContainer.INGESTION_WORK, f"{prefix}{rest}")


def work_converted_path(prefix: str) -> str:
    """`{workPrefix}convert/converted.pdf`: the PDF Extract's conversion call wrote."""
    return _work_path(prefix, "convert/converted.pdf")


def extract_part_path(prefix: str, first_page: int, last_page: int) -> str:
    """`{workPrefix}extract/part-{first:05d}-{last:05d}.json`: one page-wise Extract part."""
    if type(first_page) is int and type(last_page) is int and first_page > last_page:
        raise ValueError("a part's first page comes before its last")
    first = _padded(first_page, first=1)
    last = _padded(last_page, first=1)
    return _work_path(prefix, f"extract/part-{first}-{last}.json")


def extract_document_path(prefix: str) -> str:
    """`{workPrefix}extract/document.json`: the recursive method's whole-document extraction."""
    return _work_path(prefix, "extract/document.json")


def table_summaries_path(prefix: str) -> str:
    """`{workPrefix}enrich/table-summaries.jsonl`: one line per finished table."""
    return _work_path(prefix, "enrich/table-summaries.jsonl")


def chunks_path(prefix: str, chunk_set_id: UUID) -> str:
    """`{workPrefix}chunks/{chunkSetId}.jsonl`: a header line, then one line per chunk."""
    return _work_path(prefix, f"chunks/{_id(chunk_set_id)}.jsonl")


def embedding_batch_path(prefix: str, chunk_set_id: UUID, batch: int) -> str:
    """`{workPrefix}embeddings/{chunkSetId}/batch-{n:05d}.json`: batch `n`, 0-based."""
    number = _padded(batch, first=0)
    return _work_path(prefix, f"embeddings/{_id(chunk_set_id)}/batch-{number}.json")


# --- Physical index names -------------------------------------------------------------------------

PHYSICAL_INDEX_NAME_PATTERN: Final = r"^docchat-[0-9a-f]{32}-g[1-9][0-9]{0,4}$"
PHYSICAL_INDEX_NAME_MAX_LENGTH: Final = 47
GENERATION_MAX: Final = 99_999

_PHYSICAL_INDEX_NAME_RE: Final = re.compile(PHYSICAL_INDEX_NAME_PATTERN)
_SERVICE_INDEX_NAME_RE: Final = re.compile(r"^[a-z0-9][a-z0-9_-]{1,127}$")


def validate_physical_index_name(name: str) -> str:
    """Return `name` when it matches the generated pattern and the search service's rules (2 to
    128 lower-case letters, digits, `-` and `_`, no consecutive dashes; at most 47 characters
    here). A mismatch is a programming error, which the failure boundary turns into
    `internal_error`."""
    if (
        not _PHYSICAL_INDEX_NAME_RE.fullmatch(name)
        or not _SERVICE_INDEX_NAME_RE.fullmatch(name)
        or "--" in name
        or len(name) > PHYSICAL_INDEX_NAME_MAX_LENGTH
    ):
        raise ValueError("not a generated physical index name")
    return name


def physical_index_name(search_index_id: UUID, generation: int) -> str:
    """`docchat-` + the logical index ID's 32 hexadecimal digits + `-g` + the generation, so
    generation 2 of `3f2c0d9e-8b7a-41c6-a5d4-e3f2c1b0a998` is
    `docchat-3f2c0d9e8b7a41c6a5d4e3f2c1b0a998-g2`. No admin text ever enters a name."""
    if not isinstance(search_index_id, UUID):
        raise TypeError("an index name is built from the logical index's UUID")
    if type(generation) is not int or not 1 <= generation <= GENERATION_MAX:
        raise ValueError(f"a generation lies from 1 to {GENERATION_MAX}")
    return validate_physical_index_name(f"docchat-{search_index_id.hex}-g{generation}")
