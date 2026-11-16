"""The fact categories, a closed set and part of the API contract (`13-conversations-and-memory.md`
section 7.2).

Each category has a label, the word the memory lines of the answer prompt start with, and a
place in the order that the memory screen and `get_prompt_memory` sort facts by.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final


class FactCategory(StrEnum):
    """What a remembered fact is about."""

    ROLE = "role"
    """The user's job title or function."""
    TEAM = "team"
    """The team, department or business unit the user belongs to."""
    EXPERTISE = "expertise"
    """How familiar the user says they are with a subject."""
    TOPIC = "topic"
    """A subject the user says they work on regularly."""
    ANSWER_STYLE = "answer_style"
    """How the user wants answers written: length, format, tone or language."""


CATEGORY_ORDER: Final[tuple[FactCategory, ...]] = tuple(FactCategory)
"""`role`, `team`, `expertise`, `topic`, `answer_style`: the order facts are listed in."""

CATEGORY_LABELS: Final[Mapping[FactCategory, str]] = MappingProxyType(
    {
        FactCategory.ROLE: "Role",
        FactCategory.TEAM: "Team",
        FactCategory.EXPERTISE: "Expertise",
        FactCategory.TOPIC: "Recurring topic",
        FactCategory.ANSWER_STYLE: "Answer preference",
    }
)
"""Each category's label in a memory line `{label}: {text}`."""
