"""The OpenAPI access metadata of every operation (`11-api.md` sections 1.4.1, 1.13 and 1.14;
`84-testing.md` section 3; `82-security.md` SEC-11 to SEC-14).

Each endpoint declares, with the `access` decorator, its access level (`public`, `user` or
`admin`), its scoped parameters and its own problem codes. The route class publishes them as
`x-docchat-access`, `x-docchat-scoped-params` and `x-docchat-error-codes`, the last one completed
with the common answer sets of section 1.14 that apply to the route, so a route can't forget a
code its platform checks can answer. The authorisation matrix test generates its cases from these
extensions; a route without them refuses to build, so access is never implicit.

The access decisions themselves are `docchat_core.access`'s; the route class runs the role and
administrator checks of section 1.3 from the declared level.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Final, Literal

from docchat_core.errors import CODES

type AccessLevel = Literal["public", "user", "admin"]

ACCESS_EXTENSION: Final = "x-docchat-access"
SCOPED_PARAMS_EXTENSION: Final = "x-docchat-scoped-params"
ERROR_CODES_EXTENSION: Final = "x-docchat-error-codes"
ACCESS_ATTRIBUTE: Final = "__docchat_access__"

BODY_POINTER_PREFIX: Final = "body:/"
ROUTER_CODES: Final = frozenset({"route_not_found", "method_not_allowed"})
"""Answered by the router, in no operation's list."""

BASE_CODES: Final = frozenset(
    {
        "authentication_required",
        "invalid_token",
        "role_not_assigned",
        "internal_error",
        "authentication_unavailable",
        "dependency_unavailable",
    }
)
BURST_CODES: Final = frozenset({"too_many_requests"})
ADMIN_CODES: Final = frozenset({"admin_required"})
WRITE_CODES: Final = frozenset({"write_rate_limited"})
BODY_CODES: Final = frozenset({"request_too_large", "unsupported_media_type", "validation_failed"})
PARAMS_CODES: Final = frozenset({"validation_failed"})
PRECONDITION_CODES: Final = frozenset({"precondition_required", "precondition_failed"})


@dataclass(frozen=True, slots=True, kw_only=True)
class AccessSpec:
    """What an endpoint declares: its level, its scoped parameters (path or query names, or
    `body:<JSON pointer>`), the problem codes of its own steps, and whether it requires
    `If-Match` (the Precondition set)."""

    level: AccessLevel
    scoped_params: tuple[str, ...] = ()
    error_codes: frozenset[str] = frozenset()
    if_match: bool = False

    def __post_init__(self) -> None:
        for code in self.error_codes:
            entry = CODES.get(code)
            if entry is None or entry.status is None or code in ROUTER_CODES:
                raise ValueError(f"{code!r} isn't a problem code an operation can answer")
        if len(set(self.scoped_params)) != len(self.scoped_params):
            raise ValueError("a scoped parameter is listed twice")
        if self.level == "public" and (self.scoped_params or self.error_codes or self.if_match):
            raise ValueError("a public operation has no in-app check and no code")


def access(
    level: AccessLevel,
    *,
    scoped_params: Iterable[str] = (),
    error_codes: Iterable[str] = (),
    if_match: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Declare an endpoint's access metadata; put it below the router decorator, so it applies
    before the route is built."""
    spec = AccessSpec(
        level=level,
        scoped_params=tuple(scoped_params),
        error_codes=frozenset(error_codes),
        if_match=if_match,
    )

    def mark[F: Callable[..., Any]](endpoint: F) -> F:
        setattr(endpoint, ACCESS_ATTRIBUTE, spec)
        return endpoint

    return mark


def access_of(endpoint: Callable[..., Any]) -> AccessSpec | None:
    spec = getattr(endpoint, ACCESS_ATTRIBUTE, None)
    return spec if isinstance(spec, AccessSpec) else None


def error_codes(
    spec: AccessSpec,
    *,
    burst_limited: bool,
    write_limited: bool,
    json_body: bool,
    parameters: bool,
) -> list[str]:
    """The operation's `x-docchat-error-codes`: the common sets that apply and its own codes,
    sorted (section 1.14). `getHealth` has none."""
    if spec.level == "public":
        return []
    codes = set(BASE_CODES) | spec.error_codes
    if burst_limited:
        codes |= BURST_CODES
    if spec.level == "admin":
        codes |= ADMIN_CODES
    if write_limited:
        codes |= WRITE_CODES
    if json_body:
        codes |= BODY_CODES
    if parameters:
        codes |= PARAMS_CODES
    if spec.if_match:
        codes |= PRECONDITION_CODES
    return sorted(codes)


def extensions(spec: AccessSpec, codes: list[str]) -> dict[str, Any]:
    """The three extensions as the operation object carries them."""
    return {
        ACCESS_EXTENSION: spec.level,
        SCOPED_PARAMS_EXTENSION: list(spec.scoped_params),
        ERROR_CODES_EXTENSION: codes,
    }


def public_operation() -> dict[str, Any]:
    """The `openapi_extra` of `getHealth`, the one public operation, which isn't served by the
    route class: the three extensions, all empty but the level, and `security: []`."""
    return extensions(AccessSpec(level="public"), []) | {"security": []}
