"""The operator CLI framework (T-541, T-542): the catalogue, the global options, `--env`, the
ticket and reason rules, confirmation, the service-principal refusal, the correlation ID and one
test per exit code (`86-operations.md` sections 2.1 to 2.9)."""

from __future__ import annotations

import argparse
import re
import sys
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from docchat_ops import cli
from docchat_ops.cli import COMMANDS, Toolkit, build_parser, reason, ticket
from docchat_ops.framework.auth import SubprocessAz
from docchat_ops.framework.exitcodes import MEANINGS, ExitCode

if TYPE_CHECKING:
    from collections.abc import Callable

    from ops.tests.unit.conftest import OpsHarness, Outcome

TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
TICKET = ("--ticket", "INC-1042")
REASON = ("--reason", "investigate a reported failure")
CORRELATION_LINE = re.compile(r"^correlationId: [0-9a-f]{32}$")


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


BUILT_SECTIONS = (
    "#### 2.8.1 ",
    "#### 2.8.2 ",
    "#### 2.8.3 ",
    "#### 2.8.4 ",
    "#### 2.8.5 ",
    "#### 2.8.6 ",
    "#### 2.8.7 ",
    "#### 2.8.8 ",
    "#### 2.8.9 ",
    "#### 2.8.10 ",
)
BUILT_GROUPS = "local|config|override|user|jit|reindex|ingestion|chat|testusers|trace|document"


def _catalogue_in_spec(spec_text: Callable[[str], str]) -> set[str]:
    text = spec_text("86-operations.md")
    found: set[str] = set()
    for heading in BUILT_SECTIONS:
        start = text.index(heading)
        end = min(
            position
            for position in (text.find("\n#### ", start + 1), text.find("\n### ", start + 1))
            if position >= 0
        )
        section = text[start:end]
        found |= {
            " ".join(match.split()[:2])
            for match in re.findall(rf"`((?:{BUILT_GROUPS}) [a-z-]+)[^`]*`", section)
        }
    return found


def _trace_found(ops: OpsHarness) -> None:
    body = ops.logs_body(
        ["TimeGenerated", "Type", "Name"], [["2026-10-30T10:00:00Z", "AppRequests", "GET"]]
    )
    ops.http.answer("POST", "https://api.loganalytics.io/", 200, body)


def test_t541_catalogue_equals_the_spec_tables(spec_text: Callable[[str], str]) -> None:
    groups = _subcommands(build_parser())
    parsed = {
        f"{group} {name}" for group, parser in groups.items() for name in _subcommands(parser)
    }
    assert parsed == _catalogue_in_spec(spec_text)
    assert parsed == {spec.key for spec in COMMANDS}


def test_t541_ticketless_commands_are_the_spec_ones() -> None:
    assert {spec.key for spec in COMMANDS if not spec.ticket} == {
        "local init",
        "config show",
        "chat capacity-check",
        "ingestion status",
        "testusers check",
    }
    assert {spec.key for spec in COMMANDS if spec.changes_data} == {
        "config set",
        "config reset",
        "override set",
        "override clear",
        "user read-conversation",
        "user export",
        "user delete",
        "user redact-document",
        "jit grant",
        "jit revoke",
        "reindex start",
        "ingestion requeue-item",
        "ingestion fail-item",
        "ingestion cancel-run",
        "ingestion abandon-rebuild",
        "ingestion set-deployments",
        "ingestion drain-poison",
        "testusers rotate",
        "document fetch-source",
        "chat model-check",
    }


@pytest.mark.parametrize(
    "argv",
    [
        ("--env", "local", "local", "init"),
        ("--env", "dev", "testusers", "check", "--set", "all"),
        ("--env", "dev", "config", "show"),
        ("--env", "dev", "ingestion", "status"),
        ("--env", "prod", "chat", "capacity-check"),
    ],
)
@pytest.mark.parametrize("option", [TICKET, REASON])
def test_t541_read_only_commands_refuse_ticket_and_reason(
    ops: OpsHarness, argv: tuple[str, ...], option: tuple[str, str]
) -> None:
    outcome = ops.run(*argv, *option)
    assert outcome.code == ExitCode.USAGE
    assert "unrecognized arguments" in outcome.failure
    assert ops.az.calls == []
    assert ops.credentials_made == 0


SOME_ID = "3f2c0d9e-8b7a-41c6-a5d4-e3f2c1b0a998"
LEDGER_COMMANDS = [
    ("reindex", "start", "--index-id", SOME_ID),
    ("ingestion", "requeue-item", "--item-id", SOME_ID),
    ("ingestion", "fail-item", "--item-id", SOME_ID),
    ("ingestion", "cancel-run", "--run-id", SOME_ID),
    ("ingestion", "abandon-rebuild", "--index-id", SOME_ID),
    (
        "ingestion",
        "set-deployments",
        "--physical-index-id",
        SOME_ID,
        "--query",
        "embed-query-te3-large-v1",
        "--ingestion",
        "embed-ingest-te3-large-v1",
    ),
    ("ingestion", "drain-poison"),
    ("document", "fetch-source", "--version", SOME_ID, "--out", "never-written.bin"),
]
"""The changing commands of the `reindex`, `ingestion` and `document` groups."""

OVERRIDE_TARGET = ("--user", SOME_ID, "--project", SOME_ID)
OVERRIDE_COMMANDS = [
    ("override", "set", *OVERRIDE_TARGET, "--index", SOME_ID, "--days", "7"),
    ("override", "clear", *OVERRIDE_TARGET),
]
"""The changing commands of the `override` group."""

USER_CHANGING_COMMANDS = [
    (
        "user",
        "read-conversation",
        "--user",
        SOME_ID,
        "--session",
        SOME_ID,
        "--out",
        "never-written-dir",
    ),
    ("user", "export", "--user", SOME_ID, "--out", "never-written-dir"),
    ("user", "delete", "--user", SOME_ID, "--confirm", SOME_ID, "--assignment-removed"),
    ("user", "redact-document", "--document-id", SOME_ID),
]
"""The commands of the `user` group that change data or write an audit row."""

USER_READ_COMMANDS = [
    ("user", "verify", "--user", SOME_ID),
    ("user", "deletion-register", "--since", "2026-10-01T00:00:00Z"),
    ("user", "audit-events", "--actor", SOME_ID, "--since", "2026-10-01T00:00:00Z"),
]
"""The read-only commands of the `user` group, which still take `--ticket` and `--reason`."""

COMMANDS_WITH_TICKET = [
    (
        "jit",
        "grant",
        "--level",
        "read",
        "--member",
        "33333333-3333-4333-8333-333333333333",
        "--hours",
        "2",
    ),
    ("jit", "revoke", "--level", "read", "--member", "33333333-3333-4333-8333-333333333333"),
    ("jit", "list"),
    ("testusers", "rotate", "--set", "dev"),
    ("trace", "show", TRACE_ID),
    ("config", "set", "--key", "api.write_requests_per_minute", "--value", "60"),
    ("config", "reset", "--key", "api.write_requests_per_minute"),
    ("override", "list", *OVERRIDE_TARGET),
    ("chat", "model-check"),
    *OVERRIDE_COMMANDS,
    *USER_CHANGING_COMMANDS,
    *USER_READ_COMMANDS,
    *LEDGER_COMMANDS,
]


@pytest.mark.parametrize("command", COMMANDS_WITH_TICKET)
@pytest.mark.parametrize(
    "extra",
    [
        (),
        REASON,
        TICKET,
        ("--ticket", "", *REASON),
        ("--ticket", "x" * 65, *REASON),
        ("--ticket", "two words", *REASON),
        ("--ticket", "INC/1", *REASON),
        (*TICKET, "--reason", "too short"),
        (*TICKET, "--reason", "r" * 501),
    ],
)
def test_t541_ticket_and_reason_are_checked_before_any_sign_in(
    ops: OpsHarness, command: tuple[str, ...], extra: tuple[str, ...]
) -> None:
    outcome = ops.run("--env", "dev", *command, *extra)
    assert outcome.code == ExitCode.USAGE
    assert ops.az.calls == []
    assert ops.credentials_made == 0


def test_t541_ticket_and_reason_rules() -> None:
    assert ticket("INC-1042#a_b") == "INC-1042#a_b"
    assert ticket("x" * 64) == "x" * 64
    assert reason("r" * 10) == "r" * 10
    assert reason("r" * 500) == "r" * 500
    with pytest.raises(argparse.ArgumentTypeError):
        ticket("x" * 65)
    with pytest.raises(argparse.ArgumentTypeError):
        reason("r" * 9)


@pytest.mark.parametrize(
    "argv",
    [
        ("--env", "dev", "trace", "show", TRACE_ID, *TICKET, *REASON),
        ("trace", "show", TRACE_ID, "--env", "dev", *TICKET, *REASON),
        ("--env", "dev", "trace", "show", TRACE_ID, "--env", "dev", *TICKET, *REASON),
    ],
)
def test_t541_env_before_the_group_or_after_the_command(
    ops: OpsHarness, argv: tuple[str, ...]
) -> None:
    _trace_found(ops)
    outcome = ops.run(*argv)
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr


def test_t541_two_different_env_values_exit_2(ops: OpsHarness) -> None:
    outcome = ops.run("--env", "dev", "trace", "show", TRACE_ID, "--env", "prod", *TICKET, *REASON)
    assert outcome.code == ExitCode.USAGE
    assert "--env is given twice" in outcome.failure
    assert ops.credentials_made == 0


def test_t541_missing_env_exits_2(ops: OpsHarness) -> None:
    outcome = ops.run("trace", "show", TRACE_ID, *TICKET, *REASON)
    assert outcome.code == ExitCode.USAGE
    assert "--env is required" in outcome.failure


def test_t541_unknown_env_exits_2(ops: OpsHarness) -> None:
    assert ops.run("--env", "test", "jit", "list", *TICKET, *REASON).code == ExitCode.USAGE


@pytest.mark.parametrize("command", COMMANDS_WITH_TICKET)
def test_t541_env_local_is_refused_except_by_local_init(
    ops: OpsHarness, command: tuple[str, ...]
) -> None:
    outcome = ops.run("--env", "local", *command, *TICKET, *REASON)
    assert outcome.code == ExitCode.USAGE
    assert "--env local is accepted only by local init" in outcome.failure
    assert ops.az.calls == []


def test_t541_local_init_refuses_an_azure_environment(ops: OpsHarness) -> None:
    outcome = ops.run("--env", "dev", "local", "init")
    assert outcome.code == ExitCode.USAGE
    assert "use --env local" in outcome.failure
    assert not (ops.root / ".env").exists()


def _grant(ops: OpsHarness, environment: str, *extra: str) -> Outcome:
    ops.az.on("ad", "group", "member", "check", stdout="false\n")
    ops.az.on("ad", "group", "member", "add")
    return ops.run(
        "--env",
        environment,
        "jit",
        "grant",
        "--level",
        "config",
        "--member",
        ops.member_oid,
        "--hours",
        "1",
        *TICKET,
        *REASON,
        *extra,
    )


def test_t541_prod_accepts_only_the_typed_environment_name(ops: OpsHarness) -> None:
    ops.answer("prod\n", terminal=True)
    outcome = _grant(ops, "prod", "--yes")
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert "Type the environment name to continue: " in outcome.stderr
    assert ops.az.called("ad", "group", "member", "add")


def test_t541_prod_refuses_yes_as_the_answer(ops: OpsHarness) -> None:
    ops.answer("yes\n", terminal=True)
    outcome = _grant(ops, "prod")
    assert outcome.code == ExitCode.USAGE
    assert ops.az.called("ad", "group", "member", "add") == []


def test_t541_prod_without_a_terminal_exits_2_before_the_summary(ops: OpsHarness) -> None:
    ops.answer("prod\n", terminal=False)
    outcome = _grant(ops, "prod", "--yes")
    assert outcome.code == ExitCode.USAGE
    assert "Dry run" not in outcome.stderr
    assert ops.az.called("ad", "group", "member", "add") == []


def test_t541_yes_skips_the_prompt_outside_prod(ops: OpsHarness) -> None:
    outcome = _grant(ops, "dev", "--yes")
    assert outcome.code == ExitCode.SUCCESS
    assert "Type yes" not in outcome.stderr
    assert "Dry run in dev:" in outcome.stderr


def test_t541_yes_before_the_group_counts_too(ops: OpsHarness) -> None:
    ops.az.on("ad", "group", "member", "check", stdout="false\n")
    ops.az.on("ad", "group", "member", "add")
    outcome = ops.run(
        "--yes",
        "--env",
        "dev",
        "jit",
        "grant",
        "--level",
        "config",
        "--member",
        ops.member_oid,
        "--hours",
        "1",
        *TICKET,
        *REASON,
    )
    assert outcome.code == ExitCode.SUCCESS


def test_t541_a_declined_prompt_exits_2_with_nothing_written(ops: OpsHarness) -> None:
    ops.answer("no\n")
    outcome = _grant(ops, "dev")
    assert outcome.code == ExitCode.USAGE
    assert "Type yes to continue: " in outcome.stderr
    assert ops.az.called("ad", "group", "member", "add") == []


CHANGING = [
    (
        "config",
        "set",
        "--key",
        "api.write_requests_per_minute",
        "--value",
        "60",
        *TICKET,
        *REASON,
        "--yes",
    ),
    ("config", "reset", "--key", "api.write_requests_per_minute", *TICKET, *REASON, "--yes"),
    (
        "jit",
        "grant",
        "--level",
        "read",
        "--member",
        "33333333-3333-4333-8333-333333333333",
        "--hours",
        "2",
        *TICKET,
        *REASON,
        "--yes",
    ),
    (
        "jit",
        "revoke",
        "--level",
        "read",
        "--member",
        "33333333-3333-4333-8333-333333333333",
        *TICKET,
        *REASON,
        "--yes",
    ),
    *(
        (*command, *TICKET, *REASON, "--yes")
        for command in (*OVERRIDE_COMMANDS, *USER_CHANGING_COMMANDS, *LEDGER_COMMANDS)
    ),
]


@pytest.mark.parametrize("environment", ["dev", "prod"])
@pytest.mark.parametrize("command", CHANGING)
def test_t541_service_principal_is_refused_by_changing_commands(
    ops: OpsHarness, environment: str, command: tuple[str, ...]
) -> None:
    ops.az.on("account", "show", stdout="servicePrincipal\n")
    outcome = ops.run("--env", environment, *command)
    assert outcome.code == ExitCode.ACCESS_DENIED
    assert ops.az.called("ad", "group", "member") == []
    assert ops.database.units == []
    assert ops.cosmos_targets == []


def test_t541_service_principal_is_refused_by_testusers_rotate(ops: OpsHarness) -> None:
    ops.az.on("account", "show", stdout="servicePrincipal\n")
    ops.environ.update(
        {"DOCCHAT_TEST_VAULT_URI": ops.vault_uri, "DOCCHAT_TEST_USER_DOMAIN": ops.domain}
    )
    outcome = ops.run("--env", "dev", "testusers", "rotate", "--set", "dev", *TICKET, *REASON)
    assert outcome.code == ExitCode.ACCESS_DENIED
    assert ops.credentials_made == 0
    assert ops.http.requests == []


def test_t541_service_principal_runs_a_changing_command_in_load(ops: OpsHarness) -> None:
    ops.az.on("account", "show", stdout="servicePrincipal\n")
    assert _grant(ops, "load", "--yes").code == ExitCode.SUCCESS


def test_t541_service_principal_runs_testusers_check(ops: OpsHarness) -> None:
    ops.az.on("account", "show", stdout="servicePrincipal\n")
    for alias in ops.all_aliases:
        ops.vault.expire(alias, ops.now.replace(year=2027))
    outcome = ops.run(
        "--env", "dev", "testusers", "check", "--set", "all", "--vault-uri", ops.vault_uri
    )
    assert outcome.code == ExitCode.SUCCESS
    assert ops.az.calls == []


def test_t541_correlation_id_is_the_first_stderr_line_before_any_network_call(
    ops: OpsHarness,
) -> None:
    seen: list[str] = []
    original = ops.az.run

    async def watching(args: Any) -> Any:
        seen.append(ops.stderr_stream.getvalue())
        return await original(args)

    ops.az.run = watching  # type: ignore[method-assign]
    ops.az.on("ad", "group", "member", "list", stdout="[]")
    outcome = ops.run("--env", "dev", "jit", "list", *TICKET, *REASON)
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert CORRELATION_LINE.match(outcome.stderr.splitlines()[0])
    assert seen
    assert all(CORRELATION_LINE.match(text.splitlines()[0]) for text in seen)


def test_t541_json_report_carries_the_common_fields(ops: OpsHarness) -> None:
    _trace_found(ops)
    outcome = ops.run("--env", "dev", "--json", "trace", "show", TRACE_ID, *TICKET, *REASON)
    document = outcome.json()
    assert document["correlationId"] == outcome.correlation_id
    assert document["command"] == "trace show"
    assert document["environment"] == "dev"
    assert document["result"] == "found"


def test_help_exits_0(ops: OpsHarness) -> None:
    assert ops.run("--help").code == ExitCode.SUCCESS


def test_missing_group_exits_2(ops: OpsHarness) -> None:
    assert ops.run("--env", "dev").code == ExitCode.USAGE


def _check_failure_line(outcome: Outcome, code: ExitCode) -> None:
    line = outcome.failure
    assert outcome.code == code
    assert line.startswith(f"exit {int(code)} ({MEANINGS[code]}): ")
    assert "; next: " in line
    assert line.endswith(f"correlationId: {outcome.correlation_id}")
    assert "token-for-" not in outcome.stderr
    assert "token-for-" not in outcome.stdout


def test_t541_exit_0_success(ops: OpsHarness) -> None:
    _trace_found(ops)
    outcome = ops.run("--env", "dev", "trace", "show", TRACE_ID, *TICKET, *REASON)
    assert outcome.code == ExitCode.SUCCESS
    assert outcome.stderr.splitlines() == [f"correlationId: {outcome.correlation_id}"]


def test_t541_exit_1_unexpected_error(ops: OpsHarness) -> None:
    ops.az.raise_on("ad", "group", "member", "list", error=RuntimeError(ops.sentinel))
    outcome = ops.run("--env", "dev", "jit", "list", *TICKET, *REASON)
    _check_failure_line(outcome, ExitCode.UNEXPECTED)
    assert "builtins.RuntimeError" in outcome.failure
    assert ops.sentinel not in outcome.stderr


def test_t541_exit_2_usage_error(ops: OpsHarness) -> None:
    _check_failure_line(ops.run("--env", "dev", "jit", "list"), ExitCode.USAGE)


def test_t541_exit_3_access_denied(ops: OpsHarness) -> None:
    ops.credential.refused.add("https://api.loganalytics.io/.default")
    outcome = ops.run("--env", "dev", "trace", "show", TRACE_ID, *TICKET, *REASON)
    _check_failure_line(outcome, ExitCode.ACCESS_DENIED)
    assert "sign in with az login" in outcome.failure


def test_t541_exit_4_not_found(ops: OpsHarness) -> None:
    ops.http.answer(
        "POST", "https://api.loganalytics.io/", 200, ops.logs_body(["TimeGenerated"], [])
    )
    outcome = ops.run("--env", "dev", "trace", "show", TRACE_ID, *TICKET, *REASON)
    _check_failure_line(outcome, ExitCode.NOT_FOUND)


def test_t541_exit_5_incomplete(ops: OpsHarness) -> None:
    for alias in ops.all_aliases:
        ops.vault.expire(alias, ops.now.replace(year=2027))
    ops.vault.expire("member", ops.now.replace(year=2025))
    outcome = ops.run(
        "--env", "dev", "testusers", "check", "--set", "dev", "--vault-uri", ops.vault_uri
    )
    _check_failure_line(outcome, ExitCode.INCOMPLETE)


def test_t541_exit_6_conflict(ops: OpsHarness) -> None:
    ops.az.on("ad", "group", "member", "check", stdout="true\n")
    outcome = ops.run(
        "--env",
        "dev",
        "jit",
        "grant",
        "--level",
        "read",
        "--member",
        ops.member_oid,
        "--hours",
        "1",
        *TICKET,
        *REASON,
        "--yes",
    )
    _check_failure_line(outcome, ExitCode.CONFLICT)


def test_t541_exit_7_unreachable(ops: OpsHarness) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    ops.http.route("POST", "https://api.loganalytics.io/", refuse)
    outcome = ops.run("--env", "dev", "trace", "show", TRACE_ID, *TICKET, *REASON)
    _check_failure_line(outcome, ExitCode.UNREACHABLE)


def test_t542_chat_capacity_check_is_registered_without_a_ticket() -> None:
    (spec,) = [spec for spec in COMMANDS if spec.key == "chat capacity-check"]
    assert not spec.ticket
    assert not spec.changes_data
    assert spec.environments == {"dev", "prod", "load"}


def test_t542_jit_grant_for_an_existing_member_exits_6(ops: OpsHarness) -> None:
    ops.az.on("ad", "group", "member", "check", stdout="true\n")
    outcome = ops.run(
        "--env",
        "prod",
        "jit",
        "grant",
        "--level",
        "user_data",
        "--member",
        ops.member_oid,
        "--hours",
        "4",
        *TICKET,
        *REASON,
    )
    assert outcome.code == ExitCode.CONFLICT
    assert ops.az.called("ad", "group", "member", "add") == []


def test_table_report_lists_rows(ops: OpsHarness) -> None:
    ops.az.on("ad", "group", "member", "list", stdout=f'["{ops.member_oid.upper()}"]')
    outcome = ops.run("--env", "dev", "jit", "list", *TICKET, *REASON)
    lines = outcome.stdout.splitlines()
    assert lines[0] == "result: listed"
    assert "mode: group" in lines
    assert lines[2].split() == ["level", "group", "members"]
    assert ops.member_oid in outcome.stdout


def test_default_toolkit_uses_the_process_streams() -> None:
    toolkit = Toolkit.default()
    assert isinstance(toolkit.az, SubprocessAz)
    assert toolkit.http_transport is None
    assert toolkit.working_directory.is_absolute()


def test_main_without_arguments_uses_the_default_toolkit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["docchat-ops", "--env", "dev"])
    assert cli.main() == ExitCode.USAGE
    captured = capsys.readouterr()
    assert CORRELATION_LINE.match(captured.err.splitlines()[0])


def test_table_cells_render_every_json_type() -> None:
    assert cli._cell(None) == "-"
    assert cli._cell(True) == "true"
    assert cli._cell(False) == "false"
    assert cli._cell({"a": 1, "b": "x"}) == "a=1 b=x"
    assert cli._cell(["a", 2]) == "a,2"
    assert cli._table(["not a row"], ["column"]).splitlines() == ["column", ""]
