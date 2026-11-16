"""The operator's identity (`86-operations.md` section 2.2 items 1 to 4; ADR 0139 item 2): the
credential, the caller-type check, token acquisition, the actor of audit rows, and the Azure CLI
subprocess through which group membership, Cosmos DB role assignments and resource-group tags are
read and changed under the same sign-in.

No command accepts a credential: `azure.identity.aio.AzureCliCredential` uses the operator's own
`az login`, or a pipeline identity after `azure/login`.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

from azure.core.exceptions import ClientAuthenticationError
from azure.identity.aio import AzureCliCredential

from docchat_core.models import parse_uuid
from docchat_ops.framework.exitcodes import (
    SIGN_IN,
    AccessDenied,
    NotFound,
    OpsError,
    UnexpectedError,
    Unreachable,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from azure.core.credentials_async import AsyncTokenCredential

GRAPH_SCOPE: Final = "https://graph.microsoft.com/.default"
LOGS_SCOPE: Final = "https://api.loganalytics.io/.default"
KEY_VAULT_SCOPE: Final = "https://vault.azure.net/.default"
STORAGE_SCOPE: Final = "https://storage.azure.com/.default"
"""Blob and Queue Storage (`83-infrastructure.md` section 6.2)."""

PERSON: Final = "user"
LOAD: Final = "load"
AZ_TIMEOUT_SECONDS: Final = 300.0
"""A Cosmos DB role assignment is a long-running Resource Manager operation; 5 minutes bounds it
without cutting it short."""

_SIGN_IN_MARKERS: Final = ("az login", "AADSTS", "re-authenticate", "No subscription found")
_DENIED_MARKERS: Final = (
    "AuthorizationFailed",
    "Authorization_RequestDenied",
    "Insufficient privileges",
    "Forbidden",
    "does not have authorization",
)
_NOT_FOUND_MARKERS: Final = (
    "ResourceNotFound",
    "ResourceGroupNotFound",
    "Request_ResourceNotFound",
    "NotFound",
    "does not exist",
    "could not be found",
)
_NETWORK_MARKERS: Final = (
    "Max retries exceeded",
    "Failed to establish a new connection",
    "timed out",
    "Connection aborted",
    "ConnectionError",
    "name resolution",
    "NameResolutionError",
)


@dataclass(frozen=True)
class AzResult:
    returncode: int
    stdout: str
    stderr: str


class AzRunner(Protocol):
    async def run(self, args: Sequence[str]) -> AzResult:
        """Run `az <args>` under the operator's sign-in."""
        ...


class SubprocessAz:
    """The Azure CLI as a child process, which inherits the operator's sign-in."""

    def __init__(self, timeout: float = AZ_TIMEOUT_SECONDS) -> None:
        self._timeout = timeout

    async def run(self, args: Sequence[str]) -> AzResult:
        executable = shutil.which("az")
        if executable is None:
            raise AccessDenied("the Azure CLI isn't installed", "install it, then " + SIGN_IN)
        process = await asyncio.create_subprocess_exec(
            executable,
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(self._timeout):
                stdout, stderr = await process.communicate()
        except TimeoutError:
            process.kill()
            await process.wait()
            raise Unreachable(
                f"az {' '.join(args[:3])} didn't finish in time", "run the command again"
            ) from None
        return AzResult(
            returncode=process.returncode if process.returncode is not None else 1,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )


def az_failure(stderr: str, action: str) -> OpsError:
    """The exit an `az` failure maps to. The CLI's own message isn't repeated, because it can
    name the signed-in account."""
    if any(marker in stderr for marker in _SIGN_IN_MARKERS):
        return AccessDenied(f"{action} needs a fresh sign-in", SIGN_IN)
    if any(marker in stderr for marker in _DENIED_MARKERS):
        return AccessDenied(f"{action} was refused", "check the role or ownership it needs")
    if any(marker in stderr for marker in _NOT_FOUND_MARKERS):
        return NotFound(f"{action} found no such object", "check the identifiers and the checkout")
    if any(marker in stderr for marker in _NETWORK_MARKERS):
        return Unreachable(f"{action} couldn't reach Azure", "check the network, then run again")
    return UnexpectedError(f"{action} failed", "run the az command by hand to read its message")


async def az_text(az: AzRunner, args: Sequence[str], action: str) -> str:
    """The stripped standard output of a successful `az` call."""
    result = await az.run(args)
    if result.returncode != 0:
        raise az_failure(result.stderr, action)
    return result.stdout.strip()


async def az_json(az: AzRunner, args: Sequence[str], action: str) -> object:
    """The parsed JSON output of a successful `az … -o json` call."""
    text = await az_text(az, args, action)
    try:
        return json.loads(text) if text else None
    except json.JSONDecodeError:
        raise UnexpectedError(f"{action} printed no JSON", "check the Azure CLI version") from None


async def caller_type(az: AzRunner) -> str:
    """`user` for a person; `servicePrincipal` (or any other value) for a pipeline identity."""
    return await az_text(
        az, ["account", "show", "--query", "user.type", "-o", "tsv"], "reading the sign-in"
    )


async def refuse_service_principal(az: AzRunner, environment: str) -> None:
    """A command that changes data runs as a person outside `load` (exit 3 otherwise); any
    caller type other than `user` counts as a pipeline identity."""
    if environment == LOAD:
        return
    if await caller_type(az) != PERSON:
        raise AccessDenied(
            "a service principal can't run a changing command outside load",
            "run it signed in as yourself with az login",
        )


async def signed_in_user(az: AzRunner) -> str:
    """The signed-in person's object ID."""
    text = await az_text(
        az, ["ad", "signed-in-user", "show", "--query", "id", "-o", "tsv"], "reading the sign-in"
    )
    try:
        return str(parse_uuid(text))
    except ValueError:
        raise AccessDenied("the sign-in names no person", SIGN_IN) from None


def cli_credential() -> AsyncTokenCredential:
    return AzureCliCredential()


async def acquire_token(credential: AsyncTokenCredential, scope: str, store: str) -> str:
    """A token for one target store; no sign-in, or one the store's tenant refuses, is exit 3
    before anything is read or changed."""
    try:
        token = await credential.get_token(scope)
    except ClientAuthenticationError:
        raise AccessDenied(f"no token for {store}", SIGN_IN) from None
    return token.token


def token_object_id(token: str) -> str:
    """The `oid` claim of an access token, read without checking the signature: the store that
    accepts the token checks it, and the claim only names the caller in audit rows."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return str(parse_uuid(claims["oid"]))
    except (IndexError, KeyError, TypeError, ValueError, binascii.Error):
        raise AccessDenied("the access token names no caller", SIGN_IN) from None
