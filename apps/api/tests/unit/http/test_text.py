from __future__ import annotations

from typing import Annotated
from uuid import uuid4

import pytest
from pydantic import BaseModel, ValidationError

from docchat_api.http.text import MultiLineText, SingleLineText, multi_line, single_line
from docchat_testing.fixtures.api import ApiUnderTest
from docchat_testing.fixtures.identities import SharedPrincipal


class _Texts(BaseModel):
    name: Annotated[str, SingleLineText(1, 10)]
    notes: Annotated[str, MultiLineText(0, 20)] = ""


def _reason(**values: object) -> str:
    with pytest.raises(ValidationError) as caught:
        _Texts.model_validate({"name": "n"} | values)
    return str(caught.value.errors()[0]["type"])


def test_t051_single_line_applies_nfc_trims_and_collapses_whitespace() -> None:
    combining_acute, line_separator, no_break = chr(0x301), chr(0x2028), chr(0xA0)
    text = f"  Cafe{combining_acute} \t\n  au{line_separator}lait  "

    assert single_line(text) == f"Caf{chr(0xE9)} au lait"
    assert single_line(f"a{no_break}{no_break}b") == "a b"


def test_t051_multi_line_converts_line_breaks_and_keeps_lf_and_tab() -> None:
    assert multi_line("  one\r\ntwo\rthree\n\tfour  \n") == "one\ntwo\nthree\n\tfour"


REFUSED = {
    "right-to-left override (Cf)": 0x202E,
    "left-to-right isolate (Cf)": 0x2066,
    "zero-width space (Cf)": 0x200B,
    "zero-width non-joiner (Cf)": 0x200C,
    "zero-width joiner (Cf)": 0x200D,
    "byte order mark (Cf)": 0xFEFF,
    "bell (Cc)": 0x07,
    "unit separator (Cc), not collapsed as whitespace": 0x1F,
    "private use (Co)": 0xE000,
    "language tag (Cf)": 0xE0001,
    "unassigned (Cn)": 0x378,
    "lone surrogate (Cs)": 0xD800,
}


@pytest.mark.parametrize("code_point", REFUSED.values(), ids=REFUSED.keys())
def test_t051_both_types_refuse_the_refused_categories(code_point: int) -> None:
    char = chr(code_point)

    assert _reason(name=f"a{char}b") == "invalid_characters"
    assert _reason(notes=f"a{char}b") == "invalid_characters"


@pytest.mark.parametrize("char", ["\n", "\t", "\r"])
def test_t051_single_line_text_collapses_line_breaks_and_tabs(char: str) -> None:
    assert _Texts.model_validate({"name": f"a{char}b"}).name == "a b"


def test_multi_line_refuses_other_controls_but_keeps_lf_and_tab() -> None:
    assert _Texts.model_validate({"name": "n", "notes": "a\n\tb"}).notes == "a\n\tb"
    assert _reason(notes="a\x0bb") == "invalid_characters"


def test_lengths_count_code_points_after_normalisation() -> None:
    decomposed = "e" + chr(0x301)

    assert _Texts.model_validate({"name": f"  {decomposed * 10}  "}).name == chr(0xE9) * 10
    assert _reason(name="x" * 11) == "string_too_long"
    assert _reason(name="   ") == "string_too_short"


def test_a_value_that_is_not_text_reports_its_type() -> None:
    assert _reason(name=7) == "string_type"


def test_the_schema_states_the_bounds() -> None:
    properties = _Texts.model_json_schema()["properties"]

    assert (properties["name"]["minLength"], properties["name"]["maxLength"]) == (1, 10)
    assert (properties["notes"]["minLength"], properties["notes"]["maxLength"]) == (0, 20)


def test_bounds_must_be_ordered() -> None:
    with pytest.raises(ValueError, match="bounds"):
        SingleLineText(5, 4)


async def test_t051_a_body_with_u202e_answers_invalid_characters_at_its_path(
    started: ApiUnderTest, alice: SharedPrincipal
) -> None:
    response = await started.client.post(
        f"/v1/probe/items/{uuid4()}",
        json={"name": "a\u202eb", "projectId": str(uuid4())},
        headers=alice.authorization(),
    )

    assert response.status_code == 422
    assert response.json()["errors"] == [{"path": "body.name", "reason": "invalid_characters"}]
