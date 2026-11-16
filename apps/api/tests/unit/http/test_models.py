from __future__ import annotations

import pytest
from pydantic import ValidationError

from docchat_api.http.models import QueryModel, RequestModel, ResponseModel


class _Body(RequestModel):
    example_questions: list[str]


class _Query(QueryModel):
    project_id: str | None = None


class _View(ResponseModel):
    has_default_index: bool
    next_cursor: str | None


def test_a_request_takes_camel_case_members_only() -> None:
    assert _Body.model_validate({"exampleQuestions": ["q"]}).example_questions == ["q"]
    with pytest.raises(ValidationError) as caught:
        _Body.model_validate({"example_questions": ["q"]})

    assert {error["type"] for error in caught.value.errors()} == {"missing", "extra_forbidden"}


def test_a_query_refuses_an_unknown_parameter() -> None:
    with pytest.raises(ValidationError) as caught:
        _Query.model_validate({"projectId": "x", "other": "y"})

    assert caught.value.errors()[0]["loc"] == ("other",)


def test_a_response_serialises_every_field_by_alias() -> None:
    view = _View(has_default_index=True, next_cursor=None)

    assert view.model_dump(mode="json") == {"hasDefaultIndex": True, "nextCursor": None}
    assert set(_View.model_json_schema()["required"]) == {"hasDefaultIndex", "nextCursor"}
