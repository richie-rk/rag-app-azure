"""`strip_citation_tokens` (T-257; `12-chat-pipeline.md` sections 11.1 and 11.6)."""

from __future__ import annotations

import pytest

from docchat_core.chat.citations import (
    CITATION_NUMBER_MAX,
    CITATION_TOKEN_CLOSE,
    CITATION_TOKEN_OPEN,
    strip_citation_tokens,
)


def test_t257_tokens_go_with_the_space_before_them() -> None:
    assert (
        strip_citation_tokens("Claims close in 30 days ⟦1⟧, with receipts ⟦2⟧.")
        == "Claims close in 30 days, with receipts."
    )


def test_the_section_example() -> None:
    assert (
        strip_citation_tokens("The policy covers all sites ⟦1⟧, with two exceptions ⟦2⟧.")
        == "The policy covers all sites, with two exceptions."
    )


def test_the_reserved_characters() -> None:
    assert (CITATION_TOKEN_OPEN, CITATION_TOKEN_CLOSE) == ("⟦", "⟧")
    assert CITATION_NUMBER_MAX == 16


@pytest.mark.parametrize(
    ("text", "stripped"),
    [
        ("Adjacent ⟦1⟧⟦2⟧ tokens.", "Adjacent tokens."),
        ("⟦3⟧ at the start", " at the start"),
        ("No space before⟦16⟧.", "No space before."),
        ("Only one space goes  ⟦4⟧.", "Only one space goes ."),
        ("A line break stays\n⟦5⟧.", "A line break stays\n."),
        ("", ""),
        ("Plain text without tokens.", "Plain text without tokens."),
    ],
)
def test_every_token_goes_with_at_most_one_space(text: str, stripped: str) -> None:
    assert strip_citation_tokens(text) == stripped


@pytest.mark.parametrize("text", ["⟦0⟧", "⟦17⟧", "⟦01⟧", "⟦x⟧", "[1]", "⟦1"])
def test_only_tokens_the_api_can_mint_are_removed(text: str) -> None:
    assert strip_citation_tokens(f"a {text}") == f"a {text}"


def test_every_number_from_1_to_16_is_a_token() -> None:
    text = "".join(f" ⟦{number}⟧" for number in range(1, CITATION_NUMBER_MAX + 1))
    assert strip_citation_tokens(f"x{text}.") == "x."
