"""`POST /contentsafety/text:shieldPrompt` for local and CI runs (84-testing.md section 2.3).

The stub enforces the service's limits for API version 2024-09-01: at most 5 documents and at
most 10,000 characters in one call. Characters are counted in UTF-16 code units, the count the
chat pipeline packs by, because it is never below the code-point count: a packing that only
holds under the looser count still fails here.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from docchat_model_stub.directives import marks_attack

MAX_DOCUMENTS = 5
MAX_CHARACTERS = 10_000
_UTF16_UNIT_BYTES = 2


class ShieldLimitError(ValueError):
    """A call over the service's per-call limits; answered `400 InvalidRequestBody`."""


class ShieldRequest(BaseModel):
    """The fields the stub reads; absent fields count as empty."""

    model_config = ConfigDict(extra="ignore", strict=True)

    user_prompt: StrictStr = Field(default="", alias="userPrompt")
    documents: list[StrictStr] = Field(default_factory=list)

    def texts(self) -> list[str]:
        return [self.user_prompt, *self.documents]


def _utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // _UTF16_UNIT_BYTES


def check_limits(request: ShieldRequest) -> None:
    """Raise `ShieldLimitError` when the call breaks a per-call limit."""
    if len(request.documents) > MAX_DOCUMENTS:
        raise ShieldLimitError(f"at most {MAX_DOCUMENTS} documents are allowed per call")
    if sum(_utf16_units(text) for text in request.texts()) > MAX_CHARACTERS:
        raise ShieldLimitError(f"at most {MAX_CHARACTERS} characters are allowed per call")


def analysis(request: ShieldRequest) -> dict[str, Any]:
    """The `200` body: an entry is an attack only when its own text carries the directive."""
    return {
        "userPromptAnalysis": {"attackDetected": marks_attack(request.user_prompt)},
        "documentsAnalysis": [
            {"attackDetected": marks_attack(document)} for document in request.documents
        ],
    }
