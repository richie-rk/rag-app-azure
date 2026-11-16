from __future__ import annotations

from uuid import UUID

import pytest

from docchat_core.errors.codes import CODES, RETRY_AFTER_MAX
from docchat_core.errors.problems import (
    MAX_FIELD_ERRORS,
    DocChatProblem,
    FieldIssue,
    problem_entry,
)

_SESSION = UUID("60191fe1-b79c-4c47-995d-e0a0e20cb164")
_TURN = UUID("fc6c167c-bdc0-407d-9fcb-0bb4c6a2c701")


class _AdminRequired(DocChatProblem):
    code = "admin_required"


class _DependencyUnavailable(DocChatProblem):
    code = "dependency_unavailable"


class _RateLimited(DocChatProblem):
    code = "rate_limited"


class _ValidationFailed(DocChatProblem):
    code = "validation_failed"


class _BatchTooLarge(DocChatProblem):
    code = "batch_too_large"


class _TurnInProgress(DocChatProblem):
    code = "turn_in_progress"


class _SessionTurnLimit(DocChatProblem):
    code = "session_turn_limit"


class _AbstractBase(DocChatProblem):
    """An intermediate base that names no code."""


def test_a_problem_takes_status_title_and_detail_from_the_catalogue() -> None:
    problem = _AdminRequired()

    assert problem.entry is CODES["admin_required"]
    assert (problem.status, problem.title, problem.detail) == (
        403,
        "Administrator access needed",
        "This needs administrator access.",
    )
    assert problem.retry_after_seconds is None
    assert problem.errors == ()
    assert (problem.session_id, problem.turn_id) == (None, None)


def test_the_exception_text_is_the_code_alone() -> None:
    assert str(_AdminRequired()) == "admin_required"


def test_a_fixed_wait_is_filled_in() -> None:
    assert _DependencyUnavailable().retry_after_seconds == 5


def test_a_fixed_wait_accepts_its_own_value_only() -> None:
    assert _DependencyUnavailable(retry_after_seconds=5).retry_after_seconds == 5
    with pytest.raises(ValueError, match="within 5 to 5"):
        _DependencyUnavailable(retry_after_seconds=6)


def test_a_ranged_wait_must_be_given_within_its_range() -> None:
    assert _RateLimited(retry_after_seconds=120).retry_after_seconds == 120
    with pytest.raises(ValueError, match="needs retry_after_seconds"):
        _RateLimited()
    for refused in (0, 121, True):
        with pytest.raises(ValueError, match="within 1 to 120"):
            _RateLimited(retry_after_seconds=refused)


def test_a_wait_is_refused_on_a_code_without_one() -> None:
    with pytest.raises(ValueError, match="no retryAfterSeconds"):
        _AdminRequired(retry_after_seconds=1)


def test_a_422_needs_field_errors_and_keeps_the_first_50() -> None:
    issues = [FieldIssue(f"body.items.{index}", "required") for index in range(60)]

    problem = _ValidationFailed(errors=issues)

    assert problem.errors == tuple(issues[:MAX_FIELD_ERRORS])
    with pytest.raises(ValueError, match="at least one field error"):
        _ValidationFailed()


def test_field_errors_are_refused_on_other_codes() -> None:
    with pytest.raises(ValueError, match="carries no errors"):
        _AdminRequired(errors=[FieldIssue("body", "required")])


def test_batch_too_large_fills_its_placeholder() -> None:
    problem = _BatchTooLarge(
        errors=[FieldIssue("body.files", "too_many_items")], detail_values={"maxFilesPerBatch": 100}
    )

    assert problem.detail == "Send at most 100 files at a time."
    with pytest.raises(ValueError, match="placeholders"):
        _BatchTooLarge(errors=[FieldIssue("body.files", "too_many_items")])


def test_turn_in_progress_carries_session_turn_and_a_computed_wait() -> None:
    problem = _TurnInProgress(retry_after_seconds=211, session_id=_SESSION, turn_id=_TURN)

    assert (problem.retry_after_seconds, problem.session_id, problem.turn_id) == (
        211,
        _SESSION,
        _TURN,
    )
    assert (
        _TurnInProgress(
            retry_after_seconds=RETRY_AFTER_MAX, session_id=_SESSION, turn_id=_TURN
        ).retry_after_seconds
        == RETRY_AFTER_MAX
    )
    with pytest.raises(ValueError, match="needs turnId"):
        _TurnInProgress(retry_after_seconds=1, session_id=_SESSION)


def test_session_turn_limit_carries_a_session_and_no_turn() -> None:
    assert _SessionTurnLimit(session_id=_SESSION).session_id == _SESSION
    with pytest.raises(ValueError, match="carries no turnId"):
        _SessionTurnLimit(session_id=_SESSION, turn_id=_TURN)
    with pytest.raises(ValueError, match="needs sessionId"):
        _SessionTurnLimit()


def test_session_and_turn_are_refused_elsewhere() -> None:
    with pytest.raises(ValueError, match="carries no sessionId"):
        _AdminRequired(session_id=_SESSION)


@pytest.mark.parametrize("code", ["model_busy", "turn_abandoned", "internal_errors"])
def test_a_code_without_status_or_registration_can_never_be_a_problem(code: str) -> None:
    with pytest.raises(TypeError, match=code):
        type("_Refused", (DocChatProblem,), {"code": code})


def test_every_stream_and_stored_code_is_refused_as_a_problem() -> None:
    for code, entry in CODES.items():
        if entry.status is None:
            with pytest.raises(TypeError, match="never a problem"):
                problem_entry(code)


def test_a_class_without_a_code_cannot_be_raised() -> None:
    with pytest.raises(TypeError, match="names no problem code"):
        DocChatProblem()
    with pytest.raises(TypeError, match="names no problem code"):
        _AbstractBase()


def test_problems_are_exceptions() -> None:
    with pytest.raises(DocChatProblem) as caught:
        raise _AdminRequired

    assert caught.value.status == 403


@pytest.mark.parametrize(("path", "reason"), [("", "required"), ("x" * 257, "required")])
def test_field_issue_bounds_its_path(path: str, reason: str) -> None:
    with pytest.raises(ValueError, match="field path"):
        FieldIssue(path, reason)


def test_field_issue_needs_a_registered_reason() -> None:
    with pytest.raises(ValueError, match="REASONS"):
        FieldIssue("body.name", "no_such_reason")
