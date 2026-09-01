"""Streaming chat completion against Azure OpenAI."""

import logging
from collections.abc import AsyncIterator

from openai import BadRequestError

from services.shared.azure_clients import get_openai_client
from services.shared.config import get_settings

logger = logging.getLogger(__name__)


async def stream_completion(
    messages: list[dict[str, str]],
    deployment: str | None = None,
    temperature: float = 0.0,
    max_tokens: int = 2500,
) -> AsyncIterator[str]:
    """Stream chat completion tokens from Azure OpenAI.

    Yields content delta strings. On context overflow (BadRequestError),
    truncates history and retries once.
    """
    settings = get_settings()
    client = get_openai_client()
    model = deployment or settings.default_llm_deployment

    yielded_any = False
    try:
        async for token in _do_stream(client, model, messages, temperature, max_tokens):
            yielded_any = True
            yield token
    except BadRequestError as exc:
        if yielded_any:
            # Tokens already reached the client; retrying from scratch would
            # replay the answer and duplicate text. Let the stream wrapper
            # surface this as an error chunk instead.
            raise
        logger.warning("BadRequestError (likely context overflow): %s - truncating and retrying", exc)
        truncated = _truncate_messages(messages)
        async for token in _do_stream(client, model, truncated, temperature, max_tokens):
            yield token


async def _do_stream(
    client,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
) -> AsyncIterator[str]:
    """Perform the actual streaming call."""
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=True,
    )
    for chunk in response:
        if chunk.choices and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content


def _truncate_messages(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """Keep system prompt + last 2 user/assistant turns + current query."""
    if len(messages) <= 3:
        return messages

    system = messages[0]
    current_query = messages[-1]

    # Keep the last 2 pairs of conversation before current query
    history = messages[1:-1]
    kept = history[-4:] if len(history) > 4 else history

    return [system] + kept + [current_query]
