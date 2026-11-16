"""The text types of the administrator inputs (`11-api.md` section 1.7 "Text types";
`82-security.md` SEC-24).

A text one administrator enters appears on other people's screens, so it can't hide or reorder
characters there: both types apply Unicode NFC and refuse every code point of category Cc, Cf,
Cs, Co or Cn with the reason `invalid_characters` (the bidirectional controls, the zero-width
characters and the rest of category Cf among them; the accepted cost is that a text can't hold the
joiners U+200C and U+200D). Lengths are counted in code points after normalisation, with the
reasons `too_short` and `too_long`.

- `SingleLineText(min, max)`: leading and trailing whitespace removed, every run of whitespace
  (tabs and line breaks included) collapsed to one space.
- `MultiLineText(min, max)`: CR LF and lone CR turned into LF, the whole text trimmed, LF and TAB
  kept.

Use them as metadata: `name: Annotated[str, SingleLineText(1, 80)]`.
"""

from __future__ import annotations

import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Final, override

from pydantic import GetCoreSchemaHandler
from pydantic_core import PydanticCustomError, core_schema

REFUSED_CATEGORIES: Final = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})
MULTI_LINE_KEPT: Final = frozenset("\n\t")
# Python counts the information separators U+001C to U+001F as whitespace; they are controls,
# which the types refuse rather than collapse or trim.
_NOT_WHITESPACE: Final = frozenset("\x1c\x1d\x1e\x1f")


def _is_whitespace(char: str) -> bool:
    return char.isspace() and char not in _NOT_WHITESPACE


def _trimmed(text: str) -> str:
    start, end = 0, len(text)
    while start < end and _is_whitespace(text[start]):
        start += 1
    while end > start and _is_whitespace(text[end - 1]):
        end -= 1
    return text[start:end]


def _refuse(text: str, kept: frozenset[str] = frozenset()) -> str:
    for char in text:
        if char not in kept and unicodedata.category(char) in REFUSED_CATEGORIES:
            raise PydanticCustomError(
                "invalid_characters", "The text holds a control, format or unassigned character"
            )
    return text


def single_line(text: str) -> str:
    """NFC, whitespace runs collapsed to one space and trimmed, refused categories refused."""
    normalised = unicodedata.normalize("NFC", text)
    words: list[str] = []
    word: list[str] = []
    for char in normalised:
        if _is_whitespace(char):
            if word:
                words.append("".join(word))
                word = []
        else:
            word.append(char)
    if word:
        words.append("".join(word))
    return _refuse(" ".join(words))


def multi_line(text: str) -> str:
    """NFC, CR LF and CR to LF, trimmed, refused categories refused except LF and TAB."""
    normalised = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    return _refuse(_trimmed(normalised), MULTI_LINE_KEPT)


@dataclass(frozen=True, slots=True)
class _TextType(ABC):
    min_length: int
    max_length: int

    def __post_init__(self) -> None:
        if not 0 <= self.min_length <= self.max_length:
            raise ValueError("a text type's bounds satisfy 0 <= min <= max")

    @staticmethod
    @abstractmethod
    def normalise(text: str) -> str: ...

    def __get_pydantic_core_schema__(
        self, source: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        normalise = self.normalise

        def validate(value: object) -> object:
            # A value that isn't a string is left to the string schema, which reports its type.
            return normalise(value) if isinstance(value, str) else value

        return core_schema.no_info_before_validator_function(
            validate,
            core_schema.str_schema(min_length=self.min_length, max_length=self.max_length),
        )


class SingleLineText(_TextType):
    """Single-line administrator text of `min_length` to `max_length` code points."""

    @staticmethod
    @override
    def normalise(text: str) -> str:
        return single_line(text)


class MultiLineText(_TextType):
    """Multi-line administrator text of `min_length` to `max_length` code points."""

    @staticmethod
    @override
    def normalise(text: str) -> str:
        return multi_line(text)
