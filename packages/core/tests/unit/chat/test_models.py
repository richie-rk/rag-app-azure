"""The model capability table (`12-chat-pipeline.md` section 10.7), read against the table of the
specification itself, and its use by the ops-config models (T-249's ops-config half; the start-up
half is the API's settings test)."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace

import pytest
from pydantic import JsonValue, ValidationError

from docchat_core.chat.models import (
    MODEL_CAPABILITIES,
    REASONING_EFFORTS,
    ModelCapability,
)
from docchat_core.opsconfig import validate_area

type SpecTable = Callable[[str, str], list[list[str]]]

HEADING = "### 10.7 Model capability table, deployed model check and tokenizer check"
_CODE_SPAN = re.compile(r"`([^`]+)`")

MEMORY: dict[str, JsonValue] = {
    "memory.enabled": True,
    "memory.max_facts": 50,
    "memory.max_tokens": 1000,
    "memory.prompt_max_tokens": 1300,
    "memory.max_operations_per_turn": 3,
    "memory.max_completion_tokens": 1200,
    "memory.reasoning_effort": "none",
    "memory.timeout_seconds": 20,
    "memory.lease_seconds": 60,
    "memory.max_attempts": 3,
    "memory.retry_window_hours": 24,
}


def _yes(cell: str) -> bool:
    assert cell in {"yes", "no"}
    return cell == "yes"


def test_the_table_equals_the_specification(spec_table: SpecTable) -> None:
    rows = spec_table("12-chat-pipeline.md", HEADING)
    expected = {
        model: ModelCapability(
            streaming_chat_completions=_yes(streaming),
            strict_structured_outputs=_yes(structured),
            prompt_cache_options=_yes(cache),
            reasoning_efforts=frozenset(_CODE_SPAN.findall(efforts)),
        )
        for model, streaming, structured, cache, efforts in rows
    }
    assert dict(MODEL_CAPABILITIES) == expected
    assert set(MODEL_CAPABILITIES) == {"gpt-5.6-sol", "gpt-5.6-terra"}


def test_the_efforts_a_setting_can_hold() -> None:
    assert REASONING_EFFORTS == ("none", "low", "medium", "high")
    for row in MODEL_CAPABILITIES.values():
        assert row.reasoning_efforts <= set(REASONING_EFFORTS)


def test_the_table_is_read_only() -> None:
    with pytest.raises(TypeError):
        MODEL_CAPABILITIES["gpt-4o"] = MODEL_CAPABILITIES["gpt-5.6-sol"]  # type: ignore[index]


def test_t249_the_ops_config_model_follows_the_chat_small_row() -> None:
    row = MODEL_CAPABILITIES["gpt-5.6-terra"]
    validate_area("memory", MEMORY, chat_small_efforts=row.reasoning_efforts)

    without_none = replace(row, reasoning_efforts=row.reasoning_efforts - {"none"})

    with pytest.raises(ValidationError):
        validate_area("memory", MEMORY, chat_small_efforts=without_none.reasoning_efforts)
    validate_area(
        "memory",
        {**MEMORY, "memory.reasoning_effort": "low"},
        chat_small_efforts=without_none.reasoning_efforts,
    )
