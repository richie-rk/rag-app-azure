from __future__ import annotations

from docchat_core.access.exceptions import AdminRequired, ProjectIndexUnavailable, ProjectNotFound


def test_each_refusal_answers_its_code() -> None:
    assert (AdminRequired().code, AdminRequired().status) == ("admin_required", 403)
    assert (ProjectNotFound().code, ProjectNotFound().status) == ("project_not_found", 404)
    refused = ProjectIndexUnavailable()
    assert (refused.code, refused.status) == ("project_index_unavailable", 409)
    assert refused.retry_after_seconds is None


def test_a_refusal_carries_no_identifier() -> None:
    # Every unreachable project answers the same body as a random identifier.
    assert str(ProjectNotFound()) == "project_not_found"
    assert ProjectNotFound().detail == ProjectNotFound().detail
