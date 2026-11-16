"""The model capability table, `MODEL_CAPABILITIES` (`12-chat-pipeline.md` section 10.7).

Keyed by model name, each row states what that model accepts: streaming Chat Completions, strict
structured outputs, the `prompt_cache_options` parameter and its reasoning efforts. Three readers
apply it: the API's start-up checks (a model absent from the table, an effort outside its row,
or a disabled prompt cache for a model without `prompt_cache_options`), the ops-config models of
`summary.reasoning_effort` and `memory.reasoning_effort` against the chat-small model's row, and
`docchat-ops chat model-check`. A model that a deployment refuses something for loses that value
here, in code, so it fails at start-up or at the stage gate rather than on a user's turn; a new
model needs its row first.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal, get_args

type ReasoningEffort = Literal["none", "low", "medium", "high"]
"""Every `reasoning_effort` value a DocChat setting can hold."""

REASONING_EFFORTS: Final[tuple[ReasoningEffort, ...]] = get_args(ReasoningEffort.__value__)


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelCapability:
    """One row of the capability table."""

    streaming_chat_completions: bool
    strict_structured_outputs: bool
    prompt_cache_options: bool
    """Accepts `prompt_cache_options`, so its `*_PROMPT_CACHE` setting may be `disabled`."""
    reasoning_efforts: frozenset[ReasoningEffort]


_GPT_5_6: Final = ModelCapability(
    streaming_chat_completions=True,
    strict_structured_outputs=True,
    prompt_cache_options=True,
    reasoning_efforts=frozenset(REASONING_EFFORTS),
)

MODEL_CAPABILITIES: Final[Mapping[str, ModelCapability]] = MappingProxyType(
    {
        "gpt-5.6-sol": _GPT_5_6,
        "gpt-5.6-terra": _GPT_5_6,
    }
)
"""The supported chat models and what each accepts."""
