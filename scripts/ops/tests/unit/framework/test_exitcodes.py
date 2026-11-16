"""The eight exit codes and the mapping of exceptions to them (T-541; `86-operations.md`
section 2.6)."""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import TYPE_CHECKING

import httpx
import pytest
from azure.core.exceptions import (
    ClientAuthenticationError,
    HttpResponseError,
    ServiceRequestError,
    ServiceResponseError,
)

from docchat_core.sql.engine import SqlError, SqlTimeout, SqlUnavailable, SqlUniqueViolation
from docchat_ops.framework.exitcodes import (
    MEANINGS,
    AccessDenied,
    ExitCode,
    UsageError,
    classify,
    failure_line,
)

if TYPE_CHECKING:
    from collections.abc import Callable

CORRELATION_ID = "0af7651916cd43dd8448eb211c80319c"


def test_meanings_are_the_spec_table(spec_text: Callable[[str], str]) -> None:
    text = spec_text("86-operations.md")
    section = text[text.index("### 2.6 Exit codes") : text.index("### 2.7 ")]
    rows = re.findall(r"^\| (\d) \| ([^|]+) \|", section, flags=re.MULTILINE)
    # Code 2's cell continues with what may still be written, which the line doesn't repeat.
    table = {int(code): meaning.strip() for code, meaning in rows}
    table[2] = table[2].split("; nothing written")[0]
    assert table == {int(code): meaning for code, meaning in MEANINGS.items()}
    assert [int(code) for code in ExitCode] == list(range(8))


def _http_error(status: int, code: str | None = None) -> HttpResponseError:
    error = HttpResponseError(message="refused")
    error.status_code = status
    if code is not None:
        error.error = SimpleNamespace(code=code)  # type: ignore[assignment]
    return error


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (UsageError("x", "y"), ExitCode.USAGE),
        (SqlUnavailable("token"), ExitCode.ACCESS_DENIED),
        (SqlUnavailable("connection", 18456, "28000"), ExitCode.ACCESS_DENIED),
        (SqlUnavailable("connection", 40615, "42000"), ExitCode.UNREACHABLE),
        (SqlError(229, "42000"), ExitCode.ACCESS_DENIED),
        (SqlTimeout(0, "HYT00"), ExitCode.UNREACHABLE),
        (SqlUnavailable("exhausted", 40613), ExitCode.UNREACHABLE),
        (SqlUniqueViolation(2627, "23000", "PK_x"), ExitCode.UNEXPECTED),
        (ClientAuthenticationError("no sign-in"), ExitCode.ACCESS_DENIED),
        (ServiceRequestError("no route"), ExitCode.UNREACHABLE),
        (ServiceResponseError("cut"), ExitCode.UNREACHABLE),
        (_http_error(403), ExitCode.ACCESS_DENIED),
        (_http_error(401), ExitCode.ACCESS_DENIED),
        (_http_error(404), ExitCode.NOT_FOUND),
        (_http_error(403, "ForbiddenByFirewall"), ExitCode.UNREACHABLE),
        (_http_error(500), ExitCode.UNEXPECTED),
        (httpx.ConnectError("refused"), ExitCode.UNREACHABLE),
        (TimeoutError(), ExitCode.UNREACHABLE),
        (KeyError("x"), ExitCode.UNEXPECTED),
    ],
)
def test_exceptions_map_to_exit_codes(error: BaseException, code: ExitCode) -> None:
    assert classify(error).exit_code == code


def test_an_unknown_error_is_named_by_its_type_only() -> None:
    failure = classify(ValueError("the user's question"))
    assert failure.exit_code == ExitCode.UNEXPECTED
    assert failure.cause == "unexpected builtins.ValueError"
    assert "question" not in failure_line(failure, CORRELATION_ID)


def test_failure_line_names_meaning_cause_next_step_and_correlation_id() -> None:
    line = failure_line(AccessDenied("no token", "sign in with az login"), CORRELATION_ID)
    assert line == (
        "exit 3 (access denied): no token; next: sign in with az login; "
        f"correlationId: {CORRELATION_ID}"
    )


def test_failure_line_quotes_a_refusal_code() -> None:
    error = UsageError("a load run holds the passwords", "wait", code="load_run_in_progress")
    assert "load_run_in_progress: a load run holds the passwords" in failure_line(
        error, CORRELATION_ID
    )
