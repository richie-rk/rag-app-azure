"""`ITEM_CODES`: the ingestion item-code catalogue (`14-ingestion.md` section 17.2).

An item code is stored on a run item (`outcome_code`, with its message in `outcome_message`), on a
failed version (`failure_code`) or as a warning `{code, page}`, and reaches the admin screens as
data (`11-api.md` section 2.6), never as a problem. It is a catalogue of its own because four of
its codes share a name with a problem code of `CODES` while their item messages differ, and each
catalogue keeps one text per code. The ingestion app reads `retried` to decide whether
`call_with_retry` retries a code inside the item.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

ITEM_CODE_PATTERN: Final = r"^[a-z][a-z0-9_]*$"
ITEM_CODE_MAX_LENGTH: Final = 64
ITEM_MESSAGE_MAX_LENGTH: Final = 400

_ITEM_CODE_RE: Final = re.compile(ITEM_CODE_PATTERN)


class ItemCodeKind(StrEnum):
    FAILURE = "failure"
    FAILURE_AFTER_RETRIES = "failure_after_retries"
    SKIP = "skip"
    SKIP_OR_CANCEL = "skip_or_cancel"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class ItemCode:
    """One item code. `retried` is `None` where retrying doesn't apply (skips and warnings)."""

    code: str
    kind: ItemCodeKind
    message: str
    retried: bool | None

    def __post_init__(self) -> None:
        if not _ITEM_CODE_RE.fullmatch(self.code) or len(self.code) > ITEM_CODE_MAX_LENGTH:
            raise ValueError(f"invalid item code {self.code!r}")
        if not 1 <= len(self.message) <= ITEM_MESSAGE_MAX_LENGTH:
            raise ValueError(
                f"{self.code}: a message has 1 to {ITEM_MESSAGE_MAX_LENGTH} characters"
            )
        is_failure = self.kind in (ItemCodeKind.FAILURE, ItemCodeKind.FAILURE_AFTER_RETRIES)
        if is_failure != (self.retried is not None):
            raise ValueError(f"{self.code}: only a failure states whether it is retried")


_F: Final = ItemCodeKind.FAILURE
_FR: Final = ItemCodeKind.FAILURE_AFTER_RETRIES
_SKIP: Final = ItemCodeKind.SKIP
_SKIP_OR_CANCEL: Final = ItemCodeKind.SKIP_OR_CANCEL
_WARN: Final = ItemCodeKind.WARNING

_TOO_COMPLEX: Final = "The file is too complex to process safely."
_CONVERT_LIMITS: Final = "The document couldn't be converted within the allowed time or memory."
_PARSE_LIMITS: Final = "The document couldn't be processed within the allowed time or memory."
_SOURCE_GONE: Final = "The stored file is missing or changed. Re-upload it."
_LAYOUT_DOWN: Final = "The layout service was unavailable. Reprocess the document later."
_EMBEDDING_DOWN: Final = "The embedding service was unavailable. Reprocess the document later."
_SEARCH_REFUSED: Final = (
    "The search service didn't accept the passages. Reprocess the document later."
)
_TARGET_CHANGED: Final = "The target index changed while processing. Reprocess the document."
_STORAGE_DOWN: Final = "A storage service was unavailable. Reprocess the document later."
_INDEX_NOT_CREATED: Final = "The search index couldn't be created."
_NOTHING_TARGET_CHANGED: Final = "Nothing to do: the target index changed."
_TABLE_WITHOUT_SUMMARY: Final = "A table was indexed without a summary."

_ENTRIES: Final = (
    ItemCode(
        "malware_detected",
        _F,
        "The file was blocked because a security scan found a threat.",
        retried=False,
    ),
    ItemCode(
        "scan_unavailable",
        _F,
        "The security scan didn't finish in time. Re-upload the file.",
        retried=False,
    ),
    ItemCode(
        "scan_failed",
        _F,
        "The security scan couldn't check this file. Re-upload it.",
        retried=False,
    ),
    ItemCode(
        "file_signature_mismatch", _F, "The file's content doesn't match its type.", retried=False
    ),
    ItemCode(
        "encrypted_document",
        _F,
        "The file is password-protected or encrypted. Upload an unprotected copy.",
        retried=False,
    ),
    ItemCode("zip_entry_limit_exceeded", _F, _TOO_COMPLEX, retried=False),
    ItemCode("zip_size_limit_exceeded", _F, _TOO_COMPLEX, retried=False),
    ItemCode(
        "corrupt_document", _F, "The file couldn't be read. It may be damaged.", retried=False
    ),
    ItemCode(
        "macro_content_declared", _F, "Files that contain macros aren't accepted.", retried=False
    ),
    ItemCode("convert_timeout", _F, _CONVERT_LIMITS, retried=False),
    ItemCode("convert_memory_exceeded", _F, _CONVERT_LIMITS, retried=False),
    ItemCode(
        "convert_failed",
        _F,
        "The document couldn't be converted to PDF. It may be damaged.",
        retried=False,
    ),
    ItemCode("source_changed", _F, _SOURCE_GONE, retried=False),
    ItemCode("source_missing", _F, _SOURCE_GONE, retried=False),
    ItemCode("page_limit_exceeded", _F, "The document has more pages than allowed.", retried=False),
    ItemCode("chunk_limit_exceeded", _F, "The document is too large to index.", retried=False),
    ItemCode("no_extractable_text", _F, "No text could be found in this document.", retried=False),
    ItemCode("parse_timeout", _F, _PARSE_LIMITS, retried=False),
    ItemCode("parse_memory_exceeded", _F, _PARSE_LIMITS, retried=False),
    ItemCode("parse_failed", _F, _PARSE_LIMITS, retried=False),
    ItemCode("docintel_throttled", _FR, _LAYOUT_DOWN, retried=True),
    ItemCode("docintel_unavailable", _FR, _LAYOUT_DOWN, retried=True),
    ItemCode("docintel_timeout", _FR, _LAYOUT_DOWN, retried=True),
    ItemCode(
        "docintel_failed", _F, "The layout service couldn't analyse this document.", retried=False
    ),
    ItemCode(
        "table_summary_unavailable",
        _FR,
        "The table summary service was unavailable. Reprocess the document later.",
        retried=True,
    ),
    ItemCode("embedding_throttled", _FR, _EMBEDDING_DOWN, retried=True),
    ItemCode("embedding_unavailable", _FR, _EMBEDDING_DOWN, retried=True),
    ItemCode(
        "embedding_failed", _F, "The document's passages couldn't be embedded.", retried=False
    ),
    ItemCode("index_upload_failed", _FR, _SEARCH_REFUSED, retried=True),
    ItemCode("search_unavailable", _FR, _SEARCH_REFUSED, retried=True),
    ItemCode("target_missing", _F, _TARGET_CHANGED, retried=False),
    ItemCode("chunk_set_retired", _F, _TARGET_CHANGED, retried=False),
    ItemCode(
        "render_failed", _F, "Page previews couldn't be created for this document.", retried=False
    ),
    ItemCode(
        "render_too_large",
        _F,
        "Page previews for this document are too large to create.",
        retried=False,
    ),
    ItemCode(
        "cleanup_failed",
        _FR,
        "Old passages or files couldn't be removed yet. Reprocess the document later.",
        retried=True,
    ),
    ItemCode(
        "activity_timeout",
        _FR,
        "Processing took too long. Reprocess the document later.",
        retried=True,
    ),
    ItemCode("storage_unavailable", _FR, _STORAGE_DOWN, retried=True),
    ItemCode("database_unavailable", _FR, _STORAGE_DOWN, retried=True),
    ItemCode("index_create_failed", _F, _INDEX_NOT_CREATED, retried=False),
    ItemCode("index_schema_conflict", _F, _INDEX_NOT_CREATED, retried=False),
    ItemCode("search_index_capacity", _F, _INDEX_NOT_CREATED, retried=False),
    ItemCode(
        "index_delete_failed",
        _FR,
        "The old search index couldn't be removed yet.",
        retried=True,
    ),
    ItemCode(
        "processing_interrupted",
        _F,
        "Processing stopped unexpectedly several times. Reprocess the document.",
        retried=False,
    ),
    # Retried within `ingestion.retry_max_attempts`, although its kind is a plain failure.
    ItemCode(
        "internal_error",
        _F,
        "Something went wrong while processing. Reprocess the document.",
        retried=True,
    ),
    ItemCode(
        "already_served",
        _SKIP,
        "Nothing to do: the result is already current.",
        retried=None,
    ),
    ItemCode("superseded_by_newer", _SKIP, "Nothing to do: a newer version exists.", retried=None),
    ItemCode("version_not_ready", _SKIP, "Nothing to do: this version isn't ready.", retried=None),
    ItemCode(
        "document_deleted",
        _SKIP_OR_CANCEL,
        "Not processed because the document was deleted.",
        retried=None,
    ),
    ItemCode("target_retired", _SKIP, _NOTHING_TARGET_CHANGED, retried=None),
    ItemCode("target_repointed", _SKIP_OR_CANCEL, _NOTHING_TARGET_CHANGED, retried=None),
    ItemCode(
        "renditions_current",
        _SKIP,
        "Nothing to do: the page previews are already current.",
        retried=None,
    ),
    ItemCode(
        "index_deleted",
        _SKIP_OR_CANCEL,
        "Not processed because the index was deleted.",
        retried=None,
    ),
    ItemCode("table_summary_filtered", _WARN, _TABLE_WITHOUT_SUMMARY, retried=None),
    ItemCode("table_summary_discarded", _WARN, _TABLE_WITHOUT_SUMMARY, retried=None),
    ItemCode(
        "table_summary_input_truncated",
        _WARN,
        "A table was too long to summarise in full.",
        retried=None,
    ),
    ItemCode("cleanup_incomplete", _WARN, "Clean-up will finish later.", retried=None),
    ItemCode(
        "converted_pdf_missing",
        _WARN,
        "The stored PDF copy was missing, so the document was converted from its source.",
        retried=None,
    ),
)


def _catalogue(entries: Sequence[ItemCode]) -> Mapping[str, ItemCode]:
    by_code: dict[str, ItemCode] = {}
    for entry in entries:
        if entry.code in by_code:
            raise ValueError(f"item code {entry.code!r} is registered twice")
        by_code[entry.code] = entry
    return MappingProxyType(by_code)


ITEM_CODES: Final[Mapping[str, ItemCode]] = _catalogue(_ENTRIES)
"""Every ingestion item code of `14-ingestion.md` section 17.2, keyed by code; read-only."""
