"""The public models of the chat path: the request `ChatTurnRequest` and the two shapes the stream
and the session reads share, `Citation` and `TurnUsageSummary` (`12-chat-pipeline.md` sections 2.1,
11.3 and 14.2; `11-api.md` sections 3.3.1 and 4.5).

The question is normalised before it is validated (CR LF and lone CR to LF, NFC, trimmed), and
the normalised text is the question everywhere afterwards. Its three rules give one `errors` entry
at `body.question`, in order: `invalid_characters` (a Cc code point other than LF and TAB, or a
lone surrogate), `empty` (no code point of category L, N, P or S, so a question of only spaces,
format characters or combining marks never reaches a model), `too_long` (above 4,000 code
points). Format characters are accepted inside a question that passes the second rule.
"""

from __future__ import annotations

import unicodedata
from typing import Annotated, Any, Final, Literal

from pydantic import BeforeValidator, Field, StringConstraints
from pydantic_core import PydanticCustomError

from docchat_api.http.models import RequestModel, ResponseModel
from docchat_core.cosmos.conversations import CitationContentType, CitationView, UsageTotals
from docchat_core.models import CanonicalUuid, Count

QUESTION_MAX_CODE_POINTS: Final = 4_000
CITATION_NUMBER_MAX: Final = 16
CITATION_TITLE_MAX: Final = 300
CITATION_PAGE_MAX: Final = 100_000
HEADING_PATH_MAX_ITEMS: Final = 8
HEADING_MAX: Final = 200

_KEPT_CONTROLS: Final = frozenset("\n\t")
_CONTENT_CATEGORIES: Final = frozenset("LNPS")

REQUEST_EXAMPLE: Final[dict[str, Any]] = {
    "projectId": "3f2b8c1e-7d4a-4e9b-9c55-0a6f1d2e8b74",
    "sessionId": None,
    "clientTurnId": "b8e0d6a2-51c3-4f7e-8a19-6c2d4e9f0a13",
    "question": "How many days do I have to submit an expense claim?",
}
"""The OpenAPI request example of section 2.1, from which generated tests start."""


def normalise_question(text: str) -> str:
    """CR LF and lone CR become LF, then NFC, then leading and trailing whitespace removed."""
    unified = text.replace("\r\n", "\n").replace("\r", "\n")
    return unicodedata.normalize("NFC", unified).strip()


def _question(value: object) -> object:
    # A value that isn't a string is left to the string schema, which reports its type.
    if not isinstance(value, str):
        return value
    question = normalise_question(value)
    for character in question:
        category = unicodedata.category(character)
        if (category == "Cc" and character not in _KEPT_CONTROLS) or category == "Cs":
            raise PydanticCustomError(
                "invalid_characters", "The question holds a control character or a surrogate"
            )
    if not any(unicodedata.category(character)[0] in _CONTENT_CATEGORIES for character in question):
        raise PydanticCustomError("empty", "The question holds no letter, number or symbol")
    if len(question) > QUESTION_MAX_CODE_POINTS:
        # Pydantic's own `too_long` is a list's; the string form maps to the reason `too_long`.
        raise PydanticCustomError("string_too_long", "The question is above 4,000 code points")
    return question


Question = Annotated[
    str,
    BeforeValidator(_question),
    StringConstraints(min_length=1, max_length=QUESTION_MAX_CODE_POINTS),
]
"""A normalised question of 1 to 4,000 code points."""


class ChatTurnRequest(RequestModel):
    """`POST /v1/chat/turns`: exactly four members; no index, key, deployment, model, prompt,
    option or history is accepted from the caller."""

    project_id: CanonicalUuid
    session_id: CanonicalUuid | None = None
    client_turn_id: CanonicalUuid
    question: Question


class Citation(ResponseModel):
    """The one public citation shape: the `citations` event, replays and session reads alike.
    It never carries a document, version, chunk-set or index identifier, a chunk key or a path."""

    number: Annotated[int, Field(ge=1, le=CITATION_NUMBER_MAX)]
    title: Annotated[str, Field(min_length=1, max_length=CITATION_TITLE_MAX)]
    page_number: Annotated[int, Field(ge=1, le=CITATION_PAGE_MAX)]
    heading_path: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=HEADING_MAX)]],
        Field(max_length=HEADING_PATH_MAX_ITEMS),
    ]
    content_type: CitationContentType
    open_as: Literal["pdf_page"]

    @classmethod
    def of(cls, view: CitationView) -> Citation:
        """The public projection of a stored citation."""
        return cls(
            number=view.number,
            title=view.title,
            page_number=view.page_number,
            heading_path=list(view.heading_path),
            content_type=view.content_type,
            open_as=view.open_as,
        )


class TurnUsageSummary(ResponseModel):
    """The turn's token totals as the client sees them: no cost, deployment or per-call
    record."""

    prompt_tokens: Count
    completion_tokens: Count
    reasoning_tokens: Count
    cached_tokens: Count
    total_tokens: Count
    estimated: bool

    @classmethod
    def of(cls, totals: UsageTotals) -> TurnUsageSummary:
        return cls(
            prompt_tokens=totals.prompt_tokens,
            completion_tokens=totals.completion_tokens,
            reasoning_tokens=totals.reasoning_tokens,
            cached_tokens=totals.cached_tokens,
            total_tokens=totals.total_tokens,
            estimated=totals.estimated,
        )
