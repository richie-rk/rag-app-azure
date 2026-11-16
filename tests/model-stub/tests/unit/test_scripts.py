from __future__ import annotations

import pytest
from pydantic import ValidationError

from docchat_model_stub.scripts import (
    SHIELD_PROMPT,
    ScriptBody,
    ScriptStore,
    validate_responses,
)


def test_responses_are_consumed_in_order() -> None:
    store = ScriptStore()
    store.put("standalone_query", [{"query": "a"}, {"query": "b"}])
    assert store.pop("standalone_query") == (True, {"query": "a"})
    assert store.pop("standalone_query") == (True, {"query": "b"})
    assert store.pop("standalone_query") == (False, None)


def test_put_replaces_a_queue_and_clear_empties_all() -> None:
    store = ScriptStore()
    store.put("a", [1, 2])
    store.put("a", [3])
    store.put("b", [4])
    assert store.pop("a") == (True, 3)
    store.clear()
    assert store.pop("b") == (False, None)
    assert store.pop("never") == (False, None)


def test_script_body_needs_a_responses_list() -> None:
    assert ScriptBody.model_validate({"responses": [{"x": 1}, "text"]}).responses == [
        {"x": 1},
        "text",
    ]
    with pytest.raises(ValidationError):
        ScriptBody.model_validate({"responses": {"x": 1}})
    with pytest.raises(ValidationError):
        ScriptBody.model_validate({"responses": [], "extra": 1})


def test_shield_prompt_responses_are_checked() -> None:
    validate_responses(
        SHIELD_PROMPT,
        [
            {"userPromptAnalysis": {"attackDetected": True}, "documentsAnalysis": []},
            {"status": 503, "error": {"code": "ServiceUnavailable", "message": "busy"}},
        ],
    )


@pytest.mark.parametrize(
    "response",
    [
        "not an object",
        {"status": 200, "error": {"code": "x", "message": "y"}},
        {"status": "503", "error": {"code": "x", "message": "y"}},
        {"status": 503, "error": {"code": "x"}},
        {"status": 503},
    ],
)
def test_malformed_shield_prompt_responses_are_refused(response: object) -> None:
    with pytest.raises(ValidationError):
        validate_responses(SHIELD_PROMPT, [response])


def test_other_scripts_take_any_value() -> None:
    validate_responses("standalone_query", ["not json", 3, None])
