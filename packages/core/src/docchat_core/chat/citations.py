"""Citation tokens and `strip_citation_tokens` (`12-chat-pipeline.md` sections 11.1 and 11.6).

The pipeline turns each marker the model writes (`[S2]`) into reserved citation tokens `⟦n⟧`,
`n` from 1 to 16, numbered by first use across the answer; only the API mints them, because it
deletes U+27E6 and U+27E7 from the model's text before parsing. A token's number points at a
source of its own turn only, so every text that leaves its turn (the history the conversation
store returns, the follow-ups call's answer input, the summary input, `docchat-ops user export`)
passes through `strip_citation_tokens` first.
"""

from __future__ import annotations

import re
from typing import Final

CITATION_TOKEN_OPEN: Final = "\N{MATHEMATICAL LEFT WHITE SQUARE BRACKET}"  # noqa: S105 - a character
"""`⟦`, reserved: deleted from model text, so only the API writes it."""
CITATION_TOKEN_CLOSE: Final = "\N{MATHEMATICAL RIGHT WHITE SQUARE BRACKET}"  # noqa: S105 - a character
"""`⟧`, reserved like `CITATION_TOKEN_OPEN`."""
CITATION_NUMBER_MAX: Final = 16

_TOKEN_AND_SPACE: Final = re.compile(
    f" ?{CITATION_TOKEN_OPEN}(?:1[0-6]|[1-9]){CITATION_TOKEN_CLOSE}"
)


def strip_citation_tokens(text: str) -> str:
    """`text` without its citation tokens, each removed with the single space directly before it
    when there is one: "sites ⟦1⟧, with exceptions ⟦2⟧." becomes "sites, with exceptions."."""
    return _TOKEN_AND_SPACE.sub("", text)
