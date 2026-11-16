"""Validation of the follow-up suggestions (`12-chat-pipeline.md` section 13.3; `82-security.md`
SEC-43).

Each returned item is trimmed; it is dropped when it is empty, longer than 200 characters, holds
`//` or `www.` once its format characters (category Cf, U+00AD and U+200B among them) are removed,
duplicates an earlier item, or equals the question, comparing after `str.casefold()`; the first
`followups.max_count` items that remain are kept. The `//` test covers `://` and the
protocol-relative form, so no kept item holds text the client would redact, and a suggestion the
user picks still equals the stored item for the suggested-question test.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from typing import Final

FOLLOWUP_MAX_CHARACTERS: Final = 200


def _without_format_characters(text: str) -> str:
    return "".join(character for character in text if unicodedata.category(character) != "Cf")


def holds_link(text: str) -> bool:
    """Whether `text`, without its format characters, holds `//` or `www.` (any case)."""
    folded = _without_format_characters(text).casefold()
    return "//" in folded or "www." in folded


def validate_followups(items: Iterable[str], question: str, max_count: int) -> list[str]:
    """The kept follow-up questions, in the model's order, at most `max_count`."""
    asked = question.casefold()
    kept: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = item.strip()
        folded = text.casefold()
        if (
            not text
            or len(text) > FOLLOWUP_MAX_CHARACTERS
            or holds_link(text)
            or folded in seen
            or folded == asked
        ):
            continue
        seen.add(folded)
        kept.append(text)
        if len(kept) == max_count:
            break
    return kept
