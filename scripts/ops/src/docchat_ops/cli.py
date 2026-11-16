"""`docchat-ops`, the one audited operator CLI (`86-operations.md` section 2; ADR 0139).

`uv run docchat-ops --env <dev|prod|load|local> <group> <command> [arguments]`. The parser holds
the global options of section 2.3 and one sub-parser per group and command; `main()` draws the
run's correlation ID, runs the chosen command with `asyncio.run` and maps its outcome to one of
the exit codes 0 to 7.

A command is one `CommandSpec` row of `COMMANDS`: its arguments and handler live in its group's
module under `docchat_ops.commands`, and the row says whether it takes `--ticket` and `--reason`,
which environments it runs in and whether it changes data.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final, NoReturn, override

from docchat_core.audit.models import TICKET_PATTERN
from docchat_core.models import Clock, SystemClock
from docchat_ops.commands import (
    chat,
    config,
    document,
    ingestion,
    jit,
    local,
    reindex,
    testusers,
    trace,
    users,
)

# `typing.override` holds the bare name.
from docchat_ops.commands import override as override_group
from docchat_ops.framework import auth, confirm
from docchat_ops.framework.audit import correlation_scope, new_correlation_id, ops_database
from docchat_ops.framework.endpoints import (
    AZURE_ENVIRONMENTS,
    ENVIRONMENTS,
    REQUIRED_VALUES,
    AzdEnvironment,
    Environment,
    load_azd_environment,
    repository_root,
)
from docchat_ops.framework.exitcodes import ExitCode, UsageError, classify, failure_line

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from typing import TextIO

    import httpx
    from azure.core.credentials_async import AsyncTokenCredential
    from pydantic import JsonValue

    from docchat_ops.framework.audit import OpsDatabase
    from docchat_ops.framework.auth import AzRunner
    from docchat_ops.framework.endpoints import SqlTarget

    type Handler = Callable[[Run, argparse.Namespace], Awaitable[None]]

REASON_MIN: Final = 10
REASON_MAX: Final = 500
_TICKET_RE: Final = re.compile(TICKET_PATTERN)
LOCAL: Final = "local"
HELP_STEP: Final = "see docchat-ops --help"


@dataclass(frozen=True, kw_only=True)
class CommandSpec:
    """One command of the catalogue (`86-operations.md` section 2.8)."""

    group: str
    name: str
    help: str
    arguments: Callable[[argparse.ArgumentParser], None]
    handler: Handler
    ticket: bool = True
    """Takes the required `--ticket` and `--reason`; the five read-only commands refuse both."""
    environments: frozenset[Environment] = AZURE_ENVIRONMENTS
    changes_data: bool = False
    """Refuses a service principal outside `load` (section 2.2 item 2)."""

    @property
    def key(self) -> str:
        return f"{self.group} {self.name}"


GROUPS: Final[dict[str, str]] = {
    "local": "the developer machine's local settings",
    "config": "the ops-config values of app.ops_config",
    "override": "per-user index overrides for live testing",
    "user": "one user's conversation data: reads, export, deletion and redaction",
    "jit": "just-in-time operator access, run by a group owner",
    "reindex": "re-ingesting a logical index",
    "ingestion": "the ingestion ledger's recovery and the poison queue",
    "chat": "the chat deployments' capacity shares and the deployed model check",
    "testusers": "the non-production test users' passwords",
    "trace": "the trace behind a user's reference ID",
    "document": "an uploaded file's source, for an audit or legal request",
}

COMMANDS: Final[tuple[CommandSpec, ...]] = (
    CommandSpec(
        group="local",
        name="init",
        help="write the git-ignored .env with a new local SQL Server password",
        arguments=local.init_arguments,
        handler=local.init,
        ticket=False,
        environments=frozenset({"local"}),
    ),
    CommandSpec(
        group="config",
        name="show",
        help="print each key with its value, default, last change time and actor",
        arguments=config.show_arguments,
        handler=config.show,
        ticket=False,
    ),
    CommandSpec(
        group="config",
        name="set",
        help="change one key's value, validated with its area's model",
        arguments=config.set_arguments,
        handler=config.set_value,
        changes_data=True,
    ),
    CommandSpec(
        group="config",
        name="reset",
        help="set one key back to its default",
        arguments=config.reset_arguments,
        handler=config.reset_value,
        changes_data=True,
    ),
    CommandSpec(
        group="override",
        name="list",
        help="print the per-user index overrides, without their reasons",
        arguments=override_group.list_arguments,
        handler=override_group.list_overrides,
    ),
    CommandSpec(
        group="override",
        name="set",
        help="send one user's chat in a project to another mapped index for 1 to 30 days",
        arguments=override_group.set_arguments,
        handler=override_group.set_override,
        changes_data=True,
    ),
    CommandSpec(
        group="override",
        name="clear",
        help="remove one user's override in a project",
        arguments=override_group.clear_arguments,
        handler=override_group.clear_override,
        changes_data=True,
    ),
    CommandSpec(
        group="user",
        name="read-conversation",
        help="write one session's turns and traces to --out, at level read",
        arguments=users.read_conversation_arguments,
        handler=users.read_conversation,
        # It reads conversation content and writes an audit row, so only a person runs it, as
        # with `document fetch-source` (section 2.2 item 2).
        changes_data=True,
    ),
    CommandSpec(
        group="user",
        name="export",
        help="write every item and row of one user to --out, for a data-subject request",
        arguments=users.export_arguments,
        handler=users.export,
        changes_data=True,
    ),
    CommandSpec(
        group="user",
        name="delete",
        help="delete one user's conversation data, memory, usage and SQL rows",
        arguments=users.delete_arguments,
        handler=users.delete_user,
        changes_data=True,
    ),
    CommandSpec(
        group="user",
        name="verify",
        help="count what is left of one user in the three containers and three tables",
        arguments=users.verify_arguments,
        handler=users.verify,
    ),
    CommandSpec(
        group="user",
        name="redact-document",
        help="replace a deleted document's text in every stored turn trace with [removed]",
        arguments=users.redact_document_arguments,
        handler=users.redact_document,
        changes_data=True,
    ),
    CommandSpec(
        group="user",
        name="deletion-register",
        help="list the deletions and redactions since a restore point",
        arguments=users.deletion_register_arguments,
        handler=users.deletion_register,
    ),
    CommandSpec(
        group="user",
        name="audit-events",
        help="list the audit rows one person wrote since a time",
        arguments=users.audit_events_arguments,
        handler=users.list_audit_events,
    ),
    CommandSpec(
        group="jit",
        name="grant",
        help="add an operator to a level's group for 1 to 4 hours",
        arguments=jit.grant_arguments,
        handler=jit.grant,
        changes_data=True,
    ),
    CommandSpec(
        group="jit",
        name="revoke",
        help="remove an operator from a level's group",
        arguments=jit.revoke_arguments,
        handler=jit.revoke,
        changes_data=True,
    ),
    CommandSpec(
        group="jit",
        name="list",
        help="list the members of the three just-in-time groups",
        arguments=jit.list_arguments,
        handler=jit.list_members,
    ),
    CommandSpec(
        group="reindex",
        name="start",
        help="start a re-ingest of a logical index, as adminStartReingest does",
        arguments=reindex.start_arguments,
        handler=reindex.start,
        changes_data=True,
    ),
    CommandSpec(
        group="ingestion",
        name="status",
        help="print the dispatcher row, item counts, open runs and the latest scanner canaries",
        arguments=ingestion.status_arguments,
        handler=ingestion.status,
        ticket=False,
    ),
    CommandSpec(
        group="ingestion",
        name="requeue-item",
        help="put a running item whose lease has expired back in the queue",
        arguments=ingestion.item_arguments,
        handler=ingestion.requeue_item,
        changes_data=True,
    ),
    CommandSpec(
        group="ingestion",
        name="fail-item",
        help="fail a queued item, or a running item whose lease has expired",
        arguments=ingestion.item_arguments,
        handler=ingestion.fail_item,
        changes_data=True,
    ),
    CommandSpec(
        group="ingestion",
        name="cancel-run",
        help="cancel a run's queued items",
        arguments=ingestion.cancel_run_arguments,
        handler=ingestion.cancel_run,
        changes_data=True,
    ),
    CommandSpec(
        group="ingestion",
        name="abandon-rebuild",
        help="give up a logical index's building physical index",
        arguments=ingestion.index_arguments,
        handler=ingestion.abandon_rebuild,
        changes_data=True,
    ),
    CommandSpec(
        group="ingestion",
        name="set-deployments",
        help="move a physical index to deployments of the same model and version",
        arguments=ingestion.set_deployments_arguments,
        handler=ingestion.set_deployments,
        changes_data=True,
    ),
    CommandSpec(
        group="ingestion",
        name="drain-poison",
        help="delete the messages of ingestion-requests-poison",
        arguments=ingestion.drain_poison_arguments,
        handler=ingestion.drain_poison,
        changes_data=True,
    ),
    CommandSpec(
        group="chat",
        name="capacity-check",
        help="print one user's share of each chat deployment's capacity",
        arguments=chat.capacity_check_arguments,
        handler=chat.capacity_check,
        ticket=False,
    ),
    CommandSpec(
        group="chat",
        name="model-check",
        help="check the dev deployments and Content Safety account the chat pipeline calls",
        arguments=chat.model_check_arguments,
        handler=chat.model_check,
        environments=frozenset({"dev"}),
        # It writes an audit row, so only a person runs it (section 2.2 item 2).
        changes_data=True,
    ),
    CommandSpec(
        group="testusers",
        name="rotate",
        help="set new test-user passwords and write them to the test vault",
        arguments=testusers.rotate_arguments,
        handler=testusers.rotate,
        environments=frozenset({"dev"}),
        changes_data=True,
    ),
    CommandSpec(
        group="testusers",
        name="check",
        help="check the expiry of the test-user secrets",
        arguments=testusers.check_arguments,
        handler=testusers.check,
        ticket=False,
        environments=frozenset({"dev", "load"}),
    ),
    CommandSpec(
        group="trace",
        name="show",
        help="list the records of one trace from the environment's workspace",
        arguments=trace.show_arguments,
        handler=trace.show,
    ),
    CommandSpec(
        group="document",
        name="fetch-source",
        help="write an uploaded version's source to a new file, for an audit or legal request",
        arguments=document.fetch_source_arguments,
        handler=document.fetch_source,
        # It reads user content and writes an audit row, so only a person runs it, as with
        # `chat model-check` (section 2.2 item 2).
        changes_data=True,
    ),
)


@dataclass(frozen=True, kw_only=True)
class Toolkit:
    """Everything a run reaches outside the process; tests pass fakes."""

    stdin: TextIO
    stdout: TextIO
    stderr: TextIO
    environ: Mapping[str, str]
    clock: Clock
    sleep: Callable[[float], Awaitable[None]]
    working_directory: Path
    az: AzRunner
    credential: Callable[[], AsyncTokenCredential]
    database: Callable[[SqlTarget, AsyncTokenCredential], OpsDatabase]
    vault: Callable[[str, AsyncTokenCredential], testusers.VaultSecrets]
    http_transport: httpx.AsyncBaseTransport | None = None
    """`None` in production: each `httpx.AsyncClient` uses its default transport."""
    blobs: Callable[[str, AsyncTokenCredential], document.SourceBlobs] = document.blob_store
    """The data account's container `documents` (`document fetch-source`)."""
    poison_queue: Callable[[str, AsyncTokenCredential], ingestion.PoisonQueue] = (
        ingestion.storage_poison_queue
    )
    """The queue `ingestion-requests-poison` (`ingestion drain-poison`)."""
    chat_model: Callable[[str, AsyncTokenCredential], chat.ChatModel] = chat.openai_chat_model
    """The Azure OpenAI account's chat completions (`chat model-check`)."""
    encoding: Callable[[str], chat.Encoder] = chat.tiktoken_encoding
    """A tokenizer encoding by name (`chat model-check`)."""
    cosmos: Callable[[str, str, AsyncTokenCredential], users.UserStores] = users.cosmos_stores
    """The containers of the environment's Cosmos DB database (the `user` group)."""

    @classmethod
    def default(cls) -> Toolkit:
        return cls(
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr,
            environ=os.environ,
            clock=SystemClock(),
            sleep=asyncio.sleep,
            working_directory=Path.cwd(),
            az=auth.SubprocessAz(),
            credential=auth.cli_credential,
            database=ops_database,
            vault=testusers.key_vault_secrets,
        )


@dataclass(kw_only=True)
class Run:
    """One command run: its correlation ID, environment, ticket and the toolkit."""

    correlation_id: str
    spec: CommandSpec
    environment: Environment
    ticket: str | None
    reason: str | None
    assume_yes: bool
    json_output: bool
    toolkit: Toolkit
    _root: Path | None = field(default=None, repr=False)

    @property
    def command(self) -> str:
        return self.spec.key

    def root(self) -> Path:
        """The checkout the command reads `.azure/`, `infra/` and the bootstrap file from."""
        if self._root is None:
            self._root = repository_root(self.toolkit.working_directory)
        return self._root

    def azd(self) -> AzdEnvironment:
        """`.azure/<env>/.env` with every value the command reads present (exit 2 otherwise)."""
        environment = load_azd_environment(self.root(), self.environment)
        names = REQUIRED_VALUES.get(self.command, REQUIRED_VALUES.get(self.spec.group, ()))
        environment.require_all(names)
        return environment

    async def refuse_service_principal(self) -> None:
        await auth.refuse_service_principal(self.toolkit.az, self.environment)

    async def confirm(self, summary: Sequence[str]) -> None:
        await confirm.confirm(
            summary,
            environment=self.environment,
            assume_yes=self.assume_yes,
            stdin=self.toolkit.stdin,
            stderr=self.toolkit.stderr,
        )

    def progress(self, text: str) -> None:
        self.toolkit.stderr.write(f"{text}\n")

    def report(
        self,
        result: str,
        fields: Mapping[str, JsonValue],
        *,
        rows: str | None = None,
        columns: Sequence[str] = (),
    ) -> None:
        """The report on standard output: one JSON object with `--json`, otherwise `key: value`
        lines and, when `rows` names a list field, a table of its `columns`."""
        out = self.toolkit.stdout
        if self.json_output:
            document: dict[str, JsonValue] = {
                "correlationId": self.correlation_id,
                "command": self.command,
                "environment": self.environment,
                "result": result,
                **fields,
            }
            out.write(json.dumps(document, separators=(",", ":")) + "\n")
            return
        out.write(f"result: {result}\n")
        for key, value in fields.items():
            if key != rows:
                out.write(f"{key}: {_cell(value)}\n")
        table = fields.get(rows) if rows else None
        if isinstance(table, list):
            out.write(_table(table, columns))


def _cell(value: JsonValue) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        return " ".join(f"{key}={_cell(item)}" for key, item in value.items())
    if isinstance(value, list):
        return ",".join(_cell(item) for item in value)
    return str(value)


def _table(rows: list[JsonValue], columns: Sequence[str]) -> str:
    body = [
        [_cell(row.get(column)) if isinstance(row, dict) else "" for column in columns]
        for row in rows
    ]
    widths = [
        max([len(column), *(len(line[index]) for line in body)])
        for index, column in enumerate(columns)
    ]
    lines = [list(columns), *body]
    return "".join(
        "  ".join(cell.ljust(width) for cell, width in zip(line, widths, strict=True)).rstrip()
        + "\n"
        for line in lines
    )


class _Parser(argparse.ArgumentParser):
    """argparse with its usage errors raised as exit 2, so they leave as the one line every
    failure leaves as, naming the correlation ID."""

    @override
    def error(self, message: str) -> NoReturn:
        raise UsageError(message, HELP_STEP)


def ticket(value: str) -> str:
    if not _TICKET_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("a ticket ID is 1 to 64 letters, digits, '-', '_' or '#'")
    return value


def reason(value: str) -> str:
    if not REASON_MIN <= len(value) <= REASON_MAX:
        raise argparse.ArgumentTypeError("a reason is 10 to 500 characters")
    return value


def _add_common(parser: argparse.ArgumentParser, position: str) -> None:
    # The options are accepted before the group and after the command; each position has its
    # own destination, so two different `--env` values can be told apart and refused.
    parser.add_argument(
        "--env", dest=f"env_{position}", choices=ENVIRONMENTS, help="the target environment"
    )
    parser.add_argument(
        "--yes",
        dest=f"yes_{position}",
        action="store_true",
        help="skip the confirmation prompt (never in prod)",
    )
    parser.add_argument(
        "--json", dest=f"json_{position}", action="store_true", help="print the report as JSON"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="docchat-ops", description="The DocChat operator CLI.")
    _add_common(parser, "before")
    groups = parser.add_subparsers(dest="group", metavar="<group>", required=True)
    for group, group_help in GROUPS.items():
        group_parser = groups.add_parser(group, help=group_help, description=group_help)
        commands = group_parser.add_subparsers(dest="command", metavar="<command>", required=True)
        for spec in COMMANDS:
            if spec.group != group:
                continue
            command = commands.add_parser(spec.name, help=spec.help, description=spec.help)
            _add_common(command, "after")
            if spec.ticket:
                command.add_argument(
                    "--ticket", type=ticket, required=True, help="the ticket ID of the work"
                )
                command.add_argument(
                    "--reason", type=reason, required=True, help="the purpose, 10 to 500 characters"
                )
            spec.arguments(command)
            command.set_defaults(spec=spec)
    return parser


def _environment(args: argparse.Namespace, spec: CommandSpec) -> Environment:
    given: set[Environment] = {
        value for value in (args.env_before, args.env_after) if value is not None
    }
    if not given:
        raise UsageError("--env is required", HELP_STEP)
    if len(given) > 1:
        raise UsageError("--env is given twice with different values", "give --env once")
    environment = given.pop()
    if environment not in spec.environments:
        if environment == LOCAL:
            raise UsageError("--env local is accepted only by local init", HELP_STEP)
        allowed = " or ".join(f"--env {name}" for name in ENVIRONMENTS if name in spec.environments)
        raise UsageError(f"{spec.key} doesn't run with --env {environment}", f"use {allowed}")
    return environment


def prepare(args: argparse.Namespace, correlation_id: str, toolkit: Toolkit) -> Run:
    """The run of a parsed command line, before any sign-in."""
    spec: CommandSpec = args.spec
    return Run(
        correlation_id=correlation_id,
        spec=spec,
        environment=_environment(args, spec),
        ticket=getattr(args, "ticket", None),
        reason=getattr(args, "reason", None),
        assume_yes=bool(args.yes_before or args.yes_after),
        json_output=bool(args.json_before or args.json_after),
        toolkit=toolkit,
    )


async def execute(run: Run, args: argparse.Namespace) -> None:
    with correlation_scope(run.correlation_id):
        await run.spec.handler(run, args)


async def run_cli(argv: Sequence[str] | None, toolkit: Toolkit) -> int:
    """One run on the caller's event loop; returns the exit code. `main` runs it with
    `asyncio.run`; an integration test awaits it on its own loop."""
    correlation_id = new_correlation_id()
    # The first line of standard error, before any network call, so every later message and
    # every audit row of the run can be matched to it.
    toolkit.stderr.write(f"correlationId: {correlation_id}\n")
    toolkit.stderr.flush()
    try:
        args = build_parser().parse_args(argv)
        run = prepare(args, correlation_id, toolkit)
        await execute(run, args)
    except SystemExit as stop:
        # `--help` prints the usage and stops with 0.
        return stop.code if isinstance(stop.code, int) else int(ExitCode.SUCCESS)
    except Exception as error:  # noqa: BLE001 - the CLI's failure boundary maps every failure
        failure = classify(error)
        toolkit.stderr.write(failure_line(failure, correlation_id) + "\n")
        return int(failure.exit_code)
    return int(ExitCode.SUCCESS)


def main(argv: Sequence[str] | None = None, toolkit: Toolkit | None = None) -> int:
    """The console script `docchat-ops`; returns the exit code."""
    return asyncio.run(run_cli(argv, Toolkit.default() if toolkit is None else toolkit))
