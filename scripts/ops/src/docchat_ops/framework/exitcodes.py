"""The eight exit codes of `docchat-ops` and the mapping of exceptions to them
(`86-operations.md` section 2.6; ADR 0139 item 8).

Every failure leaves the CLI as one standard-error line naming the code's meaning, the cause, the
next step and the run's correlation ID. Causes are written here or by the command that raises,
never copied from a driver, SDK or `az` message, because those can quote a user principal name,
a key value or a server's text.
"""

from __future__ import annotations

from enum import IntEnum
from typing import ClassVar, Final

import httpx
from azure.core.exceptions import (
    ClientAuthenticationError,
    HttpResponseError,
    ServiceRequestError,
    ServiceResponseError,
)

from docchat_core.sql.engine import SqlError, SqlTimeout, SqlUnavailable


class ExitCode(IntEnum):
    SUCCESS = 0
    UNEXPECTED = 1
    USAGE = 2
    ACCESS_DENIED = 3
    NOT_FOUND = 4
    INCOMPLETE = 5
    CONFLICT = 6
    UNREACHABLE = 7


MEANINGS: Final[dict[ExitCode, str]] = {
    ExitCode.SUCCESS: "success",
    ExitCode.UNEXPECTED: "unexpected error",
    ExitCode.USAGE: "usage error, validation failure or refused precondition",
    ExitCode.ACCESS_DENIED: "access denied",
    ExitCode.NOT_FOUND: "not found",
    ExitCode.INCOMPLETE: "incomplete; run the same command once more",
    ExitCode.CONFLICT: "conflict; nothing more written",
    ExitCode.UNREACHABLE: "store unreachable",
}

SIGN_IN: Final = "sign in with az login"
SQL_LOGIN_REFUSED: Final = frozenset({18456})
"""Login failed: the caller has no contained user, so no just-in-time membership."""
SQL_FIREWALL_REFUSED: Final = frozenset({40615})
"""The server's firewall refused the client address."""
SQL_PERMISSION_DENIED: Final = frozenset({229, 230, 262, 297, 300})
"""A statement refused for a missing permission on an object or the database."""
VAULT_FIREWALL_CODE: Final = "ForbiddenByFirewall"


class OpsError(Exception):
    """A failure with its exit code, a cause and the operator's next step.

    `code` is a machine-readable refusal code such as `load_run_in_progress`, quoted in the line
    when a command's rule names one.
    """

    exit_code: ClassVar[ExitCode] = ExitCode.UNEXPECTED

    def __init__(self, cause: str, next_step: str, *, code: str | None = None) -> None:
        super().__init__(cause)
        self.cause = cause
        self.next_step = next_step
        self.code = code


class UnexpectedError(OpsError):
    exit_code = ExitCode.UNEXPECTED


class UsageError(OpsError):
    """Nothing was written (except where the command's rule says otherwise)."""

    exit_code = ExitCode.USAGE


class AccessDenied(OpsError):  # noqa: N818 - the meaning of exit code 3
    exit_code = ExitCode.ACCESS_DENIED


class NotFound(OpsError):  # noqa: N818 - the meaning of exit code 4
    exit_code = ExitCode.NOT_FOUND


class Incomplete(OpsError):  # noqa: N818 - the meaning of exit code 5
    exit_code = ExitCode.INCOMPLETE


class Conflict(OpsError):  # noqa: N818 - the meaning of exit code 6
    exit_code = ExitCode.CONFLICT


class Unreachable(OpsError):  # noqa: N818 - the meaning of exit code 7
    exit_code = ExitCode.UNREACHABLE


def _sql_error(error: SqlError) -> OpsError:  # noqa: PLR0911 - one return per SQL outcome
    if isinstance(error, SqlUnavailable) and error.reason == "token":
        return AccessDenied("no access token for SQL", SIGN_IN)
    if error.number in SQL_LOGIN_REFUSED:
        return AccessDenied(
            "SQL refused the login",
            "check the just-in-time membership, then az logout and az login",
        )
    if error.number in SQL_FIREWALL_REFUSED:
        return Unreachable(
            "the SQL server's firewall refused this address",
            "run from an address in the environment's operator allowlist",
        )
    if error.number in SQL_PERMISSION_DENIED:
        return AccessDenied(
            f"SQL refused the statement (error {error.number})",
            "check the just-in-time level the command needs",
        )
    if isinstance(error, SqlTimeout):
        return Unreachable("a SQL statement timed out", "run the command again")
    if isinstance(error, SqlUnavailable):
        return Unreachable(
            f"SQL is unreachable ({error.reason})",
            "check the network path and the operator allowlist, then run the command again",
        )
    return UnexpectedError(
        f"SQL error {error.number}", "report the correlation ID with the command you ran"
    )


def _http_status_error(error: HttpResponseError) -> OpsError:
    status = error.status_code or 0
    detail = getattr(error, "error", None)
    if getattr(detail, "code", None) == VAULT_FIREWALL_CODE:
        return Unreachable(
            "the vault's firewall refused this address",
            "run from an address in the allowlist the vault admits",
        )
    if status in {401, 403}:
        return AccessDenied(
            f"the store answered {status}", "check the role or level, then " + SIGN_IN
        )
    if status == 404:  # noqa: PLR2004 - the HTTP status
        return NotFound("the store answered 404", "check the identifiers and the environment")
    return UnexpectedError(
        f"the store answered {status}", "report the correlation ID with the command you ran"
    )


def classify(error: BaseException) -> OpsError:  # noqa: PLR0911 - one return per failure family
    """The `OpsError` an exception leaves the CLI as; anything unknown is exit 1 and is named by
    its type only, because its message may carry content."""
    if isinstance(error, OpsError):
        return error
    if isinstance(error, SqlError):
        return _sql_error(error)
    if isinstance(error, ClientAuthenticationError):
        return AccessDenied("the Azure CLI sign-in gave no token", SIGN_IN)
    if isinstance(error, (ServiceRequestError, ServiceResponseError)):
        return Unreachable(
            "a store didn't answer", "check the network path and the allowlist, then run again"
        )
    if isinstance(error, HttpResponseError):
        return _http_status_error(error)
    if isinstance(error, (httpx.TransportError, TimeoutError)):
        return Unreachable(
            "a service didn't answer in time", "check the network path, then run the command again"
        )
    name = f"{type(error).__module__}.{type(error).__qualname__}"
    return UnexpectedError(
        f"unexpected {name}", "report the correlation ID with the command you ran"
    )


def failure_line(error: OpsError, correlation_id: str) -> str:
    """The one standard-error line of a non-zero exit."""
    code = error.exit_code
    cause = f"{error.code}: {error.cause}" if error.code else error.cause
    return (
        f"exit {int(code)} ({MEANINGS[code]}): {cause}; next: {error.next_step}; "
        f"correlationId: {correlation_id}"
    )
