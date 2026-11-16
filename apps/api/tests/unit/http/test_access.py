from __future__ import annotations

import pytest

from docchat_api.http.access import (
    ADMIN_CODES,
    BASE_CODES,
    BODY_CODES,
    BURST_CODES,
    PRECONDITION_CODES,
    WRITE_CODES,
    AccessSpec,
    access,
    access_of,
    error_codes,
    extensions,
    public_operation,
)


def test_the_common_sets_are_section_1_14() -> None:
    assert {
        "authentication_required",
        "invalid_token",
        "role_not_assigned",
        "internal_error",
        "authentication_unavailable",
        "dependency_unavailable",
    } == BASE_CODES
    assert {"too_many_requests"} == BURST_CODES
    assert {"admin_required"} == ADMIN_CODES
    assert {"write_rate_limited"} == WRITE_CODES
    assert {"request_too_large", "unsupported_media_type", "validation_failed"} == BODY_CODES
    assert {"precondition_required", "precondition_failed"} == PRECONDITION_CODES


def test_a_public_operation_has_no_codes() -> None:
    spec = AccessSpec(level="public")

    assert (
        error_codes(spec, burst_limited=True, write_limited=True, json_body=True, parameters=True)
        == []
    )


def test_t046_a_user_read_gets_base_burst_and_params_sorted() -> None:
    spec = AccessSpec(level="user")

    codes = error_codes(
        spec, burst_limited=True, write_limited=False, json_body=False, parameters=True
    )

    assert codes == sorted(BASE_CODES | BURST_CODES | {"validation_failed"})


def test_t046_an_admin_write_gets_every_set_and_its_own_codes() -> None:
    spec = AccessSpec(level="admin", error_codes=frozenset({"project_archived"}), if_match=True)

    codes = error_codes(
        spec, burst_limited=True, write_limited=True, json_body=True, parameters=True
    )

    assert codes == sorted(
        BASE_CODES
        | BURST_CODES
        | ADMIN_CODES
        | WRITE_CODES
        | BODY_CODES
        | PRECONDITION_CODES
        | {"project_archived"}
    )
    assert "route_not_found" not in codes
    assert "method_not_allowed" not in codes


def test_a_route_without_the_burst_limit_lacks_too_many_requests() -> None:
    spec = AccessSpec(level="user")

    codes = error_codes(
        spec, burst_limited=False, write_limited=False, json_body=True, parameters=False
    )

    assert "too_many_requests" not in codes


@pytest.mark.parametrize(
    "codes", [{"route_not_found"}, {"method_not_allowed"}, {"turn_timeout"}, {"no_such_code"}]
)
def test_an_operation_declares_only_problem_codes(codes: set[str]) -> None:
    with pytest.raises(ValueError, match="isn't a problem code"):
        AccessSpec(level="user", error_codes=frozenset(codes))


def test_a_scoped_parameter_is_listed_once() -> None:
    with pytest.raises(ValueError, match="twice"):
        AccessSpec(level="user", scoped_params=("projectId", "projectId"))


def test_a_public_operation_declares_nothing_else() -> None:
    with pytest.raises(ValueError, match="public"):
        AccessSpec(level="public", scoped_params=("projectId",))


def test_the_decorator_marks_the_endpoint() -> None:
    @access("admin", scoped_params=["indexId"], error_codes=["index_not_found"], if_match=True)
    async def endpoint() -> None:
        return

    assert access_of(endpoint) == AccessSpec(
        level="admin",
        scoped_params=("indexId",),
        error_codes=frozenset({"index_not_found"}),
        if_match=True,
    )
    assert access_of(test_the_decorator_marks_the_endpoint) is None


def test_the_extensions() -> None:
    spec = AccessSpec(level="user", scoped_params=("sessionId",))

    assert extensions(spec, ["a"]) == {
        "x-docchat-access": "user",
        "x-docchat-scoped-params": ["sessionId"],
        "x-docchat-error-codes": ["a"],
    }


def test_get_health_is_public_with_empty_security() -> None:
    assert public_operation() == {
        "x-docchat-access": "public",
        "x-docchat-scoped-params": [],
        "x-docchat-error-codes": [],
        "security": [],
    }
