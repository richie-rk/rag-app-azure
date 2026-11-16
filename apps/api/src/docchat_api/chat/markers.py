"""Citation markers, citation tokens, citation records and allowed links (`12-chat-pipeline.md`
sections 10.3, 11 and 14.2; ADR 0064; `82-security.md` SEC-34 and SEC-39).

The model writes markers such as `[S2]`; `CitationMinter` turns them into the reserved tokens
`⟦n⟧` as the answer streams. Before parsing, every U+27E6 and U+27E7 is deleted from the model's
text, so only the API mints a token. Each complete marker (case-insensitive, `[S2]`, `[ S2 ]`,
`[S2, S5]`, `[S2; S5]`; `[S02]` names `S2`) becomes one token per distinct known source it names,
in its order, numbered by first use across the answer from 1 to 16; a source this answer call
didn't receive (an invented ID, a flagged or a budget-dropped source) leaves nothing. A marker
split across deltas is held back while the text from the last `[` is at most 40 characters and
can still become a marker, so a split at any character gives the unsplit text's output; at the
end of the stream the held text is released as the model wrote it.

`allowed_links` lists the `https` URLs that appear verbatim in the source blocks sent, the only
URLs the client renders as links.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Final
from urllib.parse import urlsplit
from uuid import UUID

from docchat_api.chat.sources import NumberedSource
from docchat_core.chat import CITATION_TOKEN_CLOSE, CITATION_TOKEN_OPEN, strip_citation_tokens
from docchat_core.cosmos.conversations import CitationContentType, StoredCitation

PARTIAL_HOLD_MAX: Final = 40
"""Longer than the longest marker group the parser accepts."""
ANSWER_MAX_CODE_POINTS: Final = 100_000
"""The conversation store's limit for a stored answer: the answer cap."""
DELTA_MAX_CODE_POINTS: Final = 4_000
REPLAY_DELTA_MAX_CODE_POINTS: Final = 2_000
ALLOWED_LINKS_MAX: Final = 50
ALLOWED_LINK_MAX_LENGTH: Final = 2_048
HTTPS_PREFIX_LENGTH: Final = len("https://")

COMPLETE_MARKER: Final = re.compile(r"\[\s*S(\d{1,2})(?:\s*[,;]\s*S(\d{1,2}))*\s*\]", re.IGNORECASE)
PARTIAL_SUFFIX: Final = re.compile(
    r"^\[\s*(?:S\d{0,2}(?:\s*[,;](?:\s*S\d{0,2})?)*\s*)?$", re.IGNORECASE
)
"""Section 11.1's partial suffix with the whitespace the complete marker allows before `]`, so
`[ S2 ` split from its `]` is held like any other split. It accepts the same strings as that
pattern with whitespace allowed on both sides of a separator, but no two whitespace runs are
adjacent, so a suffix that doesn't match is refused without exponential backtracking."""
_SOURCE_REFERENCE: Final = re.compile(r"S(\d{1,2})", re.IGNORECASE)
_RESERVED: Final = str.maketrans("", "", CITATION_TOKEN_OPEN + CITATION_TOKEN_CLOSE)
_TOKEN: Final = re.compile(f"{CITATION_TOKEN_OPEN}\\d+{CITATION_TOKEN_CLOSE}")

ALLOWED_LINK: Final = re.compile(r"(?i:https)://[^\s<>\"'{}|\\^\[\]`]+")
_TRAILING: Final = frozenset(".,;:!?'\"*_")


def delete_reserved(text: str) -> str:
    """`text` without U+27E6 and U+27E7."""
    return text.translate(_RESERVED)


def token(number: int) -> str:
    return f"{CITATION_TOKEN_OPEN}{number}{CITATION_TOKEN_CLOSE}"


def has_visible_text(answer: str) -> bool:
    """Whether the answer, with its citation tokens removed, holds a non-whitespace character."""
    return bool(strip_citation_tokens(answer).strip())


def _safe_cut(text: str, limit: int) -> int:
    """The largest cut at or before `limit` that isn't inside a citation token."""
    if len(text) <= limit:
        return len(text)
    for match in _TOKEN.finditer(text, max(0, limit - 4), limit + 4):
        if match.start() < limit < match.end():
            return match.start()
    return limit


def split_deltas(text: str, limit: int = DELTA_MAX_CODE_POINTS) -> list[str]:
    """`text` as delta texts of at most `limit` code points, never cut inside a citation token;
    no piece is empty."""
    pieces: list[str] = []
    while text:
        cut = _safe_cut(text, limit) or min(len(text), limit)
        pieces.append(text[:cut])
        text = text[cut:]
    return pieces


class CitationMinter:
    """The marker processing of one answer call over the sources it received."""

    def __init__(self, sources: Sequence[NumberedSource]) -> None:
        self._sources: Mapping[int, NumberedSource] = {source.number: source for source in sources}
        self._numbers: dict[int, int] = {}
        self._buffer = ""
        self._answer: list[str] = []
        self._released = 0
        self.received: list[str] = []
        """The content deltas as they arrived, for the estimate of an interrupted call."""
        self.capped = False

    @property
    def answer(self) -> str:
        """The text released so far, citation tokens included."""
        return "".join(self._answer)

    def citations(self, *, search_index_id: UUID, physical_index_id: UUID) -> list[StoredCitation]:
        """One record per citation number minted so far, in number order."""
        records: list[StoredCitation] = []
        for source_number, citation_number in sorted(self._numbers.items(), key=lambda i: i[1]):
            candidate = self._sources[source_number].candidate
            hit = candidate.hit
            records.append(
                StoredCitation(
                    number=citation_number,
                    title=candidate.title,
                    page_number=candidate.page_number,
                    heading_path=list(candidate.heading_items),
                    content_type=CitationContentType(hit.content_type),
                    open_as="pdf_page",
                    source_id=f"S{source_number}",
                    chunk_key=hit.chunk_key,
                    search_index_id=search_index_id,
                    physical_index_id=physical_index_id,
                    document_id=hit.document_id,
                    document_version_id=hit.document_version_id,
                    chunk_set_id=hit.chunk_set_id,
                )
            )
        return records

    def _convert(self, match: re.Match[str]) -> str:
        tokens: list[str] = []
        named: set[int] = set()
        for reference in _SOURCE_REFERENCE.finditer(match.group(0)):
            source_number = int(reference.group(1))
            if source_number in named or source_number not in self._sources:
                continue
            named.add(source_number)
            number = self._numbers.setdefault(source_number, len(self._numbers) + 1)
            tokens.append(token(number))
        return "".join(tokens)

    def _release(self, text: str) -> str:
        """Append `text` to the answer up to the cap; what was actually released."""
        room = ANSWER_MAX_CODE_POINTS - self._released
        if len(text) >= room:
            text = text[: _safe_cut(text, room)]
            self.capped = True
        self._answer.append(text)
        self._released += len(text)
        return text

    def feed(self, delta: str) -> str:
        """Process one model delta; the text released now (possibly empty)."""
        if self.capped:
            return ""
        self.received.append(delta)
        self._buffer = COMPLETE_MARKER.sub(self._convert, self._buffer + delete_reserved(delta))
        held = ""
        last = self._buffer.rfind("[")
        if last >= 0:
            suffix = self._buffer[last:]
            if len(suffix) <= PARTIAL_HOLD_MAX and PARTIAL_SUFFIX.match(suffix):
                held = suffix
        released = self._buffer[: len(self._buffer) - len(held)]
        self._buffer = held
        return self._release(released) if released else ""

    def finish(self) -> str:
        """The end of the stream: the whole buffer, a held partial marker as written."""
        held, self._buffer = self._buffer, ""
        if self.capped or not held:
            return ""
        return self._release(held)


def _trimmed(url: str) -> str:
    while url:
        if url[-1] in _TRAILING or (url[-1] == ")" and url.count(")") > url.count("(")):
            url = url[:-1]
            continue
        break
    return url


def _has_host(url: str) -> bool:
    # The stored turn accepts only an https URL with a host; a match without one can't be listed.
    try:
        return bool(urlsplit(url).hostname)
    except ValueError:
        return False


def allowed_links(bodies: Iterable[str]) -> list[str]:
    """Every distinct `https` URL appearing verbatim in `bodies`, trimmed of trailing
    punctuation and of an unmatched `)`, in first-seen order, at most 50; a URL above 2,048
    characters is left out, never cut."""
    links: list[str] = []
    seen: set[str] = set()
    for body in bodies:
        for match in ALLOWED_LINK.finditer(body):
            url = _trimmed(match.group(0))
            if (
                len(url) <= HTTPS_PREFIX_LENGTH
                or len(url) > ALLOWED_LINK_MAX_LENGTH
                or url in seen
                or not _has_host(url)
            ):
                continue
            seen.add(url)
            links.append(url)
            if len(links) == ALLOWED_LINKS_MAX:
                return links
    return links
