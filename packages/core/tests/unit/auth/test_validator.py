"""Unit tests of the token validator (T-004, T-005, T-006, T-007; `11-api.md` section 1.2 and
acceptance criterion 13)."""

from __future__ import annotations

import base64
import inspect
import json
import re
import time
from pathlib import Path
from typing import Any, override
from uuid import uuid4

import jwt
import pytest

from docchat_core.auth.exceptions import (
    AuthenticationRequired,
    AuthenticationUnavailable,
    InvalidToken,
    RoleNotAssigned,
)
from docchat_core.auth.keys import StaticSigningKeyProvider
from docchat_core.auth.settings import AuthSettings
from docchat_core.auth.validator import REQUIRED_CLAIMS, TokenValidator
from docchat_core.settings import EnvironmentName
from docchat_testing.fixtures.identities import SharedPrincipal
from docchat_testing.tokens import OMIT, SESSION_AUTH, SigningKeys, mint_token, token_claims

SETTINGS = AuthSettings(
    auth_tenant_id=SESSION_AUTH.tenant_id,
    auth_api_client_id=SESSION_AUTH.api_client_id,
    auth_web_client_id=SESSION_AUTH.web_client_id,
    auth_test_client_id=SESSION_AUTH.test_client_id,
)


class RecordingKeys(StaticSigningKeyProvider):
    """The session's key set, recording each lookup."""

    def __init__(self, signing_keys: SigningKeys) -> None:
        super().__init__(signing_keys.public_keys())
        self.lookups: list[str] = []

    @override
    async def get_key(self, kid: str) -> jwt.PyJWK:
        self.lookups.append(kid)
        return await super().get_key(kid)


class UnavailableKeys(StaticSigningKeyProvider):
    @override
    async def get_key(self, kid: str) -> jwt.PyJWK:
        raise AuthenticationUnavailable()


@pytest.fixture
def keys(signing_keys: SigningKeys) -> RecordingKeys:
    return RecordingKeys(signing_keys)


@pytest.fixture
def validator(keys: RecordingKeys) -> TokenValidator:
    return TokenValidator(SETTINGS, environment=EnvironmentName.DEV, keys=keys)


@pytest.fixture(scope="module")
def foreign_keys() -> SigningKeys:
    return SigningKeys()


def bearer(token: str) -> str:
    return f"Bearer {token}"


def unsigned(header: dict[str, Any], claims: dict[str, Any], signature: str = "") -> str:
    def part(value: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{part(header)}.{part(claims)}.{signature}"


async def reason_of(validator: TokenValidator, authorization: Any) -> str:
    with pytest.raises((AuthenticationRequired, InvalidToken, RoleNotAssigned)) as caught:
        await validator.validate(authorization)
    reason: str = caught.value.reason
    return reason


# --- A valid token ------------------------------------------------------------------------------


async def test_t007_a_valid_user_token_maps_to_its_principal(
    validator: TokenValidator, alice: SharedPrincipal
) -> None:
    principal = await validator.validate(bearer(mint_token(alice)))

    assert principal.oid == alice.oid
    assert principal.tid == SESSION_AUTH.tenant_id
    assert principal.azp == SESSION_AUTH.web_client_id
    assert principal.client == "web"
    assert principal.roles == frozenset({"User"})
    assert principal.is_admin is False
    assert principal.display_name == alice.display_name
    assert principal.email == alice.email


async def test_an_administrator_token_makes_an_administrator(
    validator: TokenValidator, carol: SharedPrincipal
) -> None:
    principal = await validator.validate(bearer(mint_token(carol)))
    assert principal.is_admin is True


@pytest.mark.parametrize("scheme", ["bearer", "BEARER", "Bearer"])
async def test_t004_the_scheme_is_case_insensitive(
    validator: TokenValidator, alice: SharedPrincipal, scheme: str
) -> None:
    principal = await validator.validate(f"{scheme} {mint_token(alice)}")
    assert principal.oid == alice.oid


async def test_one_header_may_come_as_a_list(
    validator: TokenValidator, alice: SharedPrincipal
) -> None:
    principal = await validator.validate([bearer(mint_token(alice))])
    assert principal.oid == alice.oid


async def test_the_display_claims_are_normalised(
    validator: TokenValidator, alice: SharedPrincipal
) -> None:
    token = mint_token(alice, name=" Ada\u202e ", email=OMIT, preferred_username="ada@x.test")

    principal = await validator.validate(bearer(token))

    assert principal.display_name == "Ada"
    assert principal.email == "ada@x.test"


async def test_other_roles_are_dropped(validator: TokenValidator, alice: SharedPrincipal) -> None:
    principal = await validator.validate(bearer(mint_token(alice, roles=["Reader", "User"])))
    assert principal.roles == frozenset({"User"})


# --- Step 1: the header ---------------------------------------------------------------------------


@pytest.mark.parametrize("authorization", [None, [], ()])
async def test_t004_no_header_requires_authentication(
    validator: TokenValidator, authorization: Any
) -> None:
    with pytest.raises(AuthenticationRequired) as caught:
        await validator.validate(authorization)
    assert caught.value.reason == "missing_header"
    assert caught.value.status == 401


@pytest.mark.parametrize("value", ["Basic dXNlcjpwYXNz", "Token abc.def.ghi", "", "   "])
async def test_t004_another_scheme_requires_authentication(
    validator: TokenValidator, value: str
) -> None:
    with pytest.raises(AuthenticationRequired) as caught:
        await validator.validate(value)
    assert caught.value.reason == "bad_scheme"


async def test_t004_two_headers_are_refused(
    validator: TokenValidator, alice: SharedPrincipal
) -> None:
    token = mint_token(alice)
    assert await reason_of(validator, [bearer(token), bearer(token)]) == "duplicate_header"


async def test_t004_a_token_over_16384_characters_is_refused(validator: TokenValidator) -> None:
    long_token = "a" * 8000 + "." + "b" * 8000 + "." + "c" * 383
    assert len(long_token) == 16_385
    assert await reason_of(validator, bearer(long_token)) == "token_too_long"
    exact = "a" * 8000 + "." + "b" * 8000 + "." + "c" * 382
    assert len(exact) == 16_384
    assert await reason_of(validator, bearer(exact)) == "malformed"


@pytest.mark.parametrize(
    "token", ["", "abc", "a.b", "a.b.c.d", "a.b c.d", "a$.b.c", "..", "Bearer x.y.z"]
)
async def test_a_token_not_in_three_parts_is_malformed(
    validator: TokenValidator, token: str
) -> None:
    assert await reason_of(validator, bearer(token)) == "malformed"


# --- Step 2: the JOSE header ----------------------------------------------------------------


@pytest.mark.parametrize("algorithm", ["none", "HS256", "PS256", "ES256", "RS512", None])
async def test_t004_any_algorithm_but_rs256_fails_before_a_key_lookup(
    validator: TokenValidator,
    keys: RecordingKeys,
    signing_keys: SigningKeys,
    alice: SharedPrincipal,
    algorithm: str | None,
) -> None:
    header: dict[str, Any] = {"kid": signing_keys.kid, "typ": "JWT"}
    if algorithm is not None:
        header["alg"] = algorithm
    token = unsigned(header, token_claims(alice), "c2ln" if algorithm != "none" else "")

    assert await reason_of(validator, bearer(token)) == "bad_alg"
    assert keys.lookups == []


async def test_t004_an_hs256_token_signed_with_the_public_key_is_refused(
    validator: TokenValidator,
    keys: RecordingKeys,
    signing_keys: SigningKeys,
    alice: SharedPrincipal,
) -> None:
    token = jwt.encode(
        token_claims(alice), "x" * 32, algorithm="HS256", headers={"kid": signing_keys.kid}
    )
    assert await reason_of(validator, bearer(token)) == "bad_alg"
    assert keys.lookups == []


@pytest.mark.parametrize("kid", [None, "", "k" * 129, 7])
async def test_a_header_without_a_usable_kid_is_refused(
    validator: TokenValidator, alice: SharedPrincipal, kid: object
) -> None:
    header: dict[str, Any] = {"alg": "RS256"}
    if kid is not None:
        header["kid"] = kid
    token = unsigned(header, token_claims(alice), "c2ln")
    assert await reason_of(validator, bearer(token)) == "missing_kid"


@pytest.mark.parametrize("typ", ["at+jwt", "JWS", None])
async def test_a_type_other_than_jwt_is_malformed(
    validator: TokenValidator, signing_keys: SigningKeys, alice: SharedPrincipal, typ: object
) -> None:
    token = unsigned({"alg": "RS256", "kid": signing_keys.kid, "typ": typ}, token_claims(alice))
    assert await reason_of(validator, bearer(token + "c2ln")) == "malformed"


async def test_a_header_that_is_not_a_json_object_is_malformed(validator: TokenValidator) -> None:
    array = base64.urlsafe_b64encode(b"[1]").rstrip(b"=").decode()
    assert await reason_of(validator, bearer(f"{array}.e30.c2ln")) == "malformed"
    assert await reason_of(validator, bearer("bm90IGpzb24.e30.c2ln")) == "malformed"
    assert await reason_of(validator, bearer("a.e30.c2ln")) == "malformed"


async def test_a_typ_of_jwt_and_an_absent_typ_pass(
    validator: TokenValidator, signing_keys: SigningKeys, alice: SharedPrincipal
) -> None:
    # PyJWT leaves out a `typ` whose value is empty.
    untyped = signing_keys.sign(token_claims(alice), headers={"typ": None})
    assert "typ" not in jwt.get_unverified_header(untyped)
    for token in (untyped, signing_keys.sign(token_claims(alice))):
        assert (await validator.validate(bearer(token))).oid == alice.oid


# --- Steps 3 and 4: the key and the signed claims ----------------------------------------------


async def test_t004_an_unknown_kid_is_refused(
    validator: TokenValidator, foreign_keys: SigningKeys, alice: SharedPrincipal
) -> None:
    token = foreign_keys.sign(token_claims(alice))
    assert await reason_of(validator, bearer(token)) == "unknown_kid"


async def test_t004_another_key_under_a_known_kid_has_a_bad_signature(
    validator: TokenValidator,
    signing_keys: SigningKeys,
    foreign_keys: SigningKeys,
    alice: SharedPrincipal,
) -> None:
    forged = foreign_keys.sign(token_claims(alice), headers={"kid": signing_keys.kid})
    assert await reason_of(validator, bearer(forged)) == "bad_signature"


async def test_t004_no_valid_key_set_is_unavailable(alice: SharedPrincipal) -> None:
    validator = TokenValidator(SETTINGS, environment=EnvironmentName.DEV, keys=UnavailableKeys({}))
    with pytest.raises(AuthenticationUnavailable) as caught:
        await validator.validate(bearer(mint_token(alice)))
    assert caught.value.retry_after_seconds == 30


@pytest.mark.parametrize(
    ("claim", "offset", "reason"),
    [
        ("exp", -59, None),
        ("exp", -61, "expired"),
        ("nbf", 59, None),
        ("nbf", 61, "not_yet_valid"),
        ("iat", 59, None),
        ("iat", 61, "not_yet_valid"),
    ],
)
async def test_t004_times_have_60_seconds_of_leeway(
    validator: TokenValidator, alice: SharedPrincipal, claim: str, offset: int, reason: str | None
) -> None:
    now = int(time.time())
    token = mint_token(alice, **{claim: now + offset})
    if reason is None:
        assert (await validator.validate(bearer(token))).oid == alice.oid
    else:
        assert await reason_of(validator, bearer(token)) == reason


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"iss": "https://login.microsoftonline.com/other/v2.0"}, "bad_issuer"),
        ({"iss": f"https://sts.windows.net/{SESSION_AUTH.tenant_id}/"}, "bad_issuer"),
        ({"aud": [str(SESSION_AUTH.api_client_id)]}, "bad_audience"),
        ({"aud": str(uuid4())}, "bad_audience"),
        ({"aud": f"api://{SESSION_AUTH.api_client_id}"}, "bad_audience"),
        ({"iat": "soon"}, "bad_iat"),
        ({"exp": "later"}, "malformed"),
        ({"sub": 7}, "malformed"),
    ],
)
async def test_t004_issuer_and_audience_fail_with_their_reasons(
    validator: TokenValidator, alice: SharedPrincipal, overrides: dict[str, Any], reason: str
) -> None:
    assert await reason_of(validator, bearer(mint_token(alice, **overrides))) == reason


@pytest.mark.parametrize("claim", REQUIRED_CLAIMS)
async def test_t004_each_required_claim_missing_is_refused(
    validator: TokenValidator, alice: SharedPrincipal, claim: str
) -> None:
    assert await reason_of(validator, bearer(mint_token(alice, **{claim: OMIT}))) == "missing_claim"


def test_the_eleven_required_claims() -> None:
    assert REQUIRED_CLAIMS == (
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


# --- Steps 5 to 10: the claims --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"ver": "1.0"}, "bad_version"),
        ({"ver": 2.0}, "bad_version"),
        ({"tid": str(uuid4())}, "bad_tenant"),
        ({"tid": "not-a-tenant"}, "bad_tenant"),
        ({"azp": str(uuid4())}, "bad_client"),
        ({"azp": str(SESSION_AUTH.api_client_id)}, "bad_client"),
        ({"azp": "web"}, "bad_client"),
        ({"scp": "openid profile"}, "missing_scope"),
        ({"scp": "access_as_user_extra"}, "missing_scope"),
        ({"scp": ["access_as_user"]}, "missing_scope"),
        ({"oid": "not-a-uuid"}, "bad_oid"),
        ({"oid": 7}, "bad_oid"),
        ({"roles": "User"}, "bad_roles"),
        ({"roles": ["User", 1]}, "bad_roles"),
        ({"roles": None}, "bad_roles"),
    ],
)
async def test_t004_each_claim_check_has_its_reason(
    validator: TokenValidator, alice: SharedPrincipal, overrides: dict[str, Any], reason: str
) -> None:
    with pytest.raises(InvalidToken) as caught:
        await validator.validate(bearer(mint_token(alice, **overrides)))
    assert caught.value.reason == reason
    assert caught.value.status == 401


async def test_the_scope_may_hold_other_scopes(
    validator: TokenValidator, alice: SharedPrincipal
) -> None:
    token = mint_token(alice, scp="openid access_as_user profile")
    assert (await validator.validate(bearer(token))).oid == alice.oid


@pytest.mark.parametrize("roles", [[], ["Reader"], OMIT])
async def test_t004_a_token_without_either_role_is_role_not_assigned(
    validator: TokenValidator, alice: SharedPrincipal, roles: object
) -> None:
    with pytest.raises(RoleNotAssigned) as caught:
        await validator.validate(bearer(mint_token(alice, roles=roles)))
    assert caught.value.status == 403
    assert caught.value.reason == "no_role"


async def test_t004_the_principal_without_roles_is_role_not_assigned(
    validator: TokenValidator, erin: SharedPrincipal
) -> None:
    with pytest.raises(RoleNotAssigned):
        await validator.validate(bearer(erin.token()))


# --- T-005: the test client -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("environment", "accepted"),
    [
        (EnvironmentName.DEV, True),
        (EnvironmentName.LOAD, True),
        (EnvironmentName.LOCAL, True),
        (EnvironmentName.PROD, False),
    ],
)
async def test_t005_the_test_client_is_accepted_only_outside_prod(
    keys: RecordingKeys, alice: SharedPrincipal, environment: EnvironmentName, accepted: bool
) -> None:
    validator = TokenValidator(SETTINGS, environment=environment, keys=keys)
    token = mint_token(alice, azp=str(SESSION_AUTH.test_client_id))
    if accepted:
        assert (await validator.validate(bearer(token))).client == "test"
    else:
        assert await reason_of(validator, bearer(token)) == "bad_client"


async def test_t005_the_test_client_is_refused_without_its_setting(
    keys: RecordingKeys, alice: SharedPrincipal
) -> None:
    settings = AuthSettings(
        auth_tenant_id=SESSION_AUTH.tenant_id,
        auth_api_client_id=SESSION_AUTH.api_client_id,
        auth_web_client_id=SESSION_AUTH.web_client_id,
        auth_test_client_id=None,
    )
    validator = TokenValidator(settings, environment=EnvironmentName.DEV, keys=keys)
    assert dict(validator.allowed_clients) == {SESSION_AUTH.web_client_id: "web"}
    token = mint_token(alice, azp=str(SESSION_AUTH.test_client_id))
    assert await reason_of(validator, bearer(token)) == "bad_client"


# --- Telemetry -----------------------------------------------------------------------------------


async def test_a_validation_is_traced_and_counted_without_the_token(
    validator: TokenValidator, captured: Any, alice: SharedPrincipal
) -> None:
    token = mint_token(alice)
    await validator.validate(bearer(token))
    await reason_of(validator, bearer(mint_token(alice, scp="openid")))
    await reason_of(validator, bearer(mint_token(alice, azp=str(uuid4()))))
    with pytest.raises(RoleNotAssigned):
        await validator.validate(bearer(mint_token(alice, roles=[])))

    assert captured.span_attributes("docchat.auth.validate_token") == [
        {"docchat.auth.outcome": "ok", "docchat.auth.client": "web"},
        {
            "docchat.auth.outcome": "invalid",
            "docchat.auth.reason": "missing_scope",
            "docchat.auth.client": "web",
        },
        {
            "docchat.auth.outcome": "invalid",
            "docchat.auth.reason": "bad_client",
            "docchat.auth.client": "unknown",
        },
        {
            "docchat.auth.outcome": "no_role",
            "docchat.auth.reason": "no_role",
            "docchat.auth.client": "web",
        },
    ]
    assert captured.points("docchat.auth.validations") == {
        (("client", "web"), ("outcome", "ok"), ("reason", "none")): 1,
        (("client", "web"), ("outcome", "invalid"), ("reason", "missing_scope")): 1,
        (("client", "unknown"), ("outcome", "invalid"), ("reason", "bad_client")): 1,
        (("client", "web"), ("outcome", "no_role"), ("reason", "no_role")): 1,
    }
    for span in captured.finished_spans("docchat.auth.validate_token"):
        assert span.events == ()
        assert token not in repr(dict(span.attributes or {}))


async def test_an_unavailable_key_set_is_traced(captured: Any, alice: SharedPrincipal) -> None:
    validator = TokenValidator(SETTINGS, environment=EnvironmentName.DEV, keys=UnavailableKeys({}))
    with pytest.raises(AuthenticationUnavailable):
        await validator.validate(bearer(mint_token(alice)))
    assert captured.span_attributes("docchat.auth.validate_token") == [
        {
            "docchat.auth.outcome": "unavailable",
            "docchat.auth.reason": "keys_unavailable",
            "docchat.auth.client": "unknown",
        }
    ]


# --- T-006: identity only from the validated claims ---------------------------------------------

_FORBIDDEN = re.compile(r"x-ms-client-principal|x-ms-token", re.IGNORECASE)


def _source_roots() -> list[Path]:
    for parent in Path(__file__).resolve().parents:
        if (parent / "packages" / "core" / "src").is_dir():
            return [parent / "packages" / "core" / "src", parent / "apps" / "api" / "src"]
    raise FileNotFoundError("the repository root wasn't found above the tests")


def test_t006_no_source_file_reads_the_platform_principal_headers() -> None:
    offenders = [
        str(path)
        for root in _source_roots()
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".json", ".toml", ".cfg", ".txt", ".md"}
        and _FORBIDDEN.search(path.read_text(encoding="utf-8", errors="replace"))
    ]
    assert offenders == []


def test_t006_the_validator_reads_the_authorization_header_alone() -> None:
    assert list(inspect.signature(TokenValidator.validate).parameters) == ["self", "authorization"]
