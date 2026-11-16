"""Token counting in the API (`12-chat-pipeline.md` section 9.9; `13-conversations-and-memory.md`
section 1.1 rule 3).

Every token count the API makes follows one rule: a count over texts that together hold fewer than
20,000 characters runs inline on the event loop, a larger one in one `asyncio.to_thread` call, so a
long text never holds the loop. `TiktokenTokenizer` is a deployment's configured encoding
(`DOCCHAT_OPENAI_CHAT_ENCODING`, `DOCCHAT_OPENAI_SMALL_ENCODING`); it loads on first use from the
image's `TIKTOKEN_CACHE_DIR`, which `gunicorn.conf.py` has already read in the master before the
fork, so a worker's first count finds the encoding in tiktoken's own cache.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Sequence
from typing import TYPE_CHECKING, Final, Protocol

if TYPE_CHECKING:
    from tiktoken import Encoding

    from docchat_core.ingestion.settings import TokenEncoding

INLINE_COUNT_MAX_CHARACTERS: Final = 20_000
"""Texts holding together fewer characters than this are counted on the event loop."""


class Tokenizer(Protocol):
    """The one operation a count needs of an encoding."""

    def encode(self, text: str) -> Sequence[int]: ...


class TiktokenTokenizer:
    """A `tiktoken` encoding that treats special-token text as ordinary text, so a question or an
    answer that contains `<|endoftext|>` is counted, never refused."""

    def __init__(self, encoding: TokenEncoding) -> None:
        self._name = encoding.value
        self._encoding: Encoding | None = None
        self._lock = threading.Lock()

    def _loaded(self) -> Encoding:
        encoding = self._encoding
        if encoding is None:
            # Counts run on the loop and in worker threads, so the first load is serialised.
            with self._lock:
                encoding = self._encoding
                if encoding is None:
                    import tiktoken  # noqa: PLC0415 - read at the first count, not at import

                    encoding = self._encoding = tiktoken.get_encoding(self._name)
        return encoding

    def encode(self, text: str) -> Sequence[int]:
        return self._loaded().encode_ordinary(text)

    def decode(self, tokens: Sequence[int]) -> str:
        """The text of `tokens`; a character split at either end decodes as U+FFFD."""
        return self._loaded().decode(list(tokens))


def count_sync(tokenizer: Tokenizer, texts: Sequence[str]) -> int:
    """The tokens of each text, summed; an empty text counts 0."""
    return sum(len(tokenizer.encode(text)) for text in texts if text)


async def count_tokens(tokenizer: Tokenizer, *texts: str) -> int:
    """The tokens of `texts`, each counted on its own and summed, under the 20,000-character
    rule."""
    if sum(len(text) for text in texts) < INLINE_COUNT_MAX_CHARACTERS:
        return count_sync(tokenizer, texts)
    return await asyncio.to_thread(count_sync, tokenizer, texts)
