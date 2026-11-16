"""The authentication refusals (`11-api.md` sections 1.2 and 2.2; ADR 0017 items 2 and 4).

Each refusal is a `DocChatProblem` that carries its telemetry `reason` from the closed set of
ADR 0017, so the API's problem handler answers with the catalogue's status and text and the
validator's span and counter name the step that failed. A `401` also carries the
`WWW-Authenticate` value the API sends with it. Nothing from the token itself (claims, header,
text) is ever held here.
"""

from __future__ import annotations

from typing import ClassVar, Final, Literal, override

from docchat_core.errors import DocChatProblem

type AuthReason = Literal[
    "missing_header",
    "bad_scheme",
    "duplicate_header",
    "token_too_long",
    "malformed",
    "bad_alg",
    "missing_kid",
    "unknown_kid",
    "keys_unavailable",
    "bad_signature",
    "expired",
    "not_yet_valid",
    "bad_iat",
    "bad_issuer",
    "bad_audience",
    "missing_claim",
    "bad_version",
    "bad_tenant",
    "bad_client",
    "missing_scope",
    "bad_oid",
    "bad_roles",
    "no_role",
]
"""The telemetry `reason` of a refused token, one per check of the validator."""

type AuthOutcome = Literal["ok", "invalid", "no_role", "unavailable"]
"""`docchat.auth.outcome` and the `outcome` dimension of `docchat.auth.validations`."""

WWW_AUTHENTICATE: Final = 'Bearer realm="docchat"'
"""The `WWW-Authenticate` value of every in-app `401`."""

# A header value, not a secret.
WWW_AUTHENTICATE_INVALID_TOKEN: Final = 'Bearer realm="docchat", error="invalid_token"'  # noqa: S105
"""The `WWW-Authenticate` value of `401 invalid_token`."""

_REQUIRED_REASONS: Final[frozenset[AuthReason]] = frozenset({"missing_header", "bad_scheme"})
_ROLE_REASONS: Final[frozenset[AuthReason]] = frozenset({"no_role"})
_UNAVAILABLE_REASONS: Final[frozenset[AuthReason]] = frozenset({"keys_unavailable"})
_INVALID_REASONS: Final[frozenset[AuthReason]] = frozenset(
    {
        "duplicate_header",
        "token_too_long",
        "malformed",
        "bad_alg",
        "missing_kid",
        "unknown_kid",
        "bad_signature",
        "expired",
        "not_yet_valid",
        "bad_iat",
        "bad_issuer",
        "bad_audience",
        "missing_claim",
        "bad_version",
        "bad_tenant",
        "bad_client",
        "missing_scope",
        "bad_oid",
        "bad_roles",
    }
)


class AuthenticationFailure(DocChatProblem):
    """Base of the four refusals of token validation. `reason` is the check that failed;
    `www_authenticate` is the header value a `401` carries, `None` otherwise."""

    reasons: ClassVar[frozenset[AuthReason]]
    outcome: ClassVar[AuthOutcome]
    www_authenticate: ClassVar[str | None] = None

    def __init__(self, reason: AuthReason) -> None:
        if reason not in self.reasons:
            raise ValueError(f"{type(self).__name__} doesn't carry the reason {reason!r}")
        super().__init__()
        self.reason: AuthReason = reason


class AuthenticationRequired(AuthenticationFailure):
    """`401 authentication_required`: no `Authorization` header, or a scheme other than
    `Bearer`."""

    code = "authentication_required"
    reasons = _REQUIRED_REASONS
    outcome = "invalid"
    www_authenticate = WWW_AUTHENTICATE


class InvalidToken(AuthenticationFailure):
    """`401 invalid_token`: any other failure of the header, the token or its claims."""

    code = "invalid_token"
    reasons = _INVALID_REASONS
    outcome = "invalid"
    www_authenticate = WWW_AUTHENTICATE_INVALID_TOKEN


class RoleNotAssigned(AuthenticationFailure):
    """`403 role_not_assigned`: a valid token whose `roles` holds neither `User` nor `Admin`."""

    code = "role_not_assigned"
    reasons = _ROLE_REASONS
    outcome = "no_role"

    @override
    def __init__(self, reason: AuthReason = "no_role") -> None:
        super().__init__(reason)


class AuthenticationUnavailable(AuthenticationFailure):
    """`503 authentication_unavailable` (`retryAfterSeconds` 30): no valid signing-key set exists
    and the fetch failed, so no token can be checked."""

    code = "authentication_unavailable"
    reasons = _UNAVAILABLE_REASONS
    outcome = "unavailable"

    @override
    def __init__(self, reason: AuthReason = "keys_unavailable") -> None:
        super().__init__(reason)
