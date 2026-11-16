from __future__ import annotations

import dataclasses
from uuid import uuid4

import pytest

from docchat_core.access.models import (
    MAX_PAGE_LIMIT,
    AccessibleProject,
    ProjectSummaryPage,
    example_questions_from,
)


def test_example_questions_come_from_the_json_array() -> None:
    assert example_questions_from('["What changed?", "Who owns it?"]') == (
        "What changed?",
        "Who owns it?",
    )
    assert example_questions_from("[]") == ()


@pytest.mark.parametrize("text", ['{"a": 1}', "[1]", '["a", null]', "nope"])
def test_a_broken_column_raises(text: str) -> None:
    with pytest.raises(ValueError, match=r"array of strings|Expecting value"):
        example_questions_from(text)


def test_the_results_are_frozen() -> None:
    project = AccessibleProject(
        id=uuid4(), name="A", description="", instructions="", example_questions=(), access="member"
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        project.access = "admin"  # type: ignore[misc]
    assert ProjectSummaryPage(items=(), next_after=None).items == ()
    assert MAX_PAGE_LIMIT == 100
