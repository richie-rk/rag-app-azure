"""`testusers rotate` and `testusers check` (`86-operations.md` section 2.8.8; `84-testing.md`
sections 6.1 and 6.2; ADR 0019 items 5, 6 and 8).

The 25 test passwords are the product's only stored secrets: each is the secret
`test-user-<alias>` of the shared test vault, written by `rotate` with a 30-day expiry and read
back only by the CI test identity. `rotate` changes one alias at a time (Microsoft Graph, then the
vault, then for the `dev` set a sign-in proof), so a failure leaves every later alias on its
earlier password and a rerun completes the set. `check` reads secret properties only, never a
value.
"""

from __future__ import annotations

import argparse
import re
import secrets
import string
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final, NamedTuple, Protocol
from urllib.parse import quote, urlsplit

import httpx
from azure.core.exceptions import AzureError
from azure.keyvault.secrets.aio import SecretClient

from docchat_core.audit.models import TestUsersRotatedMetadata
from docchat_core.clients.tls import TLS_CONTEXT
from docchat_core.models import format_utc
from docchat_core.sql.engine import SqlError
from docchat_ops.framework.audit import connect, operator_event, write_event
from docchat_ops.framework.auth import (
    GRAPH_SCOPE,
    KEY_VAULT_SCOPE,
    acquire_token,
    az_json,
)
from docchat_ops.framework.endpoints import read_checkout_json
from docchat_ops.framework.exitcodes import (
    SIGN_IN,
    AccessDenied,
    Incomplete,
    NotFound,
    OpsError,
    UnexpectedError,
    Unreachable,
    UsageError,
    classify,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from azure.core.credentials_async import AsyncTokenCredential
    from pydantic import JsonValue

    from docchat_ops.cli import Run

DEV_ALIASES: Final = ("member", "outsider", "norole", "admin")
LOAD_ALIASES: Final = (*(f"load-{number:02d}" for number in range(1, 21)), "load-admin")
ALIASES: Final = (*DEV_ALIASES, *LOAD_ALIASES)
"""The aliases of `84-testing.md` section 6.1, in rotation order: the `dev` set first."""
SETS: Final[dict[str, tuple[str, ...]]] = {"dev": DEV_ALIASES, "load": LOAD_ALIASES, "all": ALIASES}

BOOTSTRAP_CONFIG: Final = "scripts/bootstrap/bootstrap.config.json"
SECRET_PREFIX: Final = "test-user-"  # noqa: S105 - the secrets' name prefix
SECRET_TAG: Final = "docchat-test-user"  # noqa: S105 - a tag name
SECRET_CONTENT_TYPE: Final = "text/plain"  # noqa: S105 - a media type
SECRET_LIFETIME: Final = timedelta(days=30)
WARNING_WINDOW: Final = timedelta(days=7)

PASSWORD_LENGTH: Final = 32
PASSWORD_SYMBOLS: Final = "!#%+-.:=?@_~"  # noqa: S105 - the alphabet, not a password
PASSWORD_CLASSES: Final = (
    string.ascii_uppercase,
    string.ascii_lowercase,
    string.digits,
    PASSWORD_SYMBOLS,
)
PASSWORD_ALPHABET: Final = "".join(PASSWORD_CLASSES)

SIGN_IN_WAIT_SECONDS: Final = 30
SIGN_IN_ATTEMPTS: Final = 3
PASSWORD_MISMATCH: Final = 50126

LOAD_GROUP: Final = "rg-docchat-load"
LOAD_RUN_TAG: Final = "docchat-load-run"
VAULT_URI_VARIABLE: Final = "DOCCHAT_TEST_VAULT_URI"
DOMAIN_VARIABLE: Final = "DOCCHAT_TEST_USER_DOMAIN"

GRAPH_USER_URL: Final = "https://graph.microsoft.com/v1.0/users/{upn}"
TOKEN_URL: Final = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"  # noqa: S105
GRAPH_TIMEOUT: Final = httpx.Timeout(30.0, connect=5.0)
TOKEN_TIMEOUT: Final = httpx.Timeout(15.0, connect=5.0)
KEY_VAULT_CONNECTION_TIMEOUT: Final = 5
KEY_VAULT_READ_TIMEOUT: Final = 30

ROTATION_RUNBOOK: Final = "runbook First admins and test identities"
SIGN_IN_HINTS: Final[dict[int, str]] = {
    50076: "multi-factor sign-in demanded: the Conditional Access exclusion is missing "
    f"({ROTATION_RUNBOOK})",
    50079: "multi-factor sign-in demanded: the Conditional Access exclusion is missing "
    f"({ROTATION_RUNBOOK})",
    53003: "blocked by Conditional Access: the API or test client is missing from the test-user "
    "policy's exclusions",
    50105: f"user not assigned to the application ({ROTATION_RUNBOOK})",
    65001: f"admin consent missing for the test client ({ROTATION_RUNBOOK})",
    90094: f"admin consent missing for the test client ({ROTATION_RUNBOOK})",
}

_VAULT_HOST_RE: Final = re.compile(r"^[a-z0-9-]{3,24}\.vault\.azure\.net$")
_DOMAIN_RE: Final = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)
_AADSTS_RE: Final = re.compile(r"AADSTS(\d+)")


# --- Aliases, arguments and passwords ---------------------------------------------------------


def user_name(alias: str) -> str:
    """The test user of an alias (`docchat-test-member`, `docchat-load-07`)."""
    return f"docchat-test-{alias}" if alias in DEV_ALIASES else f"docchat-{alias}"


def secret_name(alias: str) -> str:
    return f"{SECRET_PREFIX}{alias}"


def configured_aliases(document: object) -> tuple[str, ...]:
    """`testUserAliases` of the bootstrap configuration, each one an alias of the catalogue."""
    aliases = document.get("testUserAliases") if isinstance(document, dict) else None
    if not isinstance(aliases, list) or not all(isinstance(item, str) for item in aliases):
        raise UsageError(f"{BOOTSTRAP_CONFIG} has no testUserAliases list", "restore it from main")
    unknown = sorted(set(aliases) - set(ALIASES))
    if unknown:
        raise UsageError(
            f"testUserAliases of {BOOTSTRAP_CONFIG} holds unknown aliases: {', '.join(unknown)}",
            "keep the aliases of 84-testing.md section 6.1",
        )
    return tuple(alias for alias in ALIASES if alias in aliases)


def selected_aliases(run: Run, set_name: str) -> tuple[str, ...]:
    configured = configured_aliases(read_checkout_json(run.root(), BOOTSTRAP_CONFIG))
    chosen = tuple(alias for alias in configured if alias in SETS[set_name])
    if not chosen:
        raise UsageError(
            f"testUserAliases names no alias of the set {set_name}", "restore it from main"
        )
    return chosen


def vault_uri(given: str | None, environ: Mapping[str, str]) -> str:
    """`--vault-uri`, by default `DOCCHAT_TEST_VAULT_URI`: an `https://` Key Vault URI."""
    value = given or environ.get(VAULT_URI_VARIABLE, "")
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or not _VAULT_HOST_RE.fullmatch(parts.netloc.lower())
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        raise UsageError(
            "no test vault URI (https://<vault>.vault.azure.net/)",
            f"pass --vault-uri or export {VAULT_URI_VARIABLE} of the GitHub environment dev-tests",
        )
    return f"https://{parts.netloc.lower()}/"


def user_domain(given: str | None, environ: Mapping[str, str]) -> str:
    """`--domain`, by default `DOCCHAT_TEST_USER_DOMAIN`: the tenant's initial domain."""
    value = (given or environ.get(DOMAIN_VARIABLE, "")).lower()
    if not _DOMAIN_RE.fullmatch(value):
        raise UsageError(
            "no test-user domain",
            f"pass --domain or export {DOMAIN_VARIABLE} from the GitHub environment dev-tests",
        )
    return value


def new_password(choice: Callable[[Sequence[str]], str] = secrets.choice) -> str:
    """32 characters from `secrets.choice` over letters, digits and `!#%+-.:=?@_~`, none of them
    whitespace, which ROPC refuses; redrawn until each class is present, so Entra's complexity
    rule always holds."""
    while True:
        password = "".join(choice(PASSWORD_ALPHABET) for _ in range(PASSWORD_LENGTH))
        if all(any(char in characters for char in password) for characters in PASSWORD_CLASSES):
            return password


def _set_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--set", dest="user_set", choices=tuple(SETS), required=True)


def _vault_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--vault-uri", help=f"default: the variable {VAULT_URI_VARIABLE}")


def min_hours(value: str) -> int:
    if not value.isdigit():
        raise argparse.ArgumentTypeError("--min-hours is a whole number of hours")
    return int(value)


def rotate_arguments(parser: argparse.ArgumentParser) -> None:
    _set_argument(parser)
    _vault_argument(parser)
    parser.add_argument("--domain", help=f"default: the variable {DOMAIN_VARIABLE}")


def check_arguments(parser: argparse.ArgumentParser) -> None:
    _set_argument(parser)
    parser.add_argument("--min-hours", type=min_hours, help="fail when a secret expires sooner")
    _vault_argument(parser)


# --- The test vault -------------------------------------------------------------------------


class SecretInfo(NamedTuple):
    name: str
    expires_on: datetime | None


class VaultSecrets(Protocol):
    """The test vault as `testusers` uses it: metadata reads and writes, never a value read."""

    def list_properties(self) -> AsyncIterator[SecretInfo]: ...

    async def set_secret(
        self,
        name: str,
        value: str,
        *,
        content_type: str,
        expires_on: datetime,
        tags: dict[str, str],
    ) -> None: ...

    async def close(self) -> None: ...


class KeyVaultSecrets:
    """`azure.keyvault.secrets.aio.SecretClient` with the tools' timeouts: connection 5 s,
    read 30 s."""

    def __init__(self, client: SecretClient) -> None:
        self._client = client

    async def list_properties(self) -> AsyncIterator[SecretInfo]:
        async for item in self._client.list_properties_of_secrets():
            if item.name:
                yield SecretInfo(item.name, item.expires_on)

    async def set_secret(
        self,
        name: str,
        value: str,
        *,
        content_type: str,
        expires_on: datetime,
        tags: dict[str, str],
    ) -> None:
        await self._client.set_secret(
            name, value, content_type=content_type, expires_on=expires_on, tags=tags
        )

    async def close(self) -> None:
        await self._client.close()


def key_vault_secrets(uri: str, credential: AsyncTokenCredential) -> VaultSecrets:
    return KeyVaultSecrets(
        SecretClient(
            uri,
            credential,
            connection_timeout=KEY_VAULT_CONNECTION_TIMEOUT,
            read_timeout=KEY_VAULT_READ_TIMEOUT,
        )
    )


# --- rotate -----------------------------------------------------------------------------------


async def refuse_load_run(run: Run) -> None:
    """A running load test holds the current `load` passwords (exit 2, nothing changed)."""
    tags = await az_json(
        run.toolkit.az,
        ["group", "show", "--name", LOAD_GROUP, "--query", "tags", "-o", "json"],
        f"reading the tags of {LOAD_GROUP}",
    )
    if isinstance(tags, dict) and LOAD_RUN_TAG in tags:
        raise UsageError(
            f"{LOAD_GROUP} carries {LOAD_RUN_TAG}: a running load test holds the current passwords",
            "wait for the load run to end and tear down, then rotate the load set",
            code="load_run_in_progress",
        )


async def set_password(
    client: httpx.AsyncClient, token: str, upn: str, password: str, alias: str
) -> None:
    """`PATCH /v1.0/users/<upn>` with the new password and no forced change."""
    try:
        response = await client.patch(
            GRAPH_USER_URL.format(upn=quote(upn, safe="@")),
            headers={"Authorization": f"Bearer {token}"},
            json={
                "passwordProfile": {"password": password, "forceChangePasswordNextSignIn": False}
            },
        )
    except httpx.TransportError:
        raise Unreachable(
            f"Microsoft Graph didn't answer for alias {alias}",
            "check the network, then run the same command once more",
        ) from None
    status = response.status_code
    if status in {200, 204}:
        return
    if status == 403:  # noqa: PLR2004 - the HTTP status
        raise AccessDenied(
            f"Microsoft Graph refused the password change of alias {alias}",
            "the Password Administrator assignment on the administrative unit DocChat test users "
            "is missing",
        )
    if status == 401:  # noqa: PLR2004 - the HTTP status
        raise AccessDenied(f"Microsoft Graph refused the token at alias {alias}", SIGN_IN)
    if status == 404:  # noqa: PLR2004 - the HTTP status
        raise NotFound(f"no test user for alias {alias}", f"create it ({ROTATION_RUNBOOK})")
    if status == 429 or status >= 500:  # noqa: PLR2004 - the HTTP statuses
        raise Unreachable(
            f"Microsoft Graph answered {status} for alias {alias}", "run the same command once more"
        )
    raise UnexpectedError(
        f"Microsoft Graph answered {status} for alias {alias}", "report the correlation ID"
    )


def _sign_in_error(response: httpx.Response) -> int | None:
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    codes = body.get("error_codes")
    if isinstance(codes, list) and codes and isinstance(codes[0], int):
        return codes[0]
    found = _AADSTS_RE.search(str(body.get("error_description", "")))
    return int(found.group(1)) if found else None


async def prove_sign_in(
    run: Run,
    client: httpx.AsyncClient,
    *,
    tenant: str,
    client_id: str,
    scope: str,
    upn: str,
    password: str,
    alias: str,
) -> None:
    """The ROPC sign-in of a `dev` user against the `dev` test client, 30 s after the change
    and up to 3 times 30 s apart while Entra hasn't taken the password yet (exit 5 after that).
    The token is discarded."""
    form = {
        "grant_type": "password",
        "client_id": client_id,
        "scope": f"{scope} openid profile",
        "username": upn,
        "password": password,
    }
    last = "no answer"
    for attempt in range(1, SIGN_IN_ATTEMPTS + 1):
        await run.toolkit.sleep(SIGN_IN_WAIT_SECONDS)
        try:
            response = await client.post(TOKEN_URL.format(tenant=tenant), data=form)
        except httpx.TransportError:
            last = "no answer"
            continue
        if response.status_code == 200:  # noqa: PLR2004 - the HTTP status
            return
        code = _sign_in_error(response)
        if code is not None and code != PASSWORD_MISMATCH:
            raise Incomplete(
                f"the sign-in of alias {alias} was refused with AADSTS{code}",
                SIGN_IN_HINTS.get(code, f"check the test client and the user ({ROTATION_RUNBOOK})")
                + "; then run the same command once more",
            )
        last = f"AADSTS{code}" if code is not None else f"HTTP {response.status_code}"
        run.progress(f"sign-in of {alias}: attempt {attempt} of {SIGN_IN_ATTEMPTS} failed ({last})")
    raise Incomplete(
        f"the sign-in of alias {alias} still fails after {SIGN_IN_ATTEMPTS} attempts ({last})",
        "wait a few minutes, then run the same command once more",
    )


async def _write_secret(
    vault: VaultSecrets, alias: str, password: str, expires_on: datetime
) -> None:
    try:
        await vault.set_secret(
            secret_name(alias),
            password,
            content_type=SECRET_CONTENT_TYPE,
            expires_on=expires_on,
            tags={SECRET_TAG: alias},
        )
    except AzureError as error:
        failure = classify(error)
        raise Incomplete(
            f"the password of alias {alias} changed but its secret wasn't written: {failure.cause}",
            f"{failure.next_step}; then run the same command once more",
        ) from None


async def _probe(vault: VaultSecrets) -> None:
    # One page of metadata proves the network path and the role before any password changes.
    async for _ in vault.list_properties():
        break


async def rotate(run: Run, args: argparse.Namespace) -> None:
    aliases = selected_aliases(run, args.user_set)
    uri = vault_uri(args.vault_uri, run.toolkit.environ)
    domain = user_domain(args.domain, run.toolkit.environ)
    azd = run.azd()
    client_id = azd.require_uuid("TEST_CLIENT_ID")
    tenant = azd.require_uuid("AZURE_TENANT_ID")
    api_scope = azd.require("API_SCOPE")
    sql = azd.sql_target()
    await run.refuse_service_principal()
    async with run.toolkit.credential() as credential:
        graph_token = await acquire_token(credential, GRAPH_SCOPE, "Microsoft Graph")
        await acquire_token(credential, KEY_VAULT_SCOPE, "Key Vault")
        database = run.toolkit.database(sql, credential)
        vault = run.toolkit.vault(uri, credential)
        try:
            try:
                actor = await connect(database)
            except (SqlError, OpsError) as error:
                failure = classify(error)
                raise AccessDenied(
                    f"no SQL connection to dev for the audit row: {failure.cause}",
                    failure.next_step,
                ) from None
            if any(alias in LOAD_ALIASES for alias in aliases):
                await refuse_load_run(run)
            await _probe(vault)
            await run.confirm(
                [
                    f"set: {args.user_set} ({len(aliases)} aliases: {', '.join(aliases)})",
                    f"vault: {urlsplit(uri).netloc}",
                    "change: a new password per user and its secret with a 30-day expiry",
                ]
            )
            rotated = await _rotate_each(
                run,
                aliases,
                vault=vault,
                graph_token=graph_token,
                domain=domain,
                tenant=tenant,
                client_id=client_id,
                api_scope=api_scope,
            )
            event = operator_event(
                action="test_users.rotated",
                actor_object_id=actor,
                target_type="test_users",
                target_id=None,
                metadata=TestUsersRotatedMetadata(environment="dev", alias_count=len(rotated)),
            )
            try:
                await write_event(database, event)
            except SqlError as error:
                failure = classify(error)
                raise Incomplete(
                    f"every password was rotated but the audit row wasn't written: {failure.cause}",
                    f"{failure.next_step}; then run the same command once more",
                ) from None
        finally:
            await vault.close()
            await database.dispose()
    run.report(
        "rotated",
        {"aliasCount": len(rotated), "aliases": rotated},
        rows="aliases",
        columns=("alias", "expiresOn"),
    )


async def _rotate_each(
    run: Run,
    aliases: tuple[str, ...],
    *,
    vault: VaultSecrets,
    graph_token: str,
    domain: str,
    tenant: str,
    client_id: str,
    api_scope: str,
) -> list[JsonValue]:
    rotated: list[JsonValue] = []
    async with (
        httpx.AsyncClient(
            timeout=GRAPH_TIMEOUT,
            follow_redirects=False,
            transport=run.toolkit.http_transport,
            verify=TLS_CONTEXT,
        ) as graph,
        httpx.AsyncClient(
            timeout=TOKEN_TIMEOUT,
            follow_redirects=False,
            transport=run.toolkit.http_transport,
            verify=TLS_CONTEXT,
        ) as login,
    ):
        for alias in aliases:
            upn = f"{user_name(alias)}@{domain}"
            password = new_password()
            await set_password(graph, graph_token, upn, password, alias)
            expires_on = run.toolkit.clock.now() + SECRET_LIFETIME
            await _write_secret(vault, alias, password, expires_on)
            if alias in DEV_ALIASES:
                await prove_sign_in(
                    run,
                    login,
                    tenant=tenant,
                    client_id=client_id,
                    scope=api_scope,
                    upn=upn,
                    password=password,
                    alias=alias,
                )
            run.progress(f"rotated {alias}; its secret expires {format_utc(expires_on)}")
            rotated.append({"alias": alias, "expiresOn": format_utc(expires_on)})
    return rotated


# --- check ------------------------------------------------------------------------------------


def _status(expires_on: datetime | None, now: datetime, hours: int | None) -> str:
    if expires_on is None:
        return "no_expiry"
    if expires_on <= now:
        return "expired"
    if hours is not None and expires_on <= now + timedelta(hours=hours):
        return "too_soon"
    if expires_on <= now + WARNING_WINDOW:
        return "warning"
    return "ok"


async def check(run: Run, args: argparse.Namespace) -> None:
    aliases = selected_aliases(run, args.user_set)
    uri = vault_uri(args.vault_uri, run.toolkit.environ)
    hours: int | None = args.min_hours
    names = {secret_name(alias): alias for alias in aliases}
    found: dict[str, datetime | None] = {}
    async with run.toolkit.credential() as credential:
        vault = run.toolkit.vault(uri, credential)
        try:
            async for info in vault.list_properties():
                if info.name in names:
                    found[names[info.name]] = info.expires_on
        finally:
            await vault.close()
    now = run.toolkit.clock.now()
    secrets_found: list[JsonValue] = []
    warnings: list[str] = []
    failed: list[str] = []
    missing: list[str] = []
    for alias in aliases:
        if alias not in found:
            missing.append(alias)
            secrets_found.append({"alias": alias, "expiresOn": None, "status": "missing"})
            continue
        expires_on = found[alias]
        status = _status(expires_on, now, hours)
        if status in {"expired", "too_soon", "no_expiry"}:
            failed.append(alias)
        if expires_on is not None and expires_on <= now + WARNING_WINDOW:
            warnings.append(alias)
        secrets_found.append(
            {
                "alias": alias,
                "expiresOn": format_utc(expires_on) if expires_on else None,
                "status": status,
            }
        )
    if run.toolkit.environ.get("GITHUB_ACTIONS") == "true":
        for alias in warnings:
            run.progress(f"::warning::{secret_name(alias)} expires within 7 days")
    fields: dict[str, JsonValue] = {"secrets": secrets_found}
    if run.json_output:
        warning_aliases: list[JsonValue] = list(warnings)
        fields["warnings"] = warning_aliases
    run.report(
        "failed" if failed or missing else "passed",
        fields,
        rows="secrets",
        columns=("alias", "expiresOn", "status"),
    )
    if missing:
        raise NotFound(
            f"no secret for {', '.join(missing)}",
            "rotate the set (runbook Test-user password rotation)",
        )
    if failed:
        raise Incomplete(
            f"secrets expired or expiring too soon: {', '.join(failed)}",
            "rotate the set (runbook Test-user password rotation)",
        )
