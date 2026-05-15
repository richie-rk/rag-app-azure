"""Batched embedding via Azure OpenAI, with exponential backoff on rate limits."""

import logging
import time

from openai import RateLimitError

from services.shared.azure_clients import get_openai_client
from services.shared.config import get_settings

logger = logging.getLogger(__name__)

_BATCH_SIZE = 100  # OpenAI embeddings API batch limit
_MAX_RETRIES = 5
_RETRY_BASE_DELAY = 2.0


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts, batching and retrying on rate limits.

    Returns list of embedding vectors (list[float]) in same order as input.
    """
    settings = get_settings()
    client = get_openai_client()
    all_embeddings: list[list[float]] = []

    for batch_start in range(0, len(texts), _BATCH_SIZE):
        batch = texts[batch_start : batch_start + _BATCH_SIZE]
        embeddings = _embed_batch_with_retry(client, batch, settings.embedding_deployment)
        all_embeddings.extend(embeddings)

    return all_embeddings


def _embed_batch_with_retry(
    client, texts: list[str], deployment: str
) -> list[list[float]]:
    """Embed a single batch with exponential backoff retry."""
    for attempt in range(_MAX_RETRIES):
        try:
            response = client.embeddings.create(
                input=texts,
                model=deployment,
            )
            return [item.embedding for item in response.data]
        except RateLimitError:
            delay = _RETRY_BASE_DELAY * (2 ** attempt)
            logger.warning(
                "Rate limited on embedding batch, retrying in %.1fs (attempt %d/%d)",
                delay, attempt + 1, _MAX_RETRIES,
            )
            time.sleep(delay)

    raise RuntimeError(f"Failed to embed batch after {_MAX_RETRIES} retries")
