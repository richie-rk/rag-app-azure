from __future__ import annotations

from typing import Any

import pytest

from docchat_api.http.preconditions import PreconditionFailed
from docchat_api.routes import ingestion_problems
from docchat_api.routes.ingestion_problems import (
    IndexBusy,
    IndexNotFound,
    InvalidMethodParameters,
    is_display_name_taken,
    parameter_issues,
    raise_refusal,
    settings_problem,
)
from docchat_core.errors import CODES, DocChatProblem, FieldIssue
from docchat_core.ingestion.methods import RECURSIVE_V1
from docchat_core.ingestion.models import IngestionRefusedError
from docchat_core.sql.engine import SqlUniqueViolation
from docchat_core.sql.repositories.indexes import IndexSettingsRefusedError


def test_every_problem_class_names_its_registered_code() -> None:
    classes = [
        value
        for value in vars(ingestion_problems).values()
        if isinstance(value, type)
        and issubclass(value, DocChatProblem)
        and value.__module__ == ingestion_problems.__name__
    ]

    assert len(classes) == 15
    for problem in classes:
        assert CODES[problem.code].status is not None


def test_a_listed_refusal_becomes_its_problem() -> None:
    with pytest.raises(IndexBusy):
        raise_refusal(IngestionRefusedError("index_busy"), {"index_busy"})
    with pytest.raises(PreconditionFailed):
        raise_refusal(IngestionRefusedError("precondition_failed"), {"precondition_failed"})
    with pytest.raises(IndexNotFound) as raised:
        raise_refusal(IngestionRefusedError("index_not_found"), {"index_not_found"})
    assert raised.value.__cause__ is None


@pytest.mark.parametrize(
    ("code", "answers"),
    [("index_busy", set()), ("unsupported_embedding_model", {"unsupported_embedding_model"})],
)
def test_an_unlisted_or_unmapped_refusal_is_raised_as_it_is(code: Any, answers: set[str]) -> None:
    refusal = IngestionRefusedError(code)

    with pytest.raises(IngestionRefusedError) as raised:
        raise_refusal(refusal, answers)

    assert raised.value is refusal


@pytest.mark.parametrize(
    ("code", "path", "reason"),
    [
        ("method_not_found", "body.methodKey", "not_found"),
        ("method_inactive", "body.methodKey", "not_allowed"),
        ("unsupported_embedding_model", "body.embeddingModel", "not_allowed"),
        ("unsupported_dimensions", "body.embeddingDimensions", "not_allowed"),
        ("unsupported_analyzer", "body.languageAnalyzer", "not_allowed"),
    ],
)
def test_each_catalogue_refusal_names_its_field(code: Any, path: str, reason: str) -> None:
    problem = settings_problem(IndexSettingsRefusedError(code))

    assert problem.entry.code == code
    assert problem.status == 422
    assert problem.errors == (FieldIssue(path, reason),)


def _parameter_errors(parameters: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        RECURSIVE_V1.validate_parameters(parameters)
    except Exception as invalid:  # noqa: BLE001 - the model's ValidationError
        return invalid.errors(include_input=False)  # type: ignore[attr-defined, no-any-return]
    raise AssertionError("the parameters are valid")


def test_invalid_parameters_get_one_entry_per_field_with_the_models_reason() -> None:
    errors = _parameter_errors(
        {"targetTokens": "512", "overlapPercent": 27, "tableMaxTokens": 9000, "other": True}
    )

    problem = settings_problem(IndexSettingsRefusedError("invalid_method_parameters", errors))

    assert isinstance(problem, InvalidMethodParameters)
    assert problem.errors == (
        FieldIssue("body.methodParameters.targetTokens", "invalid_type"),
        FieldIssue("body.methodParameters.overlapPercent", "not_allowed"),
        FieldIssue("body.methodParameters.tableMaxTokens", "out_of_range"),
        FieldIssue("body.methodParameters.other", "unknown_field"),
    )


def test_list_parameter_errors_are_reported_at_the_parameter_once() -> None:
    issues = parameter_issues(
        [
            {"loc": ("ocrLanguages", 1), "type": "literal_error"},
            {"loc": ("ocrLanguages", 2), "type": "literal_error"},
            {"loc": ("ocrLanguages",), "type": "duplicate"},
            {"loc": (), "type": "model_type"},
        ]
    )

    assert issues == [
        FieldIssue("body.methodParameters.ocrLanguages", "not_allowed"),
        FieldIssue("body.methodParameters", "invalid_type"),
    ]
    assert parameter_issues([]) == [FieldIssue("body.methodParameters", "invalid_format")]


def test_only_the_display_name_index_is_display_name_taken() -> None:
    assert is_display_name_taken(
        SqlUniqueViolation(2601, "23000", "UX_search_indexes_display_name")
    )
    assert not is_display_name_taken(SqlUniqueViolation(2627, "23000", "PK_search_indexes"))
