from __future__ import annotations

import sys
import threading
from collections.abc import Sequence
from types import SimpleNamespace

import pytest

from docchat_api.tokens import (
    INLINE_COUNT_MAX_CHARACTERS,
    TiktokenTokenizer,
    count_sync,
    count_tokens,
)
from docchat_core.ingestion.settings import TokenEncoding


class WordTokenizer:
    """One token per whitespace-separated word; records the thread of each call."""

    def __init__(self) -> None:
        self.threads: list[int] = []

    def encode(self, text: str) -> Sequence[int]:
        self.threads.append(threading.get_ident())
        return [0] * len(text.split())


def test_each_text_counts_on_its_own() -> None:
    assert count_sync(WordTokenizer(), ["two words", "", "three more words"]) == 5


async def test_short_texts_count_on_the_loop() -> None:
    tokenizer = WordTokenizer()

    assert await count_tokens(tokenizer, "a b", "c") == 3
    assert set(tokenizer.threads) == {threading.get_ident()}


async def test_20000_characters_count_in_a_worker_thread() -> None:
    tokenizer = WordTokenizer()
    half = "x " * (INLINE_COUNT_MAX_CHARACTERS // 4)

    assert await count_tokens(tokenizer, half, half) == INLINE_COUNT_MAX_CHARACTERS // 2
    assert threading.get_ident() not in tokenizer.threads


async def test_19999_characters_still_count_inline() -> None:
    tokenizer = WordTokenizer()

    await count_tokens(tokenizer, "x" * (INLINE_COUNT_MAX_CHARACTERS - 1))

    assert tokenizer.threads == [threading.get_ident()]


def test_the_tiktoken_encoding_loads_once_and_counts_special_text_as_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded: list[str] = []

    class Encoding:
        def encode_ordinary(self, text: str) -> list[int]:
            return [1] * len(text)

    def get_encoding(name: str) -> Encoding:
        loaded.append(name)
        return Encoding()

    monkeypatch.setitem(sys.modules, "tiktoken", SimpleNamespace(get_encoding=get_encoding))
    tokenizer = TiktokenTokenizer(TokenEncoding.O200K_BASE)

    assert loaded == []
    assert len(tokenizer.encode("<|endoftext|>")) == 13
    assert len(tokenizer.encode("ab")) == 2
    assert loaded == ["o200k_base"]
