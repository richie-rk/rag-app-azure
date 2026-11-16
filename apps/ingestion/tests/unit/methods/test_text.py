"""Unit tests of token counting, sentence boundaries and packing (`14-ingestion.md` sections
10.7, 11.4 and 12.4)."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from types import SimpleNamespace

import pytest

from docchat_core.ingestion.settings import TokenEncoding
from docchat_ingestion.methods import text
from docchat_ingestion.methods.text import (
    Piece,
    TiktokenTokenizer,
    Tokenizer,
    count_tokens,
    joined,
    last_tokens,
    pack,
    sentences,
    token_slices,
)


def test_sentences_split_after_latin_and_cjk_marks() -> None:
    cjk = "\u56db\u3002\u4e94\uff01\u516d\uff1f"
    sample = f"One. Two!  Three? {cjk} seven"

    assert sentences(sample) == [
        (0, "One."),
        (5, "Two!"),
        (11, "Three?"),
        (18, cjk),
        (25, "seven"),
    ]
    assert sentences("") == []
    assert sentences("No boundary here") == [(0, "No boundary here")]
    assert sentences("Ends. ") == [(0, "Ends.")]


def test_the_cjk_marks_split_only_before_whitespace() -> None:
    one, stop, two = "\u4e00", "\u3002", "\u4e8c"
    assert [sentence for _, sentence in sentences(f"{one}{stop} {two}")] == [one + stop, two]
    assert sentences(f"{one}{stop}{two}") == [(0, one + stop + two)]


def test_counting_and_slicing(tokenizer: Tokenizer) -> None:
    sample = "alpha beta gamma delta"

    assert count_tokens(tokenizer, sample) == 7
    assert count_tokens(tokenizer, "") == 0
    slices = token_slices(tokenizer, sample, 3)
    assert slices == ["alpha beta", " gamma ", "delta"]
    assert "".join(slices) == sample
    assert last_tokens(tokenizer, sample, 3) == "gamma delta"
    assert last_tokens(tokenizer, sample, 0) == ""
    with pytest.raises(ValueError, match="at least one token"):
        token_slices(tokenizer, sample, 0)


def _pieces(tokenizer: Tokenizer, *texts: str) -> list[Piece]:
    return [Piece(item, count_tokens(tokenizer, item)) for item in texts]


def test_pack_fills_groups_up_to_the_limit(tokenizer: Tokenizer) -> None:
    pieces = _pieces(tokenizer, "a b", "c d", "e f", "g")

    groups = pack(pieces, 7, tokenizer)

    assert [joined(group) for group in groups] == ["a b\n\nc d", "e f\n\ng"]
    assert all(count_tokens(tokenizer, joined(group)) <= 7 for group in groups)
    assert joined([]) == ""


def test_pack_keeps_an_oversize_piece_on_its_own(tokenizer: Tokenizer) -> None:
    pieces = _pieces(tokenizer, "a", "b c d e f g h", "i")

    assert [joined(group) for group in pack(pieces, 3, tokenizer)] == ["a", "b c d e f g h", "i"]


def test_pack_takes_a_first_limit(tokenizer: Tokenizer) -> None:
    pieces = _pieces(tokenizer, "a", "b", "c", "d")

    groups = pack(pieces, 3, tokenizer, first_limit=1)

    assert [joined(group) for group in groups] == ["a", "b\n\nc", "d"]


class _MergingTokenizer:
    """Counts a blank line between two words as three tokens, so the sum of the parts
    underestimates the joined text."""

    def encode(self, value: str) -> list[int]:
        return [0] * (len(value.split()) + 2 * value.count("\n\n"))

    def decode(self, tokens: Sequence[int]) -> str:
        return ""


def test_pack_moves_trailing_pieces_when_the_join_counts_more() -> None:
    tokenizer = _MergingTokenizer()
    pieces = [Piece("a", 1), Piece("b", 1), Piece("c", 1)]

    groups = pack(pieces, 4, tokenizer)

    assert [[piece.text for piece in group] for group in groups] == [["a", "b"], ["c"]]
    tight = pack([Piece("a", 1), Piece("b", 1)], 2, tokenizer)
    assert [[piece.text for piece in group] for group in tight] == [["a"], ["b"]]


def test_the_tiktoken_tokenizer_counts_special_token_text(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, object]] = []

    class Encoding:
        def encode(self, value: str, *, disallowed_special: object) -> list[int]:
            calls.append((value, disallowed_special))
            return [1, 2]

        def decode(self, tokens: list[int]) -> str:
            return f"decoded {tokens}"

    monkeypatch.setitem(
        sys.modules, "tiktoken", SimpleNamespace(get_encoding=lambda name: Encoding())
    )
    tokenizer = TiktokenTokenizer(TokenEncoding.CL100K_BASE)

    assert tokenizer.encode("<|endoftext|>") == [1, 2]
    assert calls == [("<|endoftext|>", ())]
    assert tokenizer.decode((3, 4)) == "decoded [3, 4]"


def test_load_tokenizer_keeps_one_per_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    loaded: list[str] = []
    monkeypatch.setitem(sys.modules, "tiktoken", SimpleNamespace(get_encoding=loaded.append))
    text.load_tokenizer.cache_clear()
    try:
        first = text.load_tokenizer(TokenEncoding.O200K_BASE)
        assert text.load_tokenizer(TokenEncoding.O200K_BASE) is first
        assert loaded == ["o200k_base"]
    finally:
        text.load_tokenizer.cache_clear()
