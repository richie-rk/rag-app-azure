"""Follow-up validation (T-258; `12-chat-pipeline.md` section 13.3)."""

from __future__ import annotations

import pytest

from docchat_api.chat.followups import holds_link, validate_followups

QUESTION = "What is the receipt limit?"


@pytest.mark.parametrize(
    "item",
    [
        "see https://evil.example/p",
        "see //evil.example/p",
        "see WWW.evil.example",
        "https:\u200b//evil.example/p",
        "ww\u00adw.evil.example",
    ],
)
def test_t258_url_bearing_items_are_dropped(item: str) -> None:
    assert holds_link(item)
    assert validate_followups([item], QUESTION, 3) == []


@pytest.mark.parametrize("item", ["What does \\\\host\\share hold?", "Ask name@example.net?"])
def test_t258_unc_paths_and_email_addresses_are_kept(item: str) -> None:
    assert validate_followups([item], QUESTION, 3) == [item]


def test_t258_empty_long_duplicate_and_question_equal_items_go_and_three_stay() -> None:
    items = [
        "   ",
        "x" * 201,
        "  Who approves late claims?  ",
        "WHO APPROVES LATE CLAIMS?",
        "what is the receipt LIMIT?",
        "Where are receipts kept?",
        "How long is the retention?",
        "Is there a fourth?",
    ]

    assert validate_followups(items, QUESTION, 3) == [
        "Who approves late claims?",
        "Where are receipts kept?",
        "How long is the retention?",
    ]
    assert validate_followups(items, QUESTION, 1) == ["Who approves late claims?"]
    assert validate_followups(["y" * 200], QUESTION, 3) == ["y" * 200]
