"""`TokenValidator`: the in-app layer of the API's authentication (`11-api.md` section 1.2;
`82-security.md` SEC-03 and SEC-04; ADR 0017 item 2).

`validate(authorization)` runs on every request except `GET /healthz`, before any other work of
the request, and either returns the `Principal` or raises one refusal. Its steps, the first
failure winning:

1. exactly one `Authorization` header, scheme `Bearer` in any case, one token of 1 to 16,384
   characters in three dot-separated parts;
2. the unverified JOSE header: `alg` exactly `RS256`, a `kid` of 1 to 128 characters, `typ`
   absent or `JWT`, so `none`, `HS*`, `PS*` and `ES*` stop before any key lookup;
3. the public key for `kid` from the signing-key provider;
4. `jwt.decode` with the signature, `exp`, `nbf`, `iat`, issuer and audience checks, a
   60-second leeway and the eleven required claims;
5. to 10. `ver`, `tid`, `azp`, `scp`, `oid` and `roles`.

Identity comes only from these claims: no other header, path, query, body or cursor value
decides who the caller is. Decoding is CPU work of well under 1 ms and runs on the event loop.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

import jwt

from docchat_core.auth import telemetry
from docchat_core.auth.exceptions import (
    AuthenticationFailure,
    AuthenticationRequired,
    AuthReason,
    InvalidToken,
    RoleNotAssigned,
)
from docchat_core.auth.keys import ALGORITHM, SigningKeyProvider
from docchat_core.auth.principal import Principal, display_name_from, email_from, roles_from
from docchat_core.auth.settings import AuthSettings, Client, allowed_clients
from docchat_core.auth.telemetry import ClientLabel
from docchat_core.models.identifiers import parse_uuid
from docchat_core.settings import EnvironmentName

type AuthorizationHeader = str | Sequence[str] | None
"""The request's `Authorization` values: none, one value, or every value when repeated."""

BEARER_SCHEME: Final = "bearer"
MAX_TOKEN_LENGTH: Final = 16_384
MAX_KID_LENGTH: Final = 128
LEEWAY_SECONDS: Final = 60
TOKEN_VERSION: Final = "2.0"  # noqa: S105 - the `ver` claim, not a secret
API_SCOPE: Final = "access_as_user"
REQUIRED_CLAIMS: Final = (
    "exp",
    "nbf",
    "iat",
    "iss",
    "aud",
    "sub",
    "oid",
    "tid",
    "azp",
    "scp",
    "ver",
)

# Three base64url parts; a part may be empty (an unsigned `alg: none` token has no signature), so
# such a token reaches the algorithm check and is refused there as `bad_alg`.
_TOKEN_RE: Final = re.compile(r"^[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*$")
_CREDENTIALS_RE: Final = re.compile(r"^(\S+)(?:[ \t]+(.*))?$", re.DOTALL)

# Step 4's failures, the more specific types first: a signature failure is also a `DecodeError`.
_DECODE_REASONS: Final[tuple[tuple[type[jwt.PyJWTError], AuthReason], ...]] = (
    (jwt.InvalidSignatureError, "bad_signature"),
    (jwt.ExpiredSignatureError, "expired"),
    (jwt.ImmatureSignatureError, "not_yet_valid"),
    (jwt.InvalidIssuedAtError, "bad_iat"),
    (jwt.InvalidIssuerError, "bad_issuer"),
    (jwt.InvalidAudienceError, "bad_audience"),
    (jwt.MissingRequiredClaimError, "missing_claim"),
    (jwt.InvalidAlgorithmError, "bad_alg"),
)


@dataclass
class _Progress:
    """The client once step 7 has passed: the only token-derived value telemetry may carry."""

    client: ClientLabel = "unknown"


def _token_of(authorization: AuthorizationHeader) -> str:
    values: Sequence[str]
    if authorization is None:
        values = ()
    elif isinstance(authorization, str):
        values = (authorization,)
    else:
        values = authorization
    if not values:
        raise AuthenticationRequired("missing_header")
    if len(values) > 1:
        raise InvalidToken("duplicate_header")
    credentials = _CREDENTIALS_RE.fullmatch(values[0].strip())
    if credentials is None or credentials.group(1).lower() != BEARER_SCHEME:
        raise AuthenticationRequired("bad_scheme")
    token = (credentials.group(2) or "").strip()
    if len(token) > MAX_TOKEN_LENGTH:
        raise InvalidToken("token_too_long")
    if not _TOKEN_RE.fullmatch(token):
        raise InvalidToken("malformed")
    return token


def _jose_header(token: str) -> Mapping[str, Any]:
    segment = token.split(".", 1)[0]
    try:
        header = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    except (ValueError, binascii.Error, RecursionError):
        raise InvalidToken("malformed") from None
    if not isinstance(header, dict):
        raise InvalidToken("malformed")
    return header


def _kid_of(header: Mapping[str, Any]) -> str:
    if header.get("alg") != ALGORITHM:
        raise InvalidToken("bad_alg")
    kid = header.get("kid")
    if not isinstance(kid, str) or not 1 <= len(kid) <= MAX_KID_LENGTH:
        raise InvalidToken("missing_kid")
    if "typ" in header and header["typ"] != "JWT":
        raise InvalidToken("malformed")
    return kid


def _decode_reason(error: jwt.PyJWTError) -> AuthReason:
    for kind, reason in _DECODE_REASONS:
        if isinstance(error, kind):
            return reason
    # Any other decoding failure (a non-numeric `exp`, a bad `sub`, an unusable key) is malformed.
    return "malformed"


def _uuid_claim(value: object) -> UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return parse_uuid(value)
    except ValueError:
        return None


class TokenValidator:
    """One per process, built at start-up from the settings and the signing-key provider."""

    def __init__(
        self, auth: AuthSettings, *, environment: EnvironmentName, keys: SigningKeyProvider
    ) -> None:
        self._audience = str(auth.auth_api_client_id)
        self._issuer = auth.issuer
        self._tenant_id = auth.auth_tenant_id
        self._clients: Mapping[UUID, Client] = allowed_clients(auth, environment)
        self._keys = keys

    @property
    def allowed_clients(self) -> Mapping[UUID, Client]:
        return self._clients

    async def validate(self, authorization: AuthorizationHeader) -> Principal:
        """The caller of a request with these `Authorization` values, or one of
        `AuthenticationRequired`, `InvalidToken`, `RoleNotAssigned` and
        `AuthenticationUnavailable`."""
        progress = _Progress()
        result: Principal | AuthenticationFailure
        with telemetry.validate_span() as span:
            try:
                result = await self._validate(authorization, progress)
            except AuthenticationFailure as refused:
                result = refused
                telemetry.record_validation(span, refused.outcome, refused.reason, progress.client)
            else:
                telemetry.record_validation(span, "ok", None, result.client)
        # Raised outside the span: a refused token is an outcome of the check, not an error of
        # the validator, and its exception never becomes a span event.
        if isinstance(result, AuthenticationFailure):
            raise result
        return result

    async def _validate(self, authorization: AuthorizationHeader, progress: _Progress) -> Principal:
        token = _token_of(authorization)
        kid = _kid_of(_jose_header(token))
        key = await self._keys.get_key(kid)
        claims = self._decode(token, key)
        if claims["ver"] != TOKEN_VERSION:
            raise InvalidToken("bad_version")
        tid = _uuid_claim(claims["tid"])
        if tid != self._tenant_id:
            raise InvalidToken("bad_tenant")
        azp = _uuid_claim(claims["azp"])
        client = None if azp is None else self._clients.get(azp)
        if azp is None or client is None:
            raise InvalidToken("bad_client")
        progress.client = client
        scopes = claims["scp"]
        if not isinstance(scopes, str) or API_SCOPE not in scopes.split(" "):
            raise InvalidToken("missing_scope")
        oid = _uuid_claim(claims["oid"])
        if oid is None:
            raise InvalidToken("bad_oid")
        roles_claim = claims.get("roles", [])
        if not isinstance(roles_claim, list) or not all(isinstance(r, str) for r in roles_claim):
            raise InvalidToken("bad_roles")
        roles = roles_from(roles_claim)
        if not roles:
            raise RoleNotAssigned()
        return Principal(
            oid=oid,
            tid=tid,
            azp=azp,
            client=client,
            roles=roles,
            display_name=display_name_from(claims.get("name")),
            email=email_from(claims.get("email"), claims.get("preferred_username")),
        )

    def _decode(self, token: str, key: jwt.PyJWK) -> dict[str, Any]:
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=[ALGORITHM],
                audience=self._audience,
                issuer=self._issuer,
                leeway=LEEWAY_SECONDS,
                options={
                    "require": list(REQUIRED_CLAIMS),
                    "strict_aud": True,
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_nbf": True,
                    "verify_iat": True,
                    "verify_aud": True,
                    "verify_iss": True,
                    "verify_sub": True,
                    "verify_jti": True,
                },
            )
        except jwt.PyJWTError as error:
            raise InvalidToken(_decode_reason(error)) from None
        return claims
