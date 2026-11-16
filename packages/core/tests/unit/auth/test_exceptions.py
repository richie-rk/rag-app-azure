from __future__ import annotations

import pytest

from docchat_core.auth.exceptions import (
    WWW_AUTHENTICATE,
    WWW_AUTHENTICATE_INVALID_TOKEN,
    AuthenticationRequired,
    AuthenticationUnavailable,
    InvalidToken,
    RoleNotAssigned,
)
from docchat_core.telemetry.attributes import AUTH_REASONS


def test_authentication_required_is_a_401_with_the_realm() -> None:
    refused = AuthenticationRequired("missing_header")
    assert (refused.code, refused.status, refused.reason) == (
        "authentication_required",
        401,
        "missing_header",
    )
    assert refused.www_authenticate == 'Bearer realm="docchat"' == WWW_AUTHENTICATE
    assert refused.outcome == "invalid"
    assert str(refused) == "authentication_required"


def test_invalid_token_names_the_error_in_its_challenge() -> None:
    refused = InvalidToken("expired")
    assert (refused.code, refused.status) == ("invalid_token", 401)
    assert refused.www_authenticate == WWW_AUTHENTICATE_INVALID_TOKEN
    assert WWW_AUTHENTICATE_INVALID_TOKEN == 'Bearer realm="docchat", error="invalid_token"'
    assert refused.outcome == "invalid"


def test_role_not_assigned_is_a_403_without_a_challenge() -> None:
    refused = RoleNotAssigned()
    assert (refused.code, refused.status, refused.reason) == ("role_not_assigned", 403, "no_role")
    assert refused.www_authenticate is None
    assert refused.outcome == "no_role"


def test_authentication_unavailable_asks_for_a_retry_after_30_seconds() -> None:
    refused = AuthenticationUnavailable()
    assert (refused.code, refused.status, refused.reason) == (
        "authentication_unavailable",
        503,
        "keys_unavailable",
    )
    assert refused.retry_after_seconds == 30
    assert refused.www_authenticate is None
    assert refused.outcome == "unavailable"


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        (AuthenticationRequired, "expired"),
        (InvalidToken, "missing_header"),
        (InvalidToken, "no_role"),
        (RoleNotAssigned, "bad_roles"),
        (AuthenticationUnavailable, "unknown_kid"),
    ],
)
def test_each_refusal_carries_only_its_own_reasons(kind: type[InvalidToken], reason: str) -> None:
    with pytest.raises(ValueError, match="doesn't carry"):
        kind(reason)  # type: ignore[arg-type]


def test_the_reasons_are_the_registered_telemetry_values() -> None:
    reasons = (
        AuthenticationRequired.reasons
        | InvalidToken.reasons
        | RoleNotAssigned.reasons
        | AuthenticationUnavailable.reasons
    )
    assert reasons == AUTH_REASONS
    assert len(reasons) == 23
