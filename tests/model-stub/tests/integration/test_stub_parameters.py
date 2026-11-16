"""T-577: the stub parameter tests, over a real connection to the stub."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractAsyncContextManager

    import httpx

    type StartStub = Callable[..., AbstractAsyncContextManager[str]]
    type ClientFor = Callable[[str], httpx.AsyncClient]

CHAT = "/openai/v1/chat/completions"


def _chat(**fields: Any) -> dict[str, Any]:
    return {
        "model": "chat-small",
        "messages": [{"role": "user", "content": "question"}],
        "max_completion_tokens": 800,
        **fields,
    }


@pytest.mark.parametrize(("name", "value"), [("temperature", 0.2), ("max_tokens", 100)])
async def test_t577_sampling_and_token_parameters_are_refused(
    start_stub: StartStub, client_for: ClientFor, name: str, value: object
) -> None:
    async with start_stub() as base_url, client_for(base_url) as client:
        response = await client.post(CHAT, json=_chat(reasoning_effort="none", **{name: value}))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_parameter"
    assert response.json()["error"]["param"] == name


async def test_t577_the_four_efforts_are_accepted(
    start_stub: StartStub, client_for: ClientFor
) -> None:
    async with start_stub() as base_url, client_for(base_url) as client:
        statuses = [
            (await client.post(CHAT, json=_chat(reasoning_effort=effort))).status_code
            for effort in ("none", "low", "medium", "high")
        ]
        without = await client.post(CHAT, json=_chat())
    assert statuses == [200, 200, 200, 200]
    assert without.status_code == 200


@pytest.mark.parametrize("effort", ["minimal", "xhigh", "max", "LOW"])
async def test_t577_other_efforts_are_unsupported_values(
    start_stub: StartStub, client_for: ClientFor, effort: str
) -> None:
    async with start_stub() as base_url, client_for(base_url) as client:
        response = await client.post(CHAT, json=_chat(reasoning_effort=effort))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_value"
    assert response.json()["error"]["param"] == "reasoning_effort"
