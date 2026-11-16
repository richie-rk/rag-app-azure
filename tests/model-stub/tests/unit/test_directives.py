from __future__ import annotations

import pytest

from docchat_model_stub.directives import (
    Directives,
    UnknownDirectiveError,
    collect,
    marks_attack,
)


def test_no_token_gives_the_defaults() -> None:
    assert collect(("plain question",), "stream") == Directives()


def test_every_directive_parses() -> None:
    text = (
        "[[stub:status=503]] [[stub:filter=completion]] [[stub:cut=3]] [[stub:no_usage]] "
        "[[stub:tokens=7]] [[stub:delay_ms=0]] [[stub:first_ms=40000]] [[stub:hang]] "
        "[[stub:shield=attack]]"
    )
    assert collect((text,), "stream") == Directives(
        status=503,
        content_filter="completion",
        cut=3,
        no_usage=True,
        tokens=7,
        delay_ms=0,
        first_ms=40000,
        hang=True,
        shield_attack=True,
    )


def test_filter_prompt() -> None:
    assert collect(("[[stub:filter=prompt]]",), "json").content_filter == "prompt"


def test_a_qualified_directive_applies_only_to_its_kind() -> None:
    text = "[[stub:status=500@stream]] question"
    assert collect((text,), "stream").status == 500
    for kind in ("json", "text", "embed", "shield"):
        assert collect((text,), kind).status is None


def test_an_unqualified_directive_applies_to_every_kind() -> None:
    for kind in ("stream", "json", "text", "embed", "shield"):
        assert collect(("[[stub:hang]]",), kind).hang


def test_the_later_token_of_a_name_wins() -> None:
    assert collect(("[[stub:tokens=5]]", "[[stub:tokens=9]]"), "stream").tokens == 9


@pytest.mark.parametrize(
    "token",
    [
        "[[stub:bogus]]",
        "[[stub:status]]",
        "[[stub:status=abc]]",
        "[[stub:status=200]]",
        "[[stub:status=600]]",
        "[[stub:status=4_00]]",
        "[[stub:filter=other]]",
        "[[stub:filter]]",
        "[[stub:cut]]",
        "[[stub:no_usage=1]]",
        "[[stub:tokens=0]]",
        "[[stub:hang=1]]",
        "[[stub:shield=benign]]",
        "[[stub:shield]]",
    ],
)
def test_unknown_directives_are_refused(token: str) -> None:
    with pytest.raises(UnknownDirectiveError) as caught:
        collect((f"question {token}",), "text")
    assert caught.value.token == token


def test_an_unknown_directive_for_another_kind_is_ignored() -> None:
    assert collect(("[[stub:bogus@embed]]",), "stream") == Directives()


def test_text_outside_the_pattern_is_plain_text() -> None:
    assert collect(("[[stub:status=500@other]] [[STUB:hang]]",), "text") == Directives()


def test_marks_attack_reads_one_text() -> None:
    assert marks_attack("doc [[stub:shield=attack]]")
    assert marks_attack("doc [[stub:shield=attack@shield]]")
    assert not marks_attack("doc [[stub:shield=attack@stream]]")
    assert not marks_attack("benign document")
