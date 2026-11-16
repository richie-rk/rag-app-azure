"""Test tokens (`84-testing.md` sections 1.4, 2.5 and 6.4; `11-api.md` section 1.2).

In-process suites: one RS256 2,048-bit key pair per test session and its key set, which the API's
injectable signing-key provider serves, and `mint_token(principal, **claim_overrides)`, which signs
the v2 access tokens the API's validator accepts. An override replaces one claim (`OMIT` removes
it), so each validator failure reason has a token that produces it.

Deployed and load suites: `load_passwords(vault_uri, aliases)` reads the test-user passwords from
the shared test vault inside the job's read window, `acquire_ropc_token(alias, password=...)`
exchanges one for a token through the environment's test client, and `DeployedTokens` holds the
tokens in memory, checks their claims and re-acquires each one near its expiry. Passwords never
leave the process's memory, and every token is masked in the job log before anything else is
printed.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import os
import re
import secrets
import shutil
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any, Final, Literal, Protocol, override
from uuid import UUID

import httpx
import jwt
from azure.core.exceptions import HttpResponseError, ServiceRequestError
from azure.identity.aio import ClientAssertionCredential
from azure.keyvault.secrets.aio import SecretClient
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from docchat_core.clients.tls import TLS_CONTEXT

# --- In-process tokens ------------------------------------------------------------------------

RSA_KEY_BITS: Final = 2048
ALGORITHM: Final = "RS256"
API_SCOPE_NAME: Final = "access_as_user"
TOKEN_VERSION: Final = "2.0"
TOKEN_LIFETIME_SECONDS: Final = 3600
LOGIN_HOST: Final = "https://login.microsoftonline.com"


@dataclass(frozen=True, slots=True, kw_only=True)
class MintSettings:
    """The tenant and registrations minted tokens name. `fixtures/api.py` gives the app under
    test the same values as its `DOCCHAT_AUTH_*` settings, so the tokens validate there."""

    tenant_id: UUID
    api_client_id: UUID
    web_client_id: UUID
    test_client_id: UUID

    @property
    def issuer(self) -> str:
        return f"{LOGIN_HOST}/{self.tenant_id}/v2.0"


SESSION_AUTH: Final = MintSettings(
    tenant_id=UUID("5b0c2c8e-7a51-4c3e-9d6f-1f2a3b4c5d6e"),
    api_client_id=UUID("8e3f1a52-4c6d-4b7e-a9f0-2c1d3e4f5a6b"),
    web_client_id=UUID("1d2e3f4a-5b6c-4d7e-8f90-a1b2c3d4e5f6"),
    test_client_id=UUID("9a8b7c6d-5e4f-4a3b-b2c1-d0e9f8a7b6c5"),
)
"""Fixed, made-up IDs: three distinct registrations in one tenant, as the API's start-up check
requires; no real tenant or registration has them."""


class _Omit:
    """The type of `OMIT`."""

    @override
    def __repr__(self) -> str:
        return "OMIT"


OMIT: Final = _Omit()
"""A claim override that removes the claim, for the validator's `missing_claim` reason."""


class TokenPrincipal(Protocol):
    """What a minted token says about its subject; `fixtures/identities.py` defines the five
    shared principals."""

    @property
    def oid(self) -> UUID: ...

    @property
    def roles(self) -> tuple[str, ...] | None: ...

    @property
    def display_name(self) -> str: ...

    @property
    def email(self) -> str: ...


class SigningKeys:
    """One RS256 key pair, its `kid` and its key set."""

    def __init__(self) -> None:
        self._private_key = rsa.generate_private_key(public_exponent=65537, key_size=RSA_KEY_BITS)
        self.kid: Final = secrets.token_hex(16)
        public_jwk: dict[str, Any] = RSAAlgorithm.to_jwk(
            self._private_key.public_key(), as_dict=True
        )
        self._jwk: Final[dict[str, Any]] = public_jwk | {
            "kid": self.kid,
            "use": "sig",
            "alg": ALGORITHM,
        }

    def jwks(self) -> dict[str, Any]:
        """The key set as Entra's discovery endpoint serves it: `{"keys": [<the public key>]}`."""
        return {"keys": [dict(self._jwk)]}

    def public_keys(self) -> dict[str, jwt.PyJWK]:
        """`kid` to public key, the form a static signing-key provider serves."""
        return {self.kid: jwt.PyJWK(dict(self._jwk), algorithm=ALGORITHM)}

    def sign(self, claims: Mapping[str, Any], *, headers: Mapping[str, Any] | None = None) -> str:
        """A compact JWS of `claims` with header `{alg: RS256, kid, typ: JWT}` plus `headers`."""
        header = {"kid": self.kid, "typ": "JWT"} | dict(headers or {})
        return jwt.encode(dict(claims), self._private_key, algorithm=ALGORITHM, headers=header)


@cache
def session_signing_keys() -> SigningKeys:
    """The test session's key pair, created on first use. Generating it takes tens of
    milliseconds, so a synchronous session fixture (`fixtures/identities.py`) creates it before
    any test's event loop runs."""
    return SigningKeys()


def subject_of(oid: UUID) -> str:
    """A stable pairwise `sub` for a principal: 43 base64url characters, as Entra issues."""
    digest = hashlib.sha256(f"docchat-test-sub:{oid}".encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def token_claims(
    principal: TokenPrincipal, *, settings: MintSettings = SESSION_AUTH, now: int | None = None
) -> dict[str, Any]:
    """The claims of a valid v2 access token for the API from the web client
    (`11-api.md` section 1.2): every required claim, `roles` only when the principal has an app
    role, and the display claims `name`, `email` and `preferred_username`."""
    issued_at = int(time.time()) if now is None else now
    claims: dict[str, Any] = {
        "aud": str(settings.api_client_id),
        "iss": settings.issuer,
        "iat": issued_at,
        "nbf": issued_at,
        "exp": issued_at + TOKEN_LIFETIME_SECONDS,
        "sub": subject_of(principal.oid),
        "oid": str(principal.oid),
        "tid": str(settings.tenant_id),
        "azp": str(settings.web_client_id),
        "scp": API_SCOPE_NAME,
        "ver": TOKEN_VERSION,
        "name": principal.display_name,
        "email": principal.email,
        "preferred_username": principal.email,
    }
    if principal.roles is not None:
        claims["roles"] = list(principal.roles)
    return claims


def mint_token(principal: TokenPrincipal, **claim_overrides: object) -> str:
    """A token for `principal` signed with the session key pair; each override replaces its
    claim, and `OMIT` removes it."""
    claims = token_claims(principal)
    for name, value in claim_overrides.items():
        if value is OMIT:
            claims.pop(name, None)
        else:
            claims[name] = value
    return session_signing_keys().sign(claims)


# --- Deployed and load suites: the token step -------------------------------------------------

ALIAS_USERS: Final[Mapping[str, str]] = {
    "admin": "docchat-test-admin",
    "member": "docchat-test-member",
    "outsider": "docchat-test-outsider",
    "norole": "docchat-test-norole",
    **{f"load-{n:02d}": f"docchat-load-{n:02d}" for n in range(1, 21)},
    "load-admin": "docchat-load-admin",
}
"""Each alias's user name before `@<DOCCHAT_TEST_USER_DOMAIN>` (`84-testing.md` section 6.1)."""

ALIAS_ROLES: Final[Mapping[str, tuple[str, ...] | None]] = {
    "admin": ("Admin",),
    "member": ("User",),
    "outsider": ("User",),
    "norole": None,
    **{f"load-{n:02d}": ("User",) for n in range(1, 21)},
    "load-admin": ("Admin",),
}
"""The `roles` claim each alias's token carries; `None`: the token has no `roles` claim."""

SECRET_PREFIX: Final = "test-user-"
OIDC_AUDIENCE: Final = "api://AzureADTokenExchange"
OIDC_URL_VARIABLE: Final = "ACTIONS_ID_TOKEN_REQUEST_URL"
OIDC_TOKEN_VARIABLE: Final = "ACTIONS_ID_TOKEN_REQUEST_TOKEN"
TENANT_VARIABLE: Final = "AZURE_TENANT_ID"
CI_CLIENT_VARIABLE: Final = "DOCCHAT_CI_TEST_CLIENT_ID"
PRIVATE_NETWORKING_VARIABLE: Final = "DOCCHAT_CI_PRIVATE_NETWORKING"
ENVIRONMENT_VARIABLE: Final = "AZURE_ENV_NAME"
TEST_CLIENT_VARIABLE: Final = "TEST_CLIENT_ID"
API_SCOPE_VARIABLE: Final = "API_SCOPE"
USER_DOMAIN_VARIABLE: Final = "DOCCHAT_TEST_USER_DOMAIN"

GITHUB_OIDC_TIMEOUT_SECONDS: Final = 10.0
KEY_VAULT_CONNECTION_TIMEOUT_SECONDS: Final = 5
KEY_VAULT_READ_TIMEOUT_SECONDS: Final = 30
VAULT_READS_SECONDS: Final = 120.0
"""All reads of one `load_passwords` attempt together, so a read window stays minutes long."""
ROPC_TIMEOUT_SECONDS: Final = 15.0
ROPC_RETRIES: Final = 2
ROPC_RETRY_WAIT_SECONDS: Final = 2.0
EXPIRY_WARNING: Final = timedelta(days=7)
REFRESH_MARGIN_SECONDS: Final = 300
HTTP_SERVER_ERROR: Final = 500
HTTP_FORBIDDEN: Final = 403

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[4]
FIREWALL_SCRIPT: Final = "scripts/deploy/firewall.py"


class TokenStepError(Exception):
    """The token step failed before any test ran; the message names no secret."""

    code: str | None = None


class VaultUnreachableError(TokenStepError):
    code = "test_vault_unreachable"


class CredentialsExpiredError(TokenStepError):
    code = "test_credentials_expired"


class VaultForbiddenError(Exception):
    """A vault read answered `403 ForbiddenByFirewall`: the runner rule isn't (or no longer) in
    the vault's rules."""


class RopcError(TokenStepError):
    """Entra refused an alias's password grant; the message names the runbook that fixes it."""

    def __init__(self, alias: str, aadsts: int | None, message: str) -> None:
        super().__init__(f"{alias}: {message}")
        self.alias = alias
        self.aadsts = aadsts


class TokenClaimsError(TokenStepError):
    """A token's `aud`, `scp` or `roles` doesn't match its alias's row."""


@dataclass(frozen=True, slots=True)
class SecretRecord:
    """One test password read from the vault, with its expiry."""

    value: str = field(repr=False)
    expires_on: datetime | None


def _required(variable: str) -> str:
    value = os.environ.get(variable, "")
    if not value:
        raise TokenStepError(f"{variable} is not set")
    return value


def _emit(line: str) -> None:
    # Workflow commands are read from standard output.
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


async def fetch_github_assertion(client: httpx.AsyncClient | None = None) -> str:
    """The job's GitHub OIDC token for `api://AzureADTokenExchange`."""
    url = _required(OIDC_URL_VARIABLE)
    bearer = _required(OIDC_TOKEN_VARIABLE)
    owned = client is None
    http = (
        httpx.AsyncClient(
            timeout=httpx.Timeout(GITHUB_OIDC_TIMEOUT_SECONDS),
            follow_redirects=False,
            verify=TLS_CONTEXT,
        )
        if client is None
        else client
    )
    try:
        response = await http.get(
            f"{url}&audience={OIDC_AUDIENCE}", headers={"Authorization": f"Bearer {bearer}"}
        )
        response.raise_for_status()
        value = response.json().get("value")
    finally:
        if owned:
            await http.aclose()
    if not isinstance(value, str) or not value:
        raise TokenStepError("the GitHub OIDC answer has no token")
    return value


def _forbidden_by_firewall(error: HttpResponseError) -> bool:
    if error.status_code != HTTP_FORBIDDEN:
        return False
    texts = [str(error.message or "")]
    if error.error is not None:
        texts += [str(error.error.code or ""), str(error.error.innererror or "")]
    if error.response is not None:
        with contextlib.suppress(UnicodeDecodeError, ValueError):
            texts.append(error.response.text())
    return any("ForbiddenByFirewall" in text for text in texts)


type AssertionSource = Callable[[], Awaitable[str]]


async def read_vault(
    vault_uri: str,
    aliases: Sequence[str],
    *,
    assertion_source: AssertionSource = fetch_github_assertion,
) -> dict[str, SecretRecord]:
    """One attempt at reading `test-user-<alias>` for every alias, with a credential built from a
    freshly fetched assertion; all reads together are bounded at 120 s, and the client and the
    credential are closed afterwards."""
    assertion = await assertion_source()
    credential = ClientAssertionCredential(
        _required(TENANT_VARIABLE),
        client_id=_required(CI_CLIENT_VARIABLE),
        func=lambda: assertion,
    )
    records: dict[str, SecretRecord] = {}
    try:
        async with SecretClient(
            vault_url=vault_uri,
            credential=credential,
            connection_timeout=KEY_VAULT_CONNECTION_TIMEOUT_SECONDS,
            read_timeout=KEY_VAULT_READ_TIMEOUT_SECONDS,
        ) as client:
            async with asyncio.timeout(VAULT_READS_SECONDS):
                for alias in aliases:
                    secret = await client.get_secret(f"{SECRET_PREFIX}{alias}")
                    if not secret.value:
                        raise TokenStepError(f"{SECRET_PREFIX}{alias} holds no value")
                    records[alias] = SecretRecord(secret.value, secret.properties.expires_on)
    except HttpResponseError as error:
        if _forbidden_by_firewall(error):
            raise VaultForbiddenError from error
        raise
    except (ServiceRequestError, TimeoutError) as error:
        raise VaultUnreachableError("the test vault didn't answer") from error
    finally:
        await credential.close()
    return records


type FirewallRunner = Callable[[Literal["open", "close"], str], Awaitable[int]]
type VaultReader = Callable[[str, Sequence[str]], Awaitable[dict[str, SecretRecord]]]


async def run_firewall(action: Literal["open", "close"], environment: str) -> int:
    """`uv run --frozen python scripts/deploy/firewall.py <action> --target vault --env <env>`
    as a child process from the repository root; its JSON lines go to the job log."""
    uv = shutil.which("uv")
    if uv is None:
        raise TokenStepError("uv is not on PATH")
    process = await asyncio.create_subprocess_exec(
        uv,
        "run",
        "--frozen",
        "python",
        FIREWALL_SCRIPT,
        action,
        "--target",
        "vault",
        "--env",
        environment,
        cwd=REPOSITORY_ROOT,
    )
    return await process.wait()


async def _open_window(firewall: FirewallRunner, environment: str) -> None:
    exit_code = await firewall("open", environment)
    if exit_code != 0:
        raise VaultUnreachableError(f"firewall.py open --target vault exited {exit_code}")


async def _read_in_window(
    vault_uri: str,
    aliases: Sequence[str],
    environment: str,
    firewall: FirewallRunner,
    reader: VaultReader,
) -> dict[str, SecretRecord]:
    await _open_window(firewall, environment)
    try:
        try:
            return await reader(vault_uri, aliases)
        except VaultForbiddenError:
            # Another job's concurrent write can undo this job's rule; open writes it again.
            await _open_window(firewall, environment)
            try:
                return await reader(vault_uri, aliases)
            except VaultForbiddenError as error:
                raise VaultUnreachableError(
                    "the test vault refused the runner after two opens (ForbiddenByFirewall)"
                ) from error
    finally:
        exit_code = await firewall("close", environment)
        if exit_code != 0:
            # The job's always-run close step removes the rule and fails the job if it can't.
            _emit(f"::warning::firewall.py close --target vault exited {exit_code}")


def _check_expiry(records: Mapping[str, SecretRecord], now: datetime) -> dict[str, str]:
    expired = sorted(
        alias
        for alias, record in records.items()
        if record.expires_on is not None and record.expires_on <= now
    )
    if expired:
        names = ", ".join(f"{SECRET_PREFIX}{alias}" for alias in expired)
        raise CredentialsExpiredError(f"expired test passwords: {names}")
    for alias, record in sorted(records.items()):
        if record.expires_on is not None and record.expires_on - now <= EXPIRY_WARNING:
            _emit(
                f"::warning::{SECRET_PREFIX}{alias} expires at "
                f"{record.expires_on.astimezone(UTC).isoformat()}"
            )
    return {alias: record.value for alias, record in records.items()}


async def load_passwords(
    vault_uri: str,
    aliases: Sequence[str],
    *,
    environment: str | None = None,
    private_networking: bool | None = None,
    firewall: FirewallRunner = run_firewall,
    reader: VaultReader | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, str]:
    """Every alias's test password, read from the shared test vault (`84-testing.md` section 6.4
    items 1 to 3).

    By default the vault answers this runner only inside the read window: `firewall.py open` runs
    before the first read and `close` in a `finally` after the last; a read refused with
    `403 ForbiddenByFirewall` runs `open` once more and reads again. Under the private-networking
    option (`DOCCHAT_CI_PRIVATE_NETWORKING=true`) nothing is opened. A vault that can't be read
    fails with `test_vault_unreachable`, an expired password with `test_credentials_expired`, and a
    password expiring within 7 days adds a `::warning::` annotation.
    """
    if not aliases:
        return {}
    read: VaultReader = reader if reader is not None else read_vault
    private = (
        os.environ.get(PRIVATE_NETWORKING_VARIABLE, "") == "true"
        if private_networking is None
        else private_networking
    )
    if private:
        try:
            records = await read(vault_uri, aliases)
        except VaultForbiddenError as error:
            raise VaultUnreachableError(
                "the test vault refused the runner (ForbiddenByFirewall); run runbook "
                "Repair the CI runner group"
            ) from error
    else:
        env = environment if environment is not None else _required(ENVIRONMENT_VARIABLE)
        records = await _read_in_window(vault_uri, aliases, env, firewall, read)
    return _check_expiry(records, now())


@dataclass(frozen=True, slots=True, kw_only=True)
class RopcRequest:
    """Where an alias's password grant goes: the tenant, the environment's test client and the
    API scope, with the test users' domain."""

    tenant_id: str
    client_id: str
    api_scope: str
    user_domain: str

    @classmethod
    def from_environment(cls) -> RopcRequest:
        return cls(
            tenant_id=_required(TENANT_VARIABLE),
            client_id=_required(TEST_CLIENT_VARIABLE),
            api_scope=_required(API_SCOPE_VARIABLE),
            user_domain=_required(USER_DOMAIN_VARIABLE),
        )

    @property
    def token_url(self) -> str:
        return f"{LOGIN_HOST}/{self.tenant_id}/oauth2/v2.0/token"


type TokenField = Literal["access_token", "id_token"]

ROPC_FAILURES: Final[Mapping[int, str]] = {
    50126: "password mismatch: run runbook Test-user password rotation",
    50076: (
        "multi-factor sign-in demanded: the Conditional Access exclusion is missing "
        "(runbook First admins and test identities)"
    ),
    50079: (
        "multi-factor sign-in demanded: the Conditional Access exclusion is missing "
        "(runbook First admins and test identities)"
    ),
    53003: (
        "blocked by Conditional Access: the run's API or test client is missing from the "
        "test-user policy's exclusions (runbook Run the load test)"
    ),
    50105: "user not assigned to the application (runbook First admins and test identities)",
    65001: "admin consent missing for the test client (runbook First admins and test identities)",
    90094: "admin consent missing for the test client (runbook First admins and test identities)",
}
"""`84-testing.md` section 6.4 item 4: each refusal names the runbook that fixes it."""

_AADSTS: Final = re.compile(r"AADSTS(\d+)")


def _aadsts_code(response: httpx.Response) -> tuple[int | None, str]:
    try:
        body = response.json()
    except ValueError:
        return None, ""
    if not isinstance(body, dict):
        return None, ""
    error = str(body.get("error") or "")
    codes = body.get("error_codes")
    if isinstance(codes, list) and codes and isinstance(codes[0], int):
        return codes[0], error
    match = _AADSTS.search(str(body.get("error_description") or ""))
    return (int(match.group(1)) if match else None), error


def ropc_failure(alias: str, response: httpx.Response) -> RopcError:
    """The error for a refused grant. Entra's description text is left out: it can name the
    user and the application."""
    code, error = _aadsts_code(response)
    if code is not None and code in ROPC_FAILURES:
        return RopcError(alias, code, ROPC_FAILURES[code])
    label = f"AADSTS{code}" if code is not None else "no AADSTS code"
    return RopcError(
        alias, code, f"token request refused: HTTP {response.status_code} {error} ({label})"
    )


async def acquire_ropc_token(
    alias: str,
    *,
    password: str,
    request: RopcRequest | None = None,
    client: httpx.AsyncClient | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    returning: TokenField = "access_token",
) -> str:
    """An access token for the API as `alias`, by the resource-owner password grant through the
    environment's test client (no client secret, no refresh token). Connection errors and `5xx`
    are retried twice; a `4xx` fails at once with its runbook. `returning="id_token"` gives
    the grant's ID token instead, a token whose audience is the client, not the API."""
    target = RopcRequest.from_environment() if request is None else request
    if alias not in ALIAS_USERS:
        raise TokenStepError(f"unknown test-user alias {alias!r}")
    form = {
        "grant_type": "password",
        "client_id": target.client_id,
        "scope": f"{target.api_scope} openid profile",
        "username": f"{ALIAS_USERS[alias]}@{target.user_domain}",
        "password": password,
    }
    owned = client is None
    http = (
        httpx.AsyncClient(
            timeout=httpx.Timeout(ROPC_TIMEOUT_SECONDS), follow_redirects=False, verify=TLS_CONTEXT
        )
        if client is None
        else client
    )
    try:
        for attempt in range(ROPC_RETRIES + 1):
            last = attempt == ROPC_RETRIES
            try:
                response = await http.post(target.token_url, data=form)
            except httpx.TransportError as error:
                if last:
                    raise TokenStepError(
                        f"{alias}: the token endpoint didn't answer ({type(error).__name__})"
                    ) from error
                await sleep(ROPC_RETRY_WAIT_SECONDS * (attempt + 1))
                continue
            if response.status_code >= HTTP_SERVER_ERROR and not last:
                await sleep(ROPC_RETRY_WAIT_SECONDS * (attempt + 1))
                continue
            if response.is_success:
                token = response.json().get(returning)
                if not isinstance(token, str) or not token:
                    raise TokenStepError(f"{alias}: the token answer has no {returning}")
                return token
            raise ropc_failure(alias, response)
    finally:
        if owned:
            await http.aclose()
    raise AssertionError("unreachable: the loop returns or raises")  # pragma: no cover


def unverified_claims(token: str) -> dict[str, Any]:
    """A token's claims, decoded without checking the signature (the API checks it)."""
    claims: dict[str, Any] = jwt.decode(token, options={"verify_signature": False})
    return claims


def check_token_claims(alias: str, token: str, *, api_client_id: str) -> dict[str, Any]:
    """Compare a token with its alias's row (`84-testing.md` section 6.4 item 5): `aud` the API
    client ID, `scp` containing `access_as_user` and `roles` as assigned."""
    claims = unverified_claims(token)
    problems: list[str] = []
    if claims.get("aud") != api_client_id:
        problems.append("aud isn't the API client ID")
    scopes = str(claims.get("scp", "")).split()
    if API_SCOPE_NAME not in scopes:
        problems.append(f"scp lacks {API_SCOPE_NAME}")
    expected = ALIAS_ROLES[alias]
    roles = claims.get("roles")
    if expected is None and roles is not None:
        problems.append("roles is present, but this alias has no app role")
    if expected is not None and sorted(roles or []) != sorted(expected):
        problems.append(f"roles isn't {list(expected)}")
    if problems:
        raise TokenClaimsError(f"{alias}: " + "; ".join(problems))
    return claims


type TokenAcquirer = Callable[[str, str], Awaitable[str]]


class DeployedTokens:
    """The deployed suites' tokens: acquired once per alias, each masked in the job log before
    anything else is printed and checked against its alias's row, then re-acquired with the
    in-memory password within 5 minutes of its `exp` (`84-testing.md` section 6.4 items 4 to 6).
    """

    def __init__(
        self,
        passwords: Mapping[str, str],
        *,
        api_client_id: str,
        acquire: TokenAcquirer | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._passwords = dict(passwords)
        self._api_client_id = api_client_id
        self._acquire: TokenAcquirer = (
            acquire
            if acquire is not None
            else lambda alias, password: acquire_ropc_token(alias, password=password)
        )
        self._clock = clock
        self._tokens: dict[str, tuple[str, dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    @property
    def aliases(self) -> tuple[str, ...]:
        return tuple(self._passwords)

    @override
    def __repr__(self) -> str:
        return f"DeployedTokens(aliases={list(self._passwords)})"

    async def _refresh(self, alias: str) -> str:
        token = await self._acquire(alias, self._passwords[alias])
        _emit(f"::add-mask::{token}")
        claims = check_token_claims(alias, token, api_client_id=self._api_client_id)
        self._tokens[alias] = (token, claims)
        return token

    async def acquire_all(self) -> None:
        """The token step: a token for every alias, or the first failure."""
        for alias in self._passwords:
            await self._refresh(alias)

    async def token(self, alias: str) -> str:
        """The alias's current token, re-acquired when it expires within 5 minutes."""
        if alias not in self._passwords:
            raise TokenStepError(f"no password was loaded for {alias!r}")
        async with self._lock:
            held = self._tokens.get(alias)
            if held is not None and float(held[1]["exp"]) - self._clock() > REFRESH_MARGIN_SECONDS:
                return held[0]
            return await self._refresh(alias)

    def claims(self, alias: str) -> dict[str, Any]:
        """The claims of the alias's last token."""
        return dict(self._tokens[alias][1])

    async def acquire_with(
        self, alias: str, request: RopcRequest, *, returning: TokenField = "access_token"
    ) -> str:
        """The alias's password grant sent to another request target (another client ID), for
        the checks that expect Entra to refuse it, or for the grant's ID token (the platform
        authentication check's token of another audience); the password stays in this object.
        The token is masked before it is returned."""
        if alias not in self._passwords:
            raise TokenStepError(f"no password was loaded for {alias!r}")
        token = await acquire_ropc_token(
            alias, password=self._passwords[alias], request=request, returning=returning
        )
        _emit(f"::add-mask::{token}")
        return token


def suite_aliases(environment: str, norole_skip_reason: str = "") -> dict[str, str]:
    """The deployed suites' roles and the alias each one signs in as (`84-testing.md` section
    6.5): in `load`, `admin` is `load-admin`, `member` `load-01` and `outsider` `load-02`, and
    `norole` is left out, because the `load` set has no user without a role; in `dev`, `norole`
    is left out while `DOCCHAT_TEST_NOROLE_SKIP_REASON` is set, because its user then gets no
    token at all."""
    if environment == "load":
        return {"admin": "load-admin", "member": "load-01", "outsider": "load-02"}
    roles = {"admin": "admin", "member": "member", "outsider": "outsider"}
    if not norole_skip_reason:
        roles["norole"] = "norole"
    return roles
