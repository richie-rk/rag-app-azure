"""Fact normalisation, the fact validators and the suggested-question test
(`13-conversations-and-memory.md` sections 7.9 and 7.11).

`validate_fact_text` is one deterministic function that every extracted fact and every user edit
passes, before the caps and before Prompt Shields screening. It works on two forms of a text: the
stored form, in which facts are kept, and the comparison form (NFKC, case-folded) its checks read,
so a full-width `https://` or an upper-case `PASSWORD IS` is caught like its plain spelling.
The patterns are English; the extraction call's `addressesAssistant` flag and the screening catch
instructions in other languages.

`is_suggested_question` tells whether a question repeats a follow-up the chat-small model wrote
or an example question an administrator wrote. Neither is the user's own statement, so the
conversation store never extracts memory from such a turn, and a poisoned document can't word a
fact that persists in every later session.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from typing import Final, Literal

from docchat_core.telemetry import scrub

FACT_MAX_CODE_POINTS: Final = 200
DUPLICATE_SIMILARITY_PERCENT: Final = 85
"""Two same-category facts whose word sets have a Jaccard similarity of at least 0.85 are one
fact."""

type FactTextRejection = Literal[
    "empty",
    "too_long",
    "contains_link",
    "contains_code",
    "contains_instruction",
    "contains_secret",
    "duplicate_fact",
]
"""The reasons `validate_fact_text` returns, in the order its checks run."""

_REMOVED_CATEGORIES: Final = frozenset({"Cc", "Cf", "Co", "Cs"})
_WHITESPACE: Final = re.compile(r"\s+")
_LINE_BREAKS: Final = re.compile(r"\r\n?")

_LINK_PATTERNS: Final = tuple(
    re.compile(pattern)
    for pattern in (
        r"[a-z][a-z0-9+.\-]{1,15}://",
        r"\bwww\.",
        r"\b(?:mailto|data|javascript|vbscript|file|ftp):",
        r"\b(?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}/\S",
        r"\b\d{1,3}(?:\.\d{1,3}){3}\b",
        r"[^\s@]+@[^\s@]+\.[a-z]{2,24}",
    )
)
_CODE_SUBSTRINGS: Final = (
    "`",
    "{{",
    "}}",
    "{%",
    "%}",
    "${",
    "<%",
    "%>",
    "=>",
    "::",
    "&&",
    "||",
    "==",
    "!=",
    "#!",
    "#include",
)
_MARKUP_TAG: Final = re.compile(r"<\s*/?\s*[a-z!][^>]*>")
_CODE_CHARACTERS: Final = frozenset("{}[]<>;=|\\")
_CODE_CHARACTERS_MAX: Final = 2
_INSTRUCTION_PATTERNS: Final = tuple(
    re.compile(pattern)
    for pattern in (
        (
            r"\b(?:ignore|disregard|forget|override|bypass|overrule)\b.{0,60}"
            r"\b(?:instructions?|rules?|prompts?|polic(?:y|ies)|guidelines?|previous|above|system"
            r"|safety)\b"
        ),
        r"\byou\s+(?:must|should|shall|will|are\s+to|have\s+to|need\s+to|can|may)\b",
        (
            r"^(?:always|never|do\s+not|don't|dont|please|make\s+sure|ensure|remember\s+to"
            r"|from\s+now\s+on|answer|respond|reply|say|tell|reveal|print|output|repeat|include|use"
            r"|act)\b"
        ),
        r"\b(?:system\s+prompt|system\s+message|developer\s+message)\b",
        r"(?:^|\s)(?:system|assistant|developer|user)\s*:",
        r"\b(?:act\s+as|pretend\s+to\s+be|role-?play|jailbreak)\b",
        (
            r"\b(?:reveal|print|show|output|repeat|disclose)\b.{0,40}"
            r"\b(?:prompts?|instructions|rules|configuration|secrets?)\b"
        ),
    )
)
_SECRET_PATTERNS: Final = tuple(
    re.compile(pattern)
    for pattern in (
        (
            r"\b(?:password|passwd|pwd|passcode|pin|secret|api[\s_\-]?key|access[\s_\-]?key|token"
            r"|connection\s+string|private\s+key|client\s+secret)\b\s*(?:is|=|:)"
        ),
        r"-----begin [a-z ]*private key-----",
        r"\beyj[a-z0-9_\-]{10,}\.[a-z0-9_\-]{10,}",
    )
)
_KEY_LIKE_RUN: Final = re.compile(r"[A-Za-z0-9+/=_\-]{24,}")
_UPPER: Final = re.compile(r"[A-Z]")
_LOWER: Final = re.compile(r"[a-z]")
_DIGIT: Final = re.compile(r"[0-9]")
_DIGIT_SEPARATOR: Final = re.compile(r"(?<=\d)[ .\-](?=\d)")
_LONG_NUMBER: Final = re.compile(r"\d{9,}")


def _collapse(text: str) -> str:
    return _WHITESPACE.sub(" ", text)


def stored_form(text: str) -> str:
    """The form a fact is stored in: Unicode NFC; each run of whitespace, line breaks included,
    one space; no character of the categories Cc, Cf, Co and Cs; no leading or trailing space.

    Whitespace collapses before the removal, because line breaks and tabs are Cc characters too,
    and once more after it, so a removed character between two spaces leaves one space; the
    result is normalised again, so it stays NFC and the function is idempotent.
    """
    text = _collapse(unicodedata.normalize("NFC", text))
    text = "".join(char for char in text if unicodedata.category(char) not in _REMOVED_CATEGORIES)
    return unicodedata.normalize("NFC", _collapse(text).strip(" "))


def comparison_form(stored: str) -> str:
    """The form checks 3 to 7 read: NFKC of the stored form, case-folded."""
    return unicodedata.normalize("NFKC", stored).casefold()


def duplicate_key(compared: str) -> str:
    """The comparison form without characters of the categories P and S, its whitespace runs
    collapsed to one space."""
    kept = "".join(char for char in compared if unicodedata.category(char)[0] not in "PS")
    return _collapse(kept).strip(" ")


def _contains_link(compared: str) -> bool:
    return any(pattern.search(compared) for pattern in _LINK_PATTERNS)


def _contains_code(compared: str) -> bool:
    if any(fragment in compared for fragment in _CODE_SUBSTRINGS):
        return True
    if _MARKUP_TAG.search(compared):
        return True
    return sum(char in _CODE_CHARACTERS for char in compared) > _CODE_CHARACTERS_MAX


def _contains_instruction(compared: str) -> bool:
    return any(pattern.search(compared) for pattern in _INSTRUCTION_PATTERNS)


def _contains_secret(stored: str, compared: str) -> bool:
    if scrub(stored) != stored:
        return True
    if any(pattern.search(compared) for pattern in _SECRET_PATTERNS):
        return True
    for run in _KEY_LIKE_RUN.findall(stored):
        if _UPPER.search(run) and _LOWER.search(run) and _DIGIT.search(run):
            return True
    return _LONG_NUMBER.search(_DIGIT_SEPARATOR.sub("", stored)) is not None


def _similar(key: str, other: str) -> bool:
    words, other_words = set(key.split()), set(other.split())
    union = len(words | other_words)
    if not union:
        return False
    # Integer arithmetic keeps a similarity of exactly 0.85 a duplicate.
    return 100 * len(words & other_words) >= DUPLICATE_SIMILARITY_PERCENT * union


def _duplicates(compared: str, category: str, others: Iterable[tuple[str, str]]) -> bool:
    key = duplicate_key(compared)
    for other_text, other_category in others:
        other_key = duplicate_key(comparison_form(stored_form(other_text)))
        if other_key == key or (other_category == category and _similar(key, other_key)):
            return True
    return False


def validate_fact_text(  # noqa: PLR0911 - one return per check, in the order of section 7.9
    text: str, category: str, others: Iterable[tuple[str, str]]
) -> FactTextRejection | None:
    """The first check `text` fails, or `None` when it passes.

    `text` is normalised to the stored form first (a stored text is unchanged). `others` holds the
    `(text, category)` pairs the duplicate check compares against, as the caller states them: the
    other stored facts for a user edit; for an extraction, the stored facts other than a replace
    target plus the operations already kept. The callers add the caps afterwards.
    """
    stored = stored_form(text)
    if not stored:
        return "empty"
    if len(stored) > FACT_MAX_CODE_POINTS:
        return "too_long"
    compared = comparison_form(stored)
    if _contains_link(compared):
        return "contains_link"
    if _contains_code(compared):
        return "contains_code"
    if _contains_instruction(compared):
        return "contains_instruction"
    if _contains_secret(stored, compared):
        return "contains_secret"
    if _duplicates(compared, category, others):
        return "duplicate_fact"
    return None


def suggestion_form(text: str) -> str:
    """The suggestion comparison form: CR LF and lone CR become LF; Unicode NFC; each run of
    whitespace, line breaks included, one space; no leading or trailing space; case-folded."""
    text = unicodedata.normalize("NFC", _LINE_BREAKS.sub("\n", text))
    return _collapse(text).strip(" ").casefold()


def is_suggested_question(question: str, suggestions: Iterable[str]) -> bool:
    """Whether `question` equals a non-empty suggestion in the suggestion comparison form: a
    follow-up stored on a turn the history read returned, or a project's example question. There
    is no fuzzy matching: a reordered or edited question is the user's own."""
    asked = suggestion_form(question)
    return any((form := suggestion_form(item)) and form == asked for item in suggestions)
