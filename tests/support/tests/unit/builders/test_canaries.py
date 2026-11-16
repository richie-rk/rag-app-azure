from __future__ import annotations

import re

import pytest

from docchat_testing.builders.canaries import CANARY_KINDS, CanaryKind, canary, is_canary


@pytest.mark.parametrize("kind", CANARY_KINDS)
def test_canary_embeds_kind_and_eight_hex(kind: CanaryKind) -> None:
    value = canary(kind)
    assert re.search(rf"cnry-{kind}-[0-9a-f]{{8}}", value)
    assert is_canary(value)
    assert canary(kind) != value


def test_the_eleven_kinds_of_section_2_10() -> None:
    assert CANARY_KINDS == (
        "question",
        "answer",
        "chunk",
        "filename",
        "fact",
        "email",
        "displayname",
        "instruction",
        "comment",
        "title",
        "summary",
    )


def test_email_canary_is_an_address() -> None:
    assert re.fullmatch(r"cnry-email-[0-9a-f]{8}@example\.com", canary("email"))


def test_unknown_kind_and_plain_text() -> None:
    with pytest.raises(ValueError, match="unknown canary kind"):
        canary("password")  # type: ignore[arg-type]
    assert not is_canary("cnry-password-0123abcd")
    assert not is_canary("an ordinary title")
    assert is_canary(f"prefix {canary('chunk')} suffix")
