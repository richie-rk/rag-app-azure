from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Literal
from uuid import uuid4

import httpx
import jwt
import pytest
import respx
from azure.core.exceptions import HttpResponseError, ServiceRequestError

from docchat_core.auth import (
    AuthenticationFailure,
    AuthSettings,
    StaticSigningKeyProvider,
    TokenValidator,
)
from docchat_core.settings import EnvironmentName
from docchat_testing import tokens
from docchat_testing.fixtures.identities import PrincipalName, SharedPrincipal, make_principals
from docchat_testing.tokens import (
    OMIT,
    SESSION_AUTH,
    CredentialsExpiredError,
    DeployedTokens,
    RopcError,
    RopcRequest,
    SecretRecord,
    SigningKeys,
    TokenClaimsError,
    TokenStepError,
    VaultForbiddenError,
    VaultUnreachableError,
    check_token_claims,
    load_passwords,
    mint_token,
    session_signing_keys,
    token_claims,
)

REQUIRED = ["exp", "nbf", "iat", "iss", "aud", "sub", "oid", "tid", "azp", "scp", "ver"]

SESSION_SETTINGS = AuthSettings(
    auth_tenant_id=SESSION_AUTH.tenant_id,
    auth_api_client_id=SESSION_AUTH.api_client_id,
    auth_web_client_id=SESSION_AUTH.web_client_id,
    auth_test_client_id=SESSION_AUTH.test_client_id,
)
"""The API settings `fixtures/api.py` gives the app under test, from the minted tokens' IDs."""


async def validate(token: str, keys: SigningKeys | None = None) -> str | None:
    """The API's validator on `token` with the session's key set (or `keys`): the failure reason,
    or `None` for a token it accepts."""
    provider = StaticSigningKeyProvider((keys or session_signing_keys()).public_keys())
    validator = TokenValidator(SESSION_SETTINGS, environment=EnvironmentName.DEV, keys=provider)
    try:
        await validator.validate(f"Bearer {token}")
    except AuthenticationFailure as refused:
        return refused.reason
    return None


@pytest.fixture(scope="module")
def principals() -> dict[PrincipalName, SharedPrincipal]:
    session_signing_keys()
    return make_principals()


@pytest.fixture(scope="module")
def foreign() -> SigningKeys:
    """Another key pair, generated outside the event loop."""
    return SigningKeys()


async def test_t570_minted_tokens_pass_the_validator(
    principals: dict[PrincipalName, SharedPrincipal],
) -> None:
    names: tuple[PrincipalName, ...] = ("alice", "bob", "carol", "dave")
    for name in names:
        assert await validate(mint_token(principals[name])) is None
    claims = jwt.decode(mint_token(principals["carol"]), options={"verify_signature": False})
    assert claims["roles"] == ["Admin"]
    assert claims["oid"] == str(principals["carol"].oid)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"exp": int(time.time()) - 3600}, "expired"),
        ({"nbf": int(time.time()) + 3600}, "not_yet_valid"),
        ({"iat": "not-a-time"}, "bad_iat"),
        ({"iss": "https://login.microsoftonline.com/other/v2.0"}, "bad_issuer"),
        ({"aud": str(uuid4())}, "bad_audience"),
        ({"oid": OMIT}, "missing_claim"),
        ({"scp": OMIT}, "missing_claim"),
        ({"ver": "1.0"}, "bad_version"),
        ({"tid": str(uuid4())}, "bad_tenant"),
        ({"azp": str(uuid4())}, "bad_client"),
        ({"scp": "openid profile"}, "missing_scope"),
        ({"oid": "not-a-uuid"}, "bad_oid"),
        ({"roles": "User"}, "bad_roles"),
        ({"roles": []}, "no_role"),
    ],
)
async def test_t570_each_claim_override_produces_its_reason(
    principals: dict[PrincipalName, SharedPrincipal], overrides: dict[str, object], reason: str
) -> None:
    assert await validate(mint_token(principals["alice"], **overrides)) == reason


async def test_t570_principal_without_roles_is_refused_with_no_role(
    principals: dict[PrincipalName, SharedPrincipal],
) -> None:
    token = mint_token(principals["erin"])
    assert "roles" not in jwt.decode(token, options={"verify_signature": False})
    assert await validate(token) == "no_role"


async def test_t570_the_test_client_token_is_accepted(
    principals: dict[PrincipalName, SharedPrincipal],
) -> None:
    token = mint_token(principals["alice"], azp=str(SESSION_AUTH.test_client_id))
    assert await validate(token) is None


async def test_t570_foreign_keys_give_unknown_kid_and_bad_signature(
    principals: dict[PrincipalName, SharedPrincipal], foreign: SigningKeys
) -> None:
    session = session_signing_keys()
    claims = token_claims(principals["alice"])
    assert await validate(foreign.sign(claims)) == "unknown_kid"
    forged = foreign.sign(claims, headers={"kid": session.kid})
    assert await validate(forged) == "bad_signature"
    hs256 = jwt.encode(claims, "x" * 32, algorithm="HS256", headers={"kid": session.kid})
    assert await validate(hs256) == "bad_alg"
    assert await validate(foreign.sign(claims), keys=foreign) is None


def test_signing_keys_publish_one_rs256_key() -> None:
    keys = session_signing_keys()
    assert session_signing_keys() is keys
    (jwk,) = keys.jwks()["keys"]
    assert jwk["kty"] == "RSA"
    assert jwk["kid"] == keys.kid
    assert jwk["use"] == "sig"
    assert jwk["alg"] == "RS256"
    assert "d" not in jwk
    header = jwt.get_unverified_header(keys.sign({"a": 1}))
    assert header == {"alg": "RS256", "kid": keys.kid, "typ": "JWT"}


def test_token_claims_carry_every_required_claim(
    principals: dict[PrincipalName, SharedPrincipal],
) -> None:
    claims = token_claims(principals["dave"], now=1_000)
    assert set(REQUIRED) <= set(claims)
    assert claims["iat"] == claims["nbf"] == 1_000
    assert claims["exp"] == 1_000 + tokens.TOKEN_LIFETIME_SECONDS
    assert claims["azp"] == str(SESSION_AUTH.web_client_id)
    assert claims["sub"] == tokens.subject_of(principals["dave"].oid)
    assert len(claims["sub"]) == 43
    assert claims["email"] == principals["dave"].email


def test_session_auth_ids_are_distinct() -> None:
    ids = {
        SESSION_AUTH.tenant_id,
        SESSION_AUTH.api_client_id,
        SESSION_AUTH.web_client_id,
        SESSION_AUTH.test_client_id,
    }
    assert len(ids) == 4
    assert repr(OMIT) == "OMIT"


# --- load_passwords --------------------------------------------------------------------------

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


class FirewallDouble:
    def __init__(self, exits: Mapping[str, Sequence[int]] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._exits = {action: list(codes) for action, codes in (exits or {}).items()}

    async def __call__(self, action: Literal["open", "close"], environment: str) -> int:
        self.calls.append((action, environment))
        codes = self._exits.get(action)
        return codes.pop(0) if codes else 0


class ReaderDouble:
    def __init__(self, outcomes: Sequence[Exception | None], expires_on: datetime | None) -> None:
        self.outcomes = list(outcomes)
        self.expires_on = expires_on
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def __call__(self, vault_uri: str, aliases: Sequence[str]) -> dict[str, SecretRecord]:
        self.calls.append((vault_uri, tuple(aliases)))
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is not None:
            raise outcome
        return {alias: SecretRecord(f"pw-{alias}", self.expires_on) for alias in aliases}


VAULT = "https://kv-docchat-tst-abc123.vault.azure.net/"


async def test_load_passwords_reads_inside_the_window() -> None:
    firewall = FirewallDouble()
    reader = ReaderDouble([], NOW + timedelta(days=20))
    passwords = await load_passwords(
        VAULT,
        ["admin", "member"],
        environment="dev",
        private_networking=False,
        firewall=firewall,
        reader=reader,
        now=lambda: NOW,
    )
    assert passwords == {"admin": "pw-admin", "member": "pw-member"}
    assert firewall.calls == [("open", "dev"), ("close", "dev")]
    assert reader.calls == [(VAULT, ("admin", "member"))]


async def test_load_passwords_opens_again_after_forbidden_by_firewall() -> None:
    firewall = FirewallDouble()
    reader = ReaderDouble([VaultForbiddenError()], None)
    passwords = await load_passwords(
        VAULT,
        ["admin"],
        environment="dev",
        private_networking=False,
        firewall=firewall,
        reader=reader,
        now=lambda: NOW,
    )
    assert passwords == {"admin": "pw-admin"}
    assert firewall.calls == [("open", "dev"), ("open", "dev"), ("close", "dev")]
    assert len(reader.calls) == 2


async def test_load_passwords_fails_unreachable_after_two_refusals() -> None:
    firewall = FirewallDouble()
    reader = ReaderDouble([VaultForbiddenError(), VaultForbiddenError()], None)
    with pytest.raises(VaultUnreachableError) as caught:
        await load_passwords(
            VAULT,
            ["admin"],
            environment="load",
            private_networking=False,
            firewall=firewall,
            reader=reader,
            now=lambda: NOW,
        )
    assert caught.value.code == "test_vault_unreachable"
    assert firewall.calls[-1] == ("close", "load")
    assert [call[0] for call in firewall.calls].count("open") == 2


async def test_load_passwords_fails_unreachable_when_open_fails() -> None:
    firewall = FirewallDouble({"open": [3]})
    reader = ReaderDouble([], None)
    with pytest.raises(VaultUnreachableError, match="exited 3"):
        await load_passwords(
            VAULT,
            ["admin"],
            environment="dev",
            private_networking=False,
            firewall=firewall,
            reader=reader,
            now=lambda: NOW,
        )
    assert firewall.calls == [("open", "dev")]
    assert reader.calls == []


async def test_load_passwords_warns_when_close_fails(capsys: pytest.CaptureFixture[str]) -> None:
    firewall = FirewallDouble({"close": [4]})
    reader = ReaderDouble([], None)
    passwords = await load_passwords(
        VAULT,
        ["admin"],
        environment="dev",
        private_networking=False,
        firewall=firewall,
        reader=reader,
        now=lambda: NOW,
    )
    assert passwords == {"admin": "pw-admin"}
    assert "::warning::firewall.py close --target vault exited 4" in capsys.readouterr().out


async def test_load_passwords_opens_nothing_under_private_networking() -> None:
    firewall = FirewallDouble()
    reader = ReaderDouble([], None)
    await load_passwords(
        VAULT,
        ["admin"],
        private_networking=True,
        firewall=firewall,
        reader=reader,
        now=lambda: NOW,
    )
    assert firewall.calls == []
    refused = ReaderDouble([VaultForbiddenError()], None)
    with pytest.raises(VaultUnreachableError, match="Repair the CI runner group"):
        await load_passwords(
            VAULT,
            ["admin"],
            private_networking=True,
            firewall=firewall,
            reader=refused,
            now=lambda: NOW,
        )
    assert firewall.calls == []


async def test_load_passwords_reads_the_networking_choice_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOCCHAT_CI_PRIVATE_NETWORKING", "true")
    firewall = FirewallDouble()
    await load_passwords(
        VAULT, ["admin"], firewall=firewall, reader=ReaderDouble([], None), now=lambda: NOW
    )
    assert firewall.calls == []
    monkeypatch.setenv("DOCCHAT_CI_PRIVATE_NETWORKING", "")
    monkeypatch.setenv("AZURE_ENV_NAME", "dev")
    await load_passwords(
        VAULT, ["admin"], firewall=firewall, reader=ReaderDouble([], None), now=lambda: NOW
    )
    assert firewall.calls == [("open", "dev"), ("close", "dev")]
    monkeypatch.delenv("AZURE_ENV_NAME")
    with pytest.raises(TokenStepError, match="AZURE_ENV_NAME"):
        await load_passwords(
            VAULT, ["admin"], firewall=firewall, reader=ReaderDouble([], None), now=lambda: NOW
        )


async def test_load_passwords_refuses_expired_and_warns_for_expiring(
    capsys: pytest.CaptureFixture[str],
) -> None:
    expired = ReaderDouble([], NOW - timedelta(seconds=1))
    with pytest.raises(CredentialsExpiredError, match="test-user-admin") as caught:
        await load_passwords(
            VAULT, ["admin"], private_networking=True, reader=expired, now=lambda: NOW
        )
    assert caught.value.code == "test_credentials_expired"
    soon = ReaderDouble([], NOW + timedelta(days=6))
    await load_passwords(VAULT, ["member"], private_networking=True, reader=soon, now=lambda: NOW)
    assert "::warning::test-user-member expires at 2026-11-05" in capsys.readouterr().out
    assert await load_passwords(VAULT, [], private_networking=True, reader=soon) == {}


# --- read_vault and the GitHub assertion ------------------------------------------------------


class SecretProperties:
    def __init__(self, expires_on: datetime | None) -> None:
        self.expires_on = expires_on


class Secret:
    def __init__(self, value: str | None) -> None:
        self.value = value
        self.properties = SecretProperties(NOW)


class SecretClientDouble:
    instances: ClassVar[list[SecretClientDouble]] = []
    failure: ClassVar[Exception | None] = None

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.names: list[str] = []
        SecretClientDouble.instances.append(self)

    async def __aenter__(self) -> SecretClientDouble:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def get_secret(self, name: str) -> Secret:
        self.names.append(name)
        if SecretClientDouble.failure is not None:
            raise SecretClientDouble.failure
        return Secret(f"value-of-{name}")


class CredentialDouble:
    instances: ClassVar[list[CredentialDouble]] = []

    def __init__(self, tenant_id: str, client_id: str, func: Any) -> None:
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.func = func
        self.closed = False
        CredentialDouble.instances.append(self)

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def vault_doubles(monkeypatch: pytest.MonkeyPatch) -> None:
    SecretClientDouble.instances = []
    SecretClientDouble.failure = None
    CredentialDouble.instances = []
    monkeypatch.setattr(tokens, "SecretClient", SecretClientDouble)
    monkeypatch.setattr(tokens, "ClientAssertionCredential", CredentialDouble)
    monkeypatch.setenv("AZURE_TENANT_ID", "tenant-1")
    monkeypatch.setenv("DOCCHAT_CI_TEST_CLIENT_ID", "citest-1")


async def assertion() -> str:
    return "assertion-1"


@pytest.mark.usefixtures("vault_doubles")
async def test_read_vault_reads_each_alias_with_a_fresh_credential() -> None:
    records = await tokens.read_vault(VAULT, ["admin", "norole"], assertion_source=assertion)
    assert records["admin"].value == "value-of-test-user-admin"
    assert "value-of" not in repr(records["admin"])
    (credential,) = CredentialDouble.instances
    assert (credential.tenant_id, credential.client_id) == ("tenant-1", "citest-1")
    assert credential.func() == "assertion-1"
    assert credential.closed
    (client,) = SecretClientDouble.instances
    assert client.names == ["test-user-admin", "test-user-norole"]
    assert client.kwargs["connection_timeout"] == 5
    assert client.kwargs["read_timeout"] == 30
    assert client.kwargs["vault_url"] == VAULT


class FirewallRefusalError(HttpResponseError):
    def __init__(self, status: int, text: str) -> None:
        super().__init__(message=text)
        self.status_code = status


@pytest.mark.usefixtures("vault_doubles")
async def test_read_vault_maps_refusals_and_network_failures() -> None:
    SecretClientDouble.failure = FirewallRefusalError(403, "Forbidden ForbiddenByFirewall")
    with pytest.raises(VaultForbiddenError):
        await tokens.read_vault(VAULT, ["admin"], assertion_source=assertion)
    SecretClientDouble.failure = FirewallRefusalError(403, "Forbidden: no access policy")
    with pytest.raises(HttpResponseError):
        await tokens.read_vault(VAULT, ["admin"], assertion_source=assertion)
    SecretClientDouble.failure = ServiceRequestError("name resolution failed")
    with pytest.raises(VaultUnreachableError):
        await tokens.read_vault(VAULT, ["admin"], assertion_source=assertion)
    assert all(credential.closed for credential in CredentialDouble.instances)


@respx.mock
async def test_fetch_github_assertion_asks_for_the_exchange_audience(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ACTIONS_ID_TOKEN_REQUEST_URL", "https://token.actions.example/x?api-version=2.0"
    )
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "runtime-token")
    route = respx.get(
        "https://token.actions.example/x",
        params={"api-version": "2.0", "audience": "api://AzureADTokenExchange"},
    ).mock(return_value=httpx.Response(200, json={"value": "oidc-assertion"}))
    assert await tokens.fetch_github_assertion() == "oidc-assertion"
    assert route.calls.last.request.headers["Authorization"] == "Bearer runtime-token"
    route.mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(TokenStepError, match="no token"):
        await tokens.fetch_github_assertion()


async def test_run_firewall_runs_the_script_from_the_repository_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    class Process:
        async def wait(self) -> int:
            return 3

    async def fake_exec(*args: str, **kwargs: Any) -> Process:
        calls.append((args, kwargs))
        return Process()

    monkeypatch.setattr("docchat_testing.tokens.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("docchat_testing.tokens.asyncio.create_subprocess_exec", fake_exec)
    assert await tokens.run_firewall("open", "load") == 3
    ((args, kwargs),) = calls
    assert args == (
        "/usr/bin/uv",
        "run",
        "--frozen",
        "python",
        "scripts/deploy/firewall.py",
        "open",
        "--target",
        "vault",
        "--env",
        "load",
    )
    assert kwargs["cwd"] == tokens.REPOSITORY_ROOT
    assert (tokens.REPOSITORY_ROOT / tokens.FIREWALL_SCRIPT).is_file()
    monkeypatch.setattr("docchat_testing.tokens.shutil.which", lambda name: None)
    with pytest.raises(TokenStepError, match="uv"):
        await tokens.run_firewall("close", "dev")


# --- ROPC ------------------------------------------------------------------------------------

ROPC = RopcRequest(
    tenant_id="tenant-1",
    client_id="test-client-1",
    api_scope="api://tenant-1/docchat-dev-abc-api/access_as_user",
    user_domain="contoso.example",
)
TOKEN_URL = "https://login.microsoftonline.com/tenant-1/oauth2/v2.0/token"


class Sleeps:
    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


@respx.mock
async def test_acquire_ropc_token_posts_the_password_grant() -> None:
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "token-1", "token_type": "Bearer"})
    )
    assert await tokens.acquire_ropc_token("load-07", password="pw", request=ROPC) == "token-1"
    form = dict(httpx.QueryParams(route.calls.last.request.content.decode()))
    assert form == {
        "grant_type": "password",
        "client_id": "test-client-1",
        "scope": "api://tenant-1/docchat-dev-abc-api/access_as_user openid profile",
        "username": "docchat-load-07@contoso.example",
        "password": "pw",
    }


@pytest.mark.parametrize(
    ("code", "fragment"),
    [
        (50126, "run runbook Test-user password rotation"),
        (50076, "Conditional Access exclusion is missing"),
        (50079, "multi-factor sign-in demanded"),
        (53003, "runbook Run the load test"),
        (50105, "user not assigned to the application"),
        (65001, "admin consent missing for the test client"),
        (90094, "admin consent missing for the test client"),
    ],
)
@respx.mock
async def test_acquire_ropc_token_names_the_runbook(code: int, fragment: str) -> None:
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            400,
            json={
                "error": "invalid_grant",
                "error_description": f"AADSTS{code}: docchat-test-admin@contoso.example ...",
                "error_codes": [code],
            },
        )
    )
    with pytest.raises(RopcError) as caught:
        await tokens.acquire_ropc_token("admin", password="pw", request=ROPC, sleep=Sleeps())
    assert fragment in str(caught.value)
    assert "contoso" not in str(caught.value)
    assert caught.value.aadsts == code
    assert route.call_count == 1


@respx.mock
async def test_acquire_ropc_token_reads_the_code_from_the_description() -> None:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            400, json={"error": "x", "error_description": "AADSTS700016: y"}
        )
    )
    with pytest.raises(RopcError, match=r"HTTP 400 x \(AADSTS700016\)"):
        await tokens.acquire_ropc_token("admin", password="pw", request=ROPC)
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(401, text="nope"))
    with pytest.raises(RopcError, match="no AADSTS code"):
        await tokens.acquire_ropc_token("admin", password="pw", request=ROPC)


@respx.mock
async def test_acquire_ropc_token_retries_server_errors_and_connection_failures() -> None:
    sleeps = Sleeps()
    route = respx.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(503),
            httpx.ConnectError("reset"),
            httpx.Response(200, json={"access_token": "token-2"}),
        ]
    )
    token = await tokens.acquire_ropc_token("member", password="pw", request=ROPC, sleep=sleeps)
    assert token == "token-2"
    assert route.call_count == 3
    assert sleeps.waits == [2.0, 4.0]
    route.mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(TokenStepError, match="didn't answer"):
        await tokens.acquire_ropc_token("member", password="pw", request=ROPC, sleep=sleeps)
    route.mock(return_value=httpx.Response(502))
    with pytest.raises(RopcError, match="HTTP 502"):
        await tokens.acquire_ropc_token("member", password="pw", request=ROPC, sleep=sleeps)


async def test_acquire_ropc_token_refuses_unknown_alias_and_reads_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(TokenStepError, match="unknown test-user alias"):
        await tokens.acquire_ropc_token("guest", password="pw", request=ROPC)
    for name, value in {
        "AZURE_TENANT_ID": "t",
        "TEST_CLIENT_ID": "c",
        "API_SCOPE": "s",
        "DOCCHAT_TEST_USER_DOMAIN": "d",
    }.items():
        monkeypatch.setenv(name, value)
    assert RopcRequest.from_environment() == RopcRequest(
        tenant_id="t", client_id="c", api_scope="s", user_domain="d"
    )


# --- Claims and DeployedTokens -----------------------------------------------------------------

API_CLIENT = str(uuid4())
OTHER_KEYS = SigningKeys()


def ropc_token(
    roles: list[str] | None, *, exp: int, aud: str = API_CLIENT, scp: str = "access_as_user openid"
) -> str:
    claims: dict[str, Any] = {"aud": aud, "scp": scp, "exp": exp, "ver": "2.0"}
    if roles is not None:
        claims["roles"] = roles
    return OTHER_KEYS.sign(claims)


def test_check_token_claims_compares_with_the_alias_row() -> None:
    later = int(time.time()) + 3600
    assert check_token_claims("admin", ropc_token(["Admin"], exp=later), api_client_id=API_CLIENT)
    assert check_token_claims("norole", ropc_token(None, exp=later), api_client_id=API_CLIENT)
    assert check_token_claims("load-20", ropc_token(["User"], exp=later), api_client_id=API_CLIENT)
    bad = [
        ("admin", ropc_token(["User"], exp=later), "roles isn't"),
        ("norole", ropc_token(["User"], exp=later), "no app role"),
        ("member", ropc_token(["User"], exp=later, aud="other"), "aud"),
        ("member", ropc_token(["User"], exp=later, scp="openid"), "scp lacks"),
    ]
    for alias, token, fragment in bad:
        with pytest.raises(TokenClaimsError, match=fragment):
            check_token_claims(alias, token, api_client_id=API_CLIENT)


async def test_deployed_tokens_mask_check_and_refresh(capsys: pytest.CaptureFixture[str]) -> None:
    now = [1_000_000.0]
    issued: list[str] = []

    async def acquire(alias: str, password: str) -> str:
        assert password == f"pw-{alias}"
        token = ropc_token(["Admin"] if alias == "admin" else ["User"], exp=int(now[0]) + 3600)
        issued.append(token)
        return token

    held = DeployedTokens(
        {"admin": "pw-admin", "member": "pw-member"},
        api_client_id=API_CLIENT,
        acquire=acquire,
        clock=lambda: now[0],
    )
    assert "pw-" not in repr(held)
    assert held.aliases == ("admin", "member")
    await held.acquire_all()
    out = capsys.readouterr().out.splitlines()
    assert out == [f"::add-mask::{issued[0]}", f"::add-mask::{issued[1]}"]
    assert await held.token("admin") == issued[0]
    now[0] += 3600 - 299
    assert await held.token("admin") == issued[2]
    assert held.claims("admin")["roles"] == ["Admin"]
    with pytest.raises(TokenStepError, match="no password"):
        await held.token("outsider")


async def test_deployed_tokens_fail_before_any_test_on_a_wrong_role() -> None:
    async def acquire(alias: str, password: str) -> str:
        del alias, password
        return ropc_token(["User"], exp=int(time.time()) + 3600)

    held = DeployedTokens({"admin": "pw"}, api_client_id=API_CLIENT, acquire=acquire)
    with pytest.raises(TokenClaimsError):
        await held.acquire_all()


def test_alias_tables_cover_section_6_1() -> None:
    assert len(tokens.ALIAS_USERS) == 25
    assert set(tokens.ALIAS_USERS) == set(tokens.ALIAS_ROLES)
    assert tokens.ALIAS_ROLES["norole"] is None
    assert tokens.ALIAS_ROLES["load-admin"] == ("Admin",)
    assert tokens.ALIAS_USERS["load-01"] == "docchat-load-01"


def test_secret_record_hides_its_value() -> None:
    assert "secret" not in repr(SecretRecord("secret", None))


@respx.mock
async def test_acquire_with_sends_the_password_to_another_client() -> None:
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            401,
            json={"error": "invalid_client", "error_codes": [7000218], "error_description": "x"},
        )
    )

    async def acquire(alias: str, password: str) -> str:
        del alias, password
        return ropc_token(["User"], exp=int(time.time()) + 3600)

    held = DeployedTokens({"member": "pw-member"}, api_client_id=API_CLIENT, acquire=acquire)
    web = RopcRequest(
        tenant_id="tenant-1", client_id="web-1", api_scope=ROPC.api_scope, user_domain="d"
    )
    with pytest.raises(RopcError) as caught:
        await held.acquire_with("member", web)
    assert caught.value.aadsts == 7000218
    form = dict(httpx.QueryParams(route.calls.last.request.content.decode()))
    assert (form["client_id"], form["password"]) == ("web-1", "pw-member")
    with pytest.raises(TokenStepError, match="no password"):
        await held.acquire_with("admin", web)


@respx.mock
async def test_acquire_with_returns_the_masked_id_token_on_request(
    capsys: pytest.CaptureFixture[str],
) -> None:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "access-1", "id_token": "id-1"})
    )

    async def acquire(alias: str, password: str) -> str:
        del alias, password
        return ropc_token(["User"], exp=int(time.time()) + 3600)

    held = DeployedTokens({"member": "pw-member"}, api_client_id=API_CLIENT, acquire=acquire)
    assert await held.acquire_with("member", ROPC, returning="id_token") == "id-1"
    assert capsys.readouterr().out == "::add-mask::id-1\n"
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "a"}))
    with pytest.raises(TokenStepError, match="no id_token"):
        await held.acquire_with("member", ROPC, returning="id_token")


def test_suite_aliases_follow_section_6_5() -> None:
    assert tokens.suite_aliases("dev") == {
        "admin": "admin",
        "member": "member",
        "outsider": "outsider",
        "norole": "norole",
    }
    assert "norole" not in tokens.suite_aliases("dev", "graph_default_access_refused")
    assert tokens.suite_aliases("load") == {
        "admin": "load-admin",
        "member": "load-01",
        "outsider": "load-02",
    }
