from __future__ import annotations

from collections.abc import Callable

import pytest

from docchat_core.errors.reasons import REASONS, ReasonEntry, _catalogue

type SpecTable = Callable[[str, str], list[list[str]]]


def test_reasons_match_section_2_5(spec_table: SpecTable) -> None:
    rows = spec_table("11-api.md", "### 2.5 Validation reasons")

    assert [row[0] for row in rows] == list(REASONS)
    for reason, meaning, _used_by in rows:
        assert REASONS[reason].meaning == meaning, reason


def test_catalogue_holds_24_reasons() -> None:
    assert len(REASONS) == 24


def test_the_reasons_the_api_maps_pydantic_errors_to_are_registered() -> None:
    mapped = {
        "required",
        "unknown_field",
        "invalid_json",
        "invalid_uuid",
        "too_short",
        "too_long",
        "too_many_items",
        "out_of_range",
        "not_allowed",
        "invalid_type",
        "invalid_characters",
        "duplicate",
        "invalid_cursor",
        "empty",
        "invalid_format",
    }

    assert mapped <= set(REASONS)


@pytest.mark.parametrize(("reason", "meaning"), [("Bad", "a meaning"), ("fine", "")])
def test_reason_entry_refuses_an_invalid_entry(reason: str, meaning: str) -> None:
    with pytest.raises(ValueError, match="reason"):
        ReasonEntry(reason, meaning)


def test_catalogue_refuses_a_reason_registered_twice() -> None:
    entry = ReasonEntry("required", "missing")

    with pytest.raises(ValueError, match="registered twice"):
        _catalogue((entry, entry))


def test_reasons_is_read_only() -> None:
    with pytest.raises(TypeError):
        REASONS["new_reason"] = ReasonEntry("new_reason", "x")  # type: ignore[index]
