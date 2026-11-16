"""`REASONS`: the validation-reason catalogue (`11-api.md` section 2.5; ADR 0012).

Every `422` carries at least one `errors[]` entry whose `reason` is a key of this catalogue. The
catalogue is open: an area that adds a reason adds its entry here in the same change.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

REASON_PATTERN: Final = r"^[a-z][a-z0-9_]*$"

_REASON_RE: Final = re.compile(REASON_PATTERN)


@dataclass(frozen=True, slots=True)
class ReasonEntry:
    reason: str
    meaning: str

    def __post_init__(self) -> None:
        if not _REASON_RE.fullmatch(self.reason):
            raise ValueError(f"invalid reason {self.reason!r}")
        if not self.meaning:
            raise ValueError(f"{self.reason}: a reason states its meaning")


_ENTRIES: Final = (
    ReasonEntry("required", "a required field, parameter, header or body is missing"),
    ReasonEntry(
        "unknown_field",
        "a body field or query parameter that isn't in the model, or a body sent where none "
        "is accepted",
    ),
    ReasonEntry("invalid_type", "the wrong JSON type"),
    ReasonEntry("invalid_json", "the body isn't JSON, or isn't UTF-8"),
    ReasonEntry("invalid_uuid", "not a canonical UUID"),
    ReasonEntry(
        "invalid_format",
        "a pattern mismatch (for example a SHA-256 that isn't 64 hexadecimal characters, a file "
        "extension that isn't accepted, content that doesn't match its type or hash)",
    ),
    ReasonEntry(
        "invalid_cursor",
        "a cursor that fails to decode, fails its model or doesn't match the request's filters",
    ),
    ReasonEntry(
        "invalid_characters",
        "a code point of category Cc, Cf, Cs, Co or Cn in a text type (LF and TAB allowed in "
        "multi-line text; the chat question allows LF and TAB and refuses other Cc code points "
        "and surrogates)",
    ),
    ReasonEntry("too_short", "below the minimum length or item count"),
    ReasonEntry("too_long", "above the maximum length"),
    ReasonEntry("out_of_range", "a number outside its range, or a declared size that doesn't fit"),
    ReasonEntry("too_many_items", "a list above its maximum"),
    ReasonEntry("not_allowed", "a value not allowed here or in this combination"),
    ReasonEntry("duplicate", "a duplicate list entry"),
    ReasonEntry("not_found", "a referenced identifier has no row"),
    ReasonEntry("category_requires_down", "a feedback category with rating `up`"),
    ReasonEntry(
        "comments_disabled", "a feedback comment while `feedback.comments_enabled` is false"
    ),
    ReasonEntry(
        "empty",
        "nothing is left after normalisation, or a question holds no code point of category "
        "L, N, P or S",
    ),
    ReasonEntry(
        "contains_link", "a memory fact holds a URL, host path, IP address or e-mail address"
    ),
    ReasonEntry("contains_code", "a memory fact holds code or markup"),
    ReasonEntry(
        "contains_instruction",
        "a memory fact reads as an instruction to the assistant (a pattern match, or a Prompt "
        "Shields detection on changed text)",
    ),
    ReasonEntry("contains_secret", "a memory fact looks like a credential or a long number"),
    ReasonEntry("duplicate_fact", "a memory fact equals or nearly equals another fact"),
    ReasonEntry("memory_full", "the facts' token total would exceed `memory.max_tokens`"),
)


def _catalogue(entries: Sequence[ReasonEntry]) -> Mapping[str, ReasonEntry]:
    by_reason: dict[str, ReasonEntry] = {}
    for entry in entries:
        if entry.reason in by_reason:
            raise ValueError(f"reason {entry.reason!r} is registered twice")
        by_reason[entry.reason] = entry
    return MappingProxyType(by_reason)


REASONS: Final[Mapping[str, ReasonEntry]] = _catalogue(_ENTRIES)
"""Every `422` reason of `11-api.md` section 2.5, keyed by reason; read-only."""
