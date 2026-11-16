"""Retrieved sources as the pipeline carries them, from a search result to a numbered source block
(`12-chat-pipeline.md` sections 6.1, 6.5, 8.1, 9.7 and 11.3).

A `SearchHit` holds the ten selected fields of one result, read through the schema version's
`ChunkIndexFields`, so no index field-name literal appears here; a result missing a field the
pipeline relies on is malformed and never becomes a hit. A `Candidate` is a hit that survived the
served-chunk-set filter, with its rank and the document's `source_name`. Its `body` is the exact
text that screening analyses, the `SOURCE` block carries and `allowedLinks` are read from: the
title line, the section line (left out without a heading), the page line, an empty line and the
chunk text, every value defanged. Its title and heading items are also its citation's.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from docchat_api.chat.prompts import defang
from docchat_core.cosmos.conversations import CitationContentType
from docchat_core.search.schema import ChunkIndexFields

TITLE_MAX_CODE_POINTS: Final = 300
HEADING_MAX_CODE_POINTS: Final = 200
HEADING_ITEMS_MAX: Final = 8
HEADING_SEPARATOR: Final = " > "
CHUNK_KEY_MAX_LENGTH: Final = 256
SCORE_KEY: Final = "@search.score"
RERANKER_SCORE_KEY: Final = "@search.reranker_score"
CONTENT_TYPES: Final = frozenset(member.value for member in CitationContentType)


def _uuid(value: object) -> UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _score(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchHit:
    """One search result; `page_number` is `None` when the result carries no valid page."""

    chunk_key: str
    document_id: UUID
    document_version_id: UUID
    chunk_set_id: UUID
    title: str
    heading_path: tuple[str, ...]
    page_number: int | None
    content_type: str
    chunk_text: str
    search_score: float
    reranker_score: float | None

    @classmethod
    def read(cls, result: Mapping[str, Any], fields: ChunkIndexFields) -> SearchHit | None:
        """The hit of one result, or `None` when the key or an identifier the trace and the
        filter need is missing or malformed."""
        key = result.get(fields.key)
        document_id = _uuid(result.get(fields.document_id))
        version_id = _uuid(result.get(fields.document_version_id))
        chunk_set_id = _uuid(result.get(fields.chunk_set_id))
        if (
            not isinstance(key, str)
            or not 1 <= len(key) <= CHUNK_KEY_MAX_LENGTH
            or document_id is None
            or version_id is None
            or chunk_set_id is None
        ):
            return None
        page = result.get(fields.page_number)
        headings = result.get(fields.heading_path)
        title = result.get(fields.title)
        text = result.get(fields.chunk_text)
        content_type = result.get(fields.content_type)
        return cls(
            chunk_key=key,
            document_id=document_id,
            document_version_id=version_id,
            chunk_set_id=chunk_set_id,
            title=title if isinstance(title, str) else "",
            heading_path=tuple(item for item in headings or () if isinstance(item, str))
            if isinstance(headings, list)
            else (),
            page_number=page if isinstance(page, int) and not isinstance(page, bool) else None,
            content_type=content_type if isinstance(content_type, str) else "",
            chunk_text=text if isinstance(text, str) else "",
            search_score=_score(result.get(SCORE_KEY)) or 0.0,
            reranker_score=_score(result.get(RERANKER_SCORE_KEY)),
        )

    @property
    def well_formed(self) -> bool:
        """A page of 1 or more and one of the three citable content types: ingestion sets both
        on every chunk, so a result without them is malformed and dropped like an unserved one."""
        return (
            self.page_number is not None
            and self.page_number >= 1
            and self.content_type in CONTENT_TYPES
        )


def _stem(source_name: str) -> str:
    stem, dot, _ = source_name.rpartition(".")
    return stem if dot and stem else source_name


@dataclass(frozen=True, slots=True, kw_only=True)
class Candidate:
    """A hit that survived the served-chunk-set filter: its rank from 1 and its document's
    `source_name`."""

    rank: int
    hit: SearchHit
    source_name: str

    @property
    def title(self) -> str:
        """The trimmed title field, else the source name without its last extension; defanged
        and cut to 300 code points."""
        title = self.hit.title.strip() or _stem(self.source_name).strip() or self.source_name
        return defang(title)[:TITLE_MAX_CODE_POINTS]

    @property
    def heading_items(self) -> tuple[str, ...]:
        """The heading path's items, each trimmed and defanged, empty ones dropped, each cut to
        200 code points, at most the first 8."""
        items = (defang(item.strip())[:HEADING_MAX_CODE_POINTS] for item in self.hit.heading_path)
        return tuple(item for item in items if item)[:HEADING_ITEMS_MAX]

    @property
    def page_number(self) -> int:
        page = self.hit.page_number
        if page is None:  # pragma: no cover - the filter keeps only well-formed hits
            raise ValueError("a candidate has a page")
        return page

    @property
    def body(self) -> str:
        """The source block's body, every value defanged."""
        lines = [f"Title: {self.title}"]
        headings = self.heading_items
        if headings:
            lines.append(f"Section: {HEADING_SEPARATOR.join(headings)}")
        lines.append(f"Page: {self.page_number}")
        lines.append("")
        lines.append(defang(self.hit.chunk_text))
        return "\n".join(lines)


@dataclass(frozen=True, slots=True, kw_only=True)
class NumberedSource:
    """A screened source with its ID in the answer prompt, `S1` onwards in rank order."""

    number: int
    candidate: Candidate
    body: str

    @property
    def source_id(self) -> str:
        return f"S{self.number}"
