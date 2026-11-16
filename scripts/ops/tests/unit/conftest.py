"""Fakes and fixtures shared by the unit tests of `docchat_ops`.

The CLI reaches Azure only through its `Toolkit`, so every test runs the real parser and commands
against these fakes: the Azure CLI, the credential, the SQL layer, the test vault, the container
`documents`, the poison queue, the Azure OpenAI chat completions, a tokenizer, the three Cosmos DB
containers (`docchat_testing.fakes.cosmos.FakeContainer`) and an `httpx` mock transport for
Microsoft Graph, Entra ID, the Logs query API and Prompt Shields. Test modules
import the fake types for annotations only (mypy names this module `ops.tests.unit.conftest`).

The SQL layer answers a command's own statements from a script, in order: every statement is
compiled for SQL Server first, so a statement the dialect can't render fails the test; inserts are
recorded and answered without consuming the script.
"""

from __future__ import annotations

import base64
import io
import json
from collections import deque
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Self, override

import httpx
import pytest
from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError, ResourceNotFoundError
from openai.types.chat import ChatCompletion
from sqlalchemy import Select, Update
from sqlalchemy.dialects import mssql
from sqlalchemy.sql.dml import Insert

from docchat_core.cosmos.containers import CONVERSATIONS, MEMORIES, USAGE
from docchat_core.ingestion.settings import DEFAULT_EMBEDDING_MODELS_JSON
from docchat_ops.cli import Toolkit, main
from docchat_ops.commands.ingestion import PoisonMessage
from docchat_ops.commands.testusers import SecretInfo
from docchat_ops.commands.users import REDACTION_QUERY, UserStores
from docchat_ops.framework.audit import CONNECTION_CHECK, WRITE_AUDIT_EVENT
from docchat_ops.framework.auth import AzResult
from docchat_testing.fakes.cosmos import FakeCall, FakeContainer, FakePages

OPERATOR_OID: Final = "11111111-1111-4111-8111-111111111111"
OWNER_OID: Final = "22222222-2222-4222-8222-222222222222"
MEMBER_OID: Final = "33333333-3333-4333-8333-333333333333"
TENANT_ID: Final = "44444444-4444-4444-8444-444444444444"
TEST_CLIENT_ID: Final = "55555555-5555-4555-8555-555555555555"
WORKSPACE_ID: Final = "66666666-6666-4666-8666-666666666666"
GROUP_IDS: Final = {
    "conversationReaders": "77777777-0000-4000-8000-000000000001",
    "userDataOperators": "77777777-0000-4000-8000-000000000002",
    "configOperators": "77777777-0000-4000-8000-000000000003",
}
NOW: Final = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
VAULT_URI: Final = "https://kv-docchat-tst-abc123.vault.azure.net/"
BLOB_ENDPOINT: Final = "https://stdocchatdevabc123.blob.core.windows.net/"
QUEUE_ENDPOINT: Final = "https://stdocchatdevabc123.queue.core.windows.net/"
OPENAI_ENDPOINT: Final = "https://oai-docchat-dev-abc123.openai.azure.com"
CONTENTSAFETY_ENDPOINT: Final = "https://cs-docchat-dev-abc123.cognitiveservices.azure.com"
COSMOS_ENDPOINT: Final = "https://cosmos-docchat-dev-abc123.documents.azure.com:443/"
OPENAI_DEPLOYMENTS: Final = {
    "chat": {"name": "chat", "model": "gpt-5.6-sol", "capacity": 500, "encoding": "o200k_base"},
    "chatSmall": {
        "name": "chat-small",
        "model": "gpt-5.6-terra",
        "capacity": 530,
        "encoding": "o200k_base",
    },
}
"""The chat deployments of `infra/env/<env>.json`, as the `prod` file holds them."""
API_SETTINGS: Final = {
    "DOCCHAT_OPENAI_CHAT_REASONING_EFFORT": "low",
    "DOCCHAT_OPENAI_SMALL_REASONING_EFFORT": "none",
    "DOCCHAT_OPENAI_CHAT_PROMPT_CACHE": "disabled",
    "DOCCHAT_OPENAI_SMALL_PROMPT_CACHE": "disabled",
}
"""The OpenAI settings of key `api` of `infra/config/app-settings.json`."""
DOMAIN: Final = "contoso.onmicrosoft.com"
SENTINEL: Final = "SENTINEL-CONTENT-7f3a"
"""Content a fake store returns where no command may print it."""
ALL_ALIASES: Final = [
    "admin",
    "member",
    "outsider",
    "norole",
    *(f"load-{number:02d}" for number in range(1, 21)),
    "load-admin",
]


def jwt(oid: str) -> str:
    """An unsigned access token whose payload carries `oid`."""

    def part(data: dict[str, str]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part({'oid': oid})}.signature"


class FakeAz:
    """Answers `az` calls by argument prefix; the latest matching answer wins."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._answers: list[tuple[tuple[str, ...], AzResult | BaseException]] = []
        self.on("account", "show", stdout="user\n")
        self.on("ad", "signed-in-user", "show", stdout=f"{OWNER_OID}\n")

    def on(self, *prefix: str, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self._answers.append((prefix, AzResult(returncode, stdout, stderr)))

    def on_json(self, *prefix: str, value: object) -> None:
        self.on(*prefix, stdout=json.dumps(value))

    def fail(self, *prefix: str, stderr: str) -> None:
        self.on(*prefix, returncode=1, stderr=stderr)

    def raise_on(self, *prefix: str, error: BaseException) -> None:
        self._answers.append((prefix, error))

    def called(self, *prefix: str) -> list[tuple[str, ...]]:
        return [call for call in self.calls if call[: len(prefix)] == prefix]

    async def run(self, args: Sequence[str]) -> AzResult:
        call = tuple(args)
        self.calls.append(call)
        for prefix, answer in reversed(self._answers):
            if call[: len(prefix)] == prefix:
                if isinstance(answer, BaseException):
                    raise answer
                return answer
        raise AssertionError(f"unexpected az call: {call}")


class FakeCredential:
    """An `AsyncTokenCredential` that hands out fixed tokens per scope."""

    def __init__(self) -> None:
        self.scopes: list[str] = []
        self.refused: set[str] = set()
        self.closed = False

    async def get_token(
        self,
        *scopes: str,
        claims: str | None = None,
        tenant_id: str | None = None,
        enable_cae: bool = False,
        **kwargs: Any,
    ) -> AccessToken:
        del claims, tenant_id, enable_cae, kwargs
        scope = scopes[0]
        self.scopes.append(scope)
        if scope in self.refused:
            raise ClientAuthenticationError("not signed in")
        return AccessToken(f"token-for-{scope}", int(NOW.timestamp()) + 3600)

    async def close(self) -> None:
        self.closed = True

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


class _Row:
    """A result row: attribute access and `_mapping`, as SQLAlchemy's `Row` offers."""

    def __init__(self, values: dict[str, Any]) -> None:
        self._mapping = dict(values)

    def __getattr__(self, name: str) -> Any:
        try:
            return self._mapping[name]
        except KeyError:
            raise AttributeError(name) from None


DIALECT: Final = mssql.dialect()  # type: ignore[no-untyped-call]


@dataclass
class _Result:
    rows: list[_Row] = field(default_factory=list)
    rowcount: int = 0

    @staticmethod
    def _first_value(row: _Row) -> Any:
        return next(iter(row._mapping.values()))

    def scalar_one(self) -> Any:
        # The connection check selects the literal 1 and is never scripted.
        return self._first_value(self.rows[0]) if self.rows else 1

    def scalar_one_or_none(self) -> Any:
        return self._first_value(self.rows[0]) if self.rows else None

    def scalars(self) -> list[Any]:
        return [self._first_value(row) for row in self.rows]

    def first(self) -> _Row | None:
        return self.rows[0] if self.rows else None

    def one(self) -> _Row:
        assert len(self.rows) == 1
        return self.rows[0]

    def all(self) -> list[_Row]:
        return self.rows


def _bound_values(statement: Any) -> dict[str, Any]:
    params: dict[str, Any] = statement.compile().params
    return params


@dataclass
class FakeDriverConnection:
    """The driver connection of a unit: records each commit a unit makes in its own name."""

    database: FakeDatabase
    unit: str

    def commit(self) -> None:
        self.database.driver_commits.append(self.unit)


@dataclass
class FakeConnection:
    """Records every statement; reads and updates `app.ops_config` rows of the fake database and
    answers the other statements of a scripted unit from the database's script."""

    database: FakeDatabase
    unit: str
    scripted: bool

    @property
    def connection(self) -> FakeDriverConnection:
        """The driver connection under the unit's connection."""
        return FakeDriverConnection(self.database, self.unit)

    def execute(self, statement: Any, parameters: object = None) -> _Result:
        self.database.executed.append((statement, parameters))
        self.database.statements.append((self.unit, str(statement.compile(dialect=DIALECT))))
        if isinstance(statement, Select) and "ops_config" in str(statement):
            return _Result(rows=self._select(statement))
        if isinstance(statement, Update) and getattr(statement.table, "name", None) == "ops_config":
            return _Result(rowcount=self._update(statement))
        if isinstance(statement, Insert):
            return _Result(rowcount=len(parameters) if isinstance(parameters, list) else 1)
        if self.scripted and self.database.script:
            answer = self.database.script.popleft()
            if isinstance(answer, BaseException):
                raise answer
            return answer
        return _Result()

    def _select(self, statement: Select[Any]) -> list[_Row]:
        wanted: set[str] = set()
        for value in _bound_values(statement).values():
            wanted.update(value if isinstance(value, list) else [value])
        rows = self.database.ops_rows
        keys = sorted(key for key in rows if not wanted or key in wanted)
        return [_Row({"config_key": key, **rows[key]}) for key in keys]

    def _update(self, statement: Update) -> int:
        values = _bound_values(statement)
        row = self.database.ops_rows.get(values["config_key_1"])
        if self.database.lose_race or row is None or row["row_version"] != values["row_version_1"]:
            return 0
        row.update(
            value_json=values["value_json"],
            change_reason=values["change_reason"],
            updated_by=values["updated_by"],
            updated_at=NOW.replace(tzinfo=None),
            row_version=(int.from_bytes(row["row_version"]) + 1).to_bytes(8),
        )
        return 1


@dataclass
class FakeDatabase:
    """The SQL layer: records units of work and the audit rows inserted, and holds
    `app.ops_config` rows keyed by `config_key`."""

    token: str | None = field(default_factory=lambda: jwt(OPERATOR_OID))
    token_error: BaseException | None = None
    connect_error: BaseException | None = None
    write_error: BaseException | None = None
    units: list[tuple[str, str]] = field(default_factory=list)
    executed: list[tuple[object, object]] = field(default_factory=list)
    ops_rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    lose_race: bool = False
    """Another change wins the row between the read and the update."""
    unit_errors: dict[str, BaseException] = field(default_factory=dict)
    """A failure raised by the named unit of work, as the data-access layer raises it."""
    script: deque[_Result | BaseException] = field(default_factory=deque)
    """The answers to the statements of every unit but the connection check, in order."""
    statements: list[tuple[str, str]] = field(default_factory=list)
    """Each statement's unit and its SQL Server text."""
    driver_commits: list[str] = field(default_factory=list)
    """The unit of each commit made on the driver connection inside a unit of work."""
    disposed: bool = False

    def answer(self, *rows: dict[str, Any], rowcount: int | None = None) -> None:
        """Script the next statement's answer: its rows and its row count (the number of rows by
        default)."""
        count = len(rows) if rowcount is None else rowcount
        self.script.append(_Result(rows=[_Row(row) for row in rows], rowcount=count))

    def answer_error(self, error: BaseException) -> None:
        """Script the next statement to fail, as the driver would make it fail."""
        self.script.append(error)

    def sql(self, unit: str) -> list[str]:
        """The SQL Server text of each statement the unit ran."""
        return [text for name, text in self.statements if name == unit]

    def seed(self, key: str, value: object, default: object | None = None) -> None:
        """A row as the migrations write it, or as a change left it when `default` differs."""
        self.ops_rows[key] = {
            "value_json": json.dumps({"value": value}),
            "default_json": json.dumps({"value": value if default is None else default}),
            "change_reason": None,
            "updated_at": datetime(2026, 10, 1, 8, 30),  # noqa: DTZ001 - DATETIME2 has no zone
            "updated_by": None,
            "row_version": (1).to_bytes(8),
        }

    async def ensure_token(self) -> str | None:
        if self.token_error is not None:
            raise self.token_error
        return self.token

    async def run[T](self, work: Callable[[Any], T], *, name: str, retry: str) -> T:
        self.units.append((name, retry))
        if name == CONNECTION_CHECK and self.connect_error is not None:
            raise self.connect_error
        if name == WRITE_AUDIT_EVENT and self.write_error is not None:
            raise self.write_error
        if name in self.unit_errors:
            raise self.unit_errors[name]
        return work(FakeConnection(self, name, scripted=name != CONNECTION_CHECK))

    async def dispose(self) -> None:
        self.disposed = True

    @property
    def audit_rows(self) -> list[dict[str, Any]]:
        return [
            dict(parameters)
            for statement, parameters in self.executed
            if isinstance(statement, Insert)
            and statement.table.name == "audit_events"
            and isinstance(parameters, dict)
        ]


@dataclass
class FakeVault:
    """The test vault: secret properties and the writes made."""

    secrets: dict[str, SecretInfo] = field(default_factory=dict)
    writes: list[dict[str, Any]] = field(default_factory=list)
    list_error: BaseException | None = None
    set_errors: dict[str, BaseException] = field(default_factory=dict)
    uris: list[str] = field(default_factory=list)
    closed: bool = False

    def expire(self, alias: str, expires_on: datetime | None) -> None:
        name = f"test-user-{alias}"
        self.secrets[name] = SecretInfo(name, expires_on)

    async def list_properties(self) -> AsyncIterator[SecretInfo]:
        if self.list_error is not None:
            raise self.list_error
        for info in list(self.secrets.values()):
            yield info

    async def set_secret(
        self,
        name: str,
        value: str,
        *,
        content_type: str,
        expires_on: datetime,
        tags: dict[str, str],
    ) -> None:
        if name in self.set_errors:
            raise self.set_errors[name]
        self.writes.append(
            {
                "name": name,
                "value": value,
                "content_type": content_type,
                "expires_on": expires_on,
                "tags": tags,
            }
        )
        self.secrets[name] = SecretInfo(name, expires_on)

    async def close(self) -> None:
        self.closed = True


@dataclass
class FakeBlobs:
    """The container `documents`: blobs by path, the downloads made and the failures to raise."""

    blobs: dict[str, bytes] = field(default_factory=dict)
    endpoints: list[str] = field(default_factory=list)
    downloads: list[str] = field(default_factory=list)
    error: BaseException | None = None
    """Raised by the download before its first chunk."""
    error_after_first_chunk: BaseException | None = None
    chunk_size: int = 4
    closed: bool = False
    on_download: Callable[[], None] | None = None
    """Called when a download starts, so a test can see what was printed before it."""

    async def download(self, path: str) -> AsyncIterator[bytes]:
        self.downloads.append(path)
        if self.on_download is not None:
            self.on_download()
        if self.error is not None:
            raise self.error
        if path not in self.blobs:
            raise ResourceNotFoundError("The specified blob does not exist.")
        data = self.blobs[path]
        for start in range(0, len(data), self.chunk_size):
            yield data[start : start + self.chunk_size]
            if self.error_after_first_chunk is not None:
                raise self.error_after_first_chunk

    async def close(self) -> None:
        self.closed = True


@dataclass
class FakePoisonQueue:
    """`ingestion-requests-poison`: visible messages, the calls made and the failures to raise."""

    messages: list[PoisonMessage] = field(default_factory=list)
    endpoints: list[str] = field(default_factory=list)
    peeks: list[int] = field(default_factory=list)
    receives: list[int] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    peek_error: BaseException | None = None
    receive_errors: dict[int, BaseException] = field(default_factory=dict)
    """A failure of the receive with this 0-based call number."""
    delete_errors: dict[int, BaseException] = field(default_factory=dict)
    """A failure of the delete with this 0-based call number."""
    closed: bool = False
    deletes: int = 0
    received: set[str] = field(default_factory=set)

    def fill(self, contents: Sequence[str]) -> None:
        for content in contents:
            number = len(self.messages) + len(self.deleted)
            self.messages.append(PoisonMessage(f"message-{number}", None, content))

    async def peek(self, count: int) -> list[PoisonMessage]:
        self.peeks.append(count)
        if self.peek_error is not None:
            raise self.peek_error
        return list(self.messages[:count])

    async def receive(self, count: int) -> list[PoisonMessage]:
        call = len(self.receives)
        self.receives.append(count)
        if call in self.receive_errors:
            raise self.receive_errors[call]
        # A received message stays invisible until it is deleted.
        visible = [message for message in self.messages if message.message_id not in self.received]
        taken = [
            PoisonMessage(message.message_id, f"receipt-{message.message_id}", message.content)
            for message in visible[:count]
        ]
        self.received.update(message.message_id for message in taken)
        return taken

    async def delete(self, message: PoisonMessage) -> None:
        call = self.deletes
        self.deletes += 1
        if call in self.delete_errors:
            raise self.delete_errors[call]
        assert message.pop_receipt == f"receipt-{message.message_id}"
        self.messages = [item for item in self.messages if item.message_id != message.message_id]
        self.deleted.append(message.message_id)

    async def close(self) -> None:
        self.closed = True


class FakeConversations(FakeContainer):
    """`conversations`, also answering the legal-removal query, whose `EXISTS` over
    `retrieval.kept` the shared fake doesn't evaluate."""

    @override
    def query_items(self, query: str, **kwargs: Any) -> FakePages:
        if query != REDACTION_QUERY:
            return super().query_items(query, **kwargs)
        self.calls.append(FakeCall("query_items", None, None, dict(kwargs), query))
        parameters = {str(item["name"]): item["value"] for item in kwargs["parameters"]}
        rows = [
            {"id": item["id"], "userId": item["userId"], "sessionId": item["sessionId"]}
            for _, item in sorted(self.items.items())
            if item.get("type") == parameters["@type"]
            and any(
                kept.get("documentId") == parameters["@documentId"]
                for kept in ((item.get("retrieval") or {}).get("kept") or [])
            )
        ]
        return FakePages(self, rows, kwargs)


type ModelResponder = Callable[[dict[str, Any]], ChatCompletion | BaseException]


class FakeChatModel:
    """The Azure OpenAI chat completions: records each call's parameters and answers it, or raises,
    as `responder` says."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.endpoints: list[str] = []
        self.responder: ModelResponder | None = None
        self.closed = False

    async def complete(self, params: Any) -> ChatCompletion:
        self.requests.append(dict(params))
        assert self.responder is not None, "the test sets the model's responder"
        answer = self.responder(dict(params))
        if isinstance(answer, BaseException):
            raise answer
        return answer

    async def close(self) -> None:
        self.closed = True


class FakeEncoder:
    """One token per whitespace-separated word."""

    def __init__(self, name: str) -> None:
        self.name = name

    def encode(self, text: str, *, disallowed_special: Any) -> list[int]:
        assert disallowed_special == ()
        return list(range(len(text.split())))


type Responder = Callable[[httpx.Request], httpx.Response]


class FakeHttp:
    """An `httpx` mock transport routing by method and URL prefix; the latest route wins."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._routes: list[tuple[str, str, Responder]] = []
        self.transport = httpx.MockTransport(self._handle)

    def route(self, method: str, prefix: str, responder: Responder) -> None:
        self._routes.append((method, prefix, responder))

    def answer(self, method: str, prefix: str, status: int, body: object = None) -> None:
        def respond(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(status, json=body) if body is not None else httpx.Response(status)

        self.route(method, prefix, respond)

    def sequence(self, method: str, prefix: str, responses: list[httpx.Response]) -> None:
        pending = list(responses)

        def respond(request: httpx.Request) -> httpx.Response:
            del request
            return pending.pop(0) if len(pending) > 1 else pending[0]

        self.route(method, prefix, respond)

    def sent(self, method: str, prefix: str) -> list[httpx.Request]:
        return [
            request
            for request in self.requests
            if request.method == method and str(request.url).startswith(prefix)
        ]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for method, prefix, responder in reversed(self._routes):
            if request.method == method and str(request.url).startswith(prefix):
                return responder(request)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")


class FakeStdin(io.StringIO):
    def __init__(self) -> None:
        super().__init__("")
        self.terminal = False

    @override
    def isatty(self) -> bool:
        return self.terminal


class FixedClock:
    def __init__(self, now: datetime) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return self.current.timestamp()


@dataclass(frozen=True)
class Outcome:
    code: int
    stdout: str
    stderr: str

    @property
    def correlation_id(self) -> str:
        first = self.stderr.splitlines()[0]
        assert first.startswith("correlationId: ")
        return first.removeprefix("correlationId: ")

    @property
    def failure(self) -> str:
        """The last standard-error line, the failure line of a non-zero exit."""
        return self.stderr.splitlines()[-1]

    def json(self) -> dict[str, Any]:
        document = json.loads(self.stdout)
        assert isinstance(document, dict)
        return document


def _quoted(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def azd_text(values: dict[str, str]) -> str:
    """A dotenv file as azd writes it: every value double-quoted, quotes escaped."""
    return "".join(f'{key}="{_quoted(value)}"\n' for key, value in values.items())


DEFAULT_AZD: Final = {
    "AZURE_ENV_NAME": "dev",
    "AZURE_TENANT_ID": TENANT_ID,
    "AZURE_RESOURCE_GROUP": "rg-docchat-dev",
    "TEST_CLIENT_ID": TEST_CLIENT_ID,
    "API_SCOPE": f"api://{TENANT_ID}/docchat-dev-abc123-api/access_as_user",
    "DOCCHAT_SQL_SERVER": "sql-docchat-dev-abc123.database.windows.net",
    "DOCCHAT_SQL_DATABASE": "docchat",
    "DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID": WORKSPACE_ID,
    "DOCCHAT_COSMOS_GROUP_DATA_ACCESS": "true",
    "DOCCHAT_COSMOS_ACCOUNT_NAME": "cosmos-docchat-dev-abc123",
    "DOCCHAT_COSMOS_ENDPOINT": COSMOS_ENDPOINT,
    "DOCCHAT_COSMOS_DATABASE": "docchat",
    "DOCCHAT_COSMOS_ROLE_ASSIGNMENT_IDS": "",
    "DOCCHAT_SEARCH_MAX_INDEXES": "15",
    "DOCCHAT_OPENAI_EMBEDDING_MODELS": DEFAULT_EMBEDDING_MODELS_JSON,
    "DOCCHAT_STORAGE_BLOB_ENDPOINT": BLOB_ENDPOINT,
    "DOCCHAT_STORAGE_QUEUE_ENDPOINT": QUEUE_ENDPOINT,
    "DOCCHAT_OPENAI_ENDPOINT": OPENAI_ENDPOINT,
    "DOCCHAT_CONTENTSAFETY_ENDPOINT": CONTENTSAFETY_ENDPOINT,
    "DOCCHAT_OPENAI_CHAT_CAPACITY": "500",
    "DOCCHAT_OPENAI_SMALL_CAPACITY": "530",
}


def logs_body(columns: Sequence[str], rows: Sequence[Sequence[object]]) -> dict[str, object]:
    """A Logs query API v1 answer with one table."""
    return {
        "tables": [
            {
                "name": "PrimaryResult",
                "columns": [{"name": name, "type": "string"} for name in columns],
                "rows": [list(row) for row in rows],
            }
        ]
    }


class OpsHarness:
    """A checkout in a temporary directory and a toolkit of fakes around `main()`.

    Test modules can't import this module at run time (pytest's importlib mode), so the shared
    constants and helpers are reachable through the harness as well.
    """

    now: Final = NOW
    operator_oid: Final = OPERATOR_OID
    owner_oid: Final = OWNER_OID
    member_oid: Final = MEMBER_OID
    tenant_id: Final = TENANT_ID
    test_client_id: Final = TEST_CLIENT_ID
    workspace_id: Final = WORKSPACE_ID
    group_ids: Final = GROUP_IDS
    vault_uri: Final = VAULT_URI
    blob_endpoint: Final = BLOB_ENDPOINT
    queue_endpoint: Final = QUEUE_ENDPOINT
    openai_endpoint: Final = OPENAI_ENDPOINT
    contentsafety_endpoint: Final = CONTENTSAFETY_ENDPOINT
    cosmos_endpoint: Final = COSMOS_ENDPOINT
    domain: Final = DOMAIN
    sentinel: Final = SENTINEL
    all_aliases: Final = ALL_ALIASES
    logs_body = staticmethod(logs_body)
    jwt = staticmethod(jwt)

    def __init__(self, root: Path) -> None:
        self.root = root
        self.stderr_stream = io.StringIO()
        self.az = FakeAz()
        self.credential = FakeCredential()
        self.credentials_made = 0
        self.database = FakeDatabase()
        self.databases: list[tuple[object, object]] = []
        self.vault = FakeVault()
        self.blobs = FakeBlobs()
        self.poison = FakePoisonQueue()
        self.model = FakeChatModel()
        self.encodings: list[str] = []
        self.http = FakeHttp()
        self.conversations = FakeConversations(
            CONVERSATIONS.name, CONVERSATIONS.partition_key_paths
        )
        self.memories = FakeContainer.of(MEMORIES)
        self.usage = FakeContainer.of(USAGE)
        self.cosmos_targets: list[tuple[str, str]] = []
        self.cosmos_closed = False
        self.stdin = FakeStdin()
        self.clock = FixedClock(NOW)
        self.sleeps: list[float] = []
        self.environ: dict[str, str] = {}
        (root / "azure.yaml").write_text("name: docchat\n", encoding="utf-8")
        (root / ".env.example").write_text(
            "DOCCHAT_LOCAL_EMULATORS=\nDOCCHAT_LOCAL_SQL_SA_PASSWORD=\n", encoding="utf-8"
        )
        for environment in ("dev", "prod", "load"):
            self.write_azd(environment)
            self.write_environment_file(environment)
        self.write_bootstrap(ALL_ALIASES)
        self.write_app_settings(API_SETTINGS)

    def write_azd(self, environment: str, **overrides: str) -> None:
        values = {**DEFAULT_AZD, "AZURE_ENV_NAME": environment, **overrides}
        directory = self.root / ".azure" / environment
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ".env").write_text(azd_text(values), encoding="utf-8")

    def write_environment_file(
        self, environment: str, groups: dict[str, str] | None = None
    ) -> None:
        directory = self.root / "infra" / "env"
        directory.mkdir(parents=True, exist_ok=True)
        document = {
            "groups": GROUP_IDS if groups is None else groups,
            "openAi": {"deployments": OPENAI_DEPLOYMENTS},
        }
        (directory / f"{environment}.json").write_text(json.dumps(document), encoding="utf-8")

    def write_checkout_json(self, relative: str, document: object) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document), encoding="utf-8")

    def write_app_settings(self, api: dict[str, str]) -> None:
        self.write_checkout_json("infra/config/app-settings.json", {"api": api})

    def write_bootstrap(self, aliases: list[str]) -> None:
        directory = self.root / "scripts" / "bootstrap"
        directory.mkdir(parents=True, exist_ok=True)
        document = {"testUserAliases": aliases}
        (directory / "bootstrap.config.json").write_text(json.dumps(document), encoding="utf-8")

    def answer(self, text: str, *, terminal: bool = False) -> None:
        self.stdin.write(text)
        self.stdin.seek(0)
        self.stdin.terminal = terminal

    async def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def _credential(self) -> FakeCredential:
        self.credentials_made += 1
        return self.credential

    def _database(self, target: object, credential: object) -> FakeDatabase:
        self.databases.append((target, credential))
        return self.database

    def _vault(self, uri: str, credential: object) -> FakeVault:
        del credential
        self.vault.uris.append(uri)
        return self.vault

    def _blobs(self, endpoint: str, credential: object) -> FakeBlobs:
        del credential
        self.blobs.endpoints.append(endpoint)
        return self.blobs

    def _poison_queue(self, endpoint: str, credential: object) -> FakePoisonQueue:
        del credential
        self.poison.endpoints.append(endpoint)
        return self.poison

    def _chat_model(self, endpoint: str, credential: object) -> FakeChatModel:
        del credential
        self.model.endpoints.append(endpoint)
        return self.model

    def _encoding(self, name: str) -> FakeEncoder:
        self.encodings.append(name)
        return FakeEncoder(name)

    async def _close_cosmos(self) -> None:
        self.cosmos_closed = True

    def _cosmos(self, endpoint: str, database: str, credential: object) -> UserStores:
        del credential
        self.cosmos_targets.append((endpoint, database))
        return UserStores(
            conversations=self.conversations,
            memories=self.memories,
            usage=self.usage,
            close=self._close_cosmos,
        )

    def toolkit(self, stdout: io.StringIO, stderr: io.StringIO) -> Toolkit:
        return Toolkit(
            stdin=self.stdin,
            stdout=stdout,
            stderr=stderr,
            environ=self.environ,
            clock=self.clock,
            sleep=self._sleep,
            working_directory=self.root,
            az=self.az,
            credential=self._credential,
            database=self._database,
            vault=self._vault,
            http_transport=self.http.transport,
            blobs=self._blobs,
            poison_queue=self._poison_queue,
            chat_model=self._chat_model,
            encoding=self._encoding,
            cosmos=self._cosmos,
        )

    def run(self, *argv: str) -> Outcome:
        stdout, stderr = io.StringIO(), io.StringIO()
        self.stderr_stream = stderr
        code = main(list(argv), self.toolkit(stdout, stderr))
        return Outcome(code, stdout.getvalue(), stderr.getvalue())


@pytest.fixture
def ops(tmp_path: Path) -> OpsHarness:
    return OpsHarness(tmp_path)


@pytest.fixture
def fake_az() -> FakeAz:
    return FakeAz()


@pytest.fixture
def fake_credential() -> FakeCredential:
    return FakeCredential()


@pytest.fixture
def fake_database() -> FakeDatabase:
    return FakeDatabase()


@pytest.fixture(scope="session")
def spec_text() -> Callable[[str], str]:
    """The text of a design document under `docs/spec/`."""

    def read(file_name: str) -> str:
        for parent in Path(__file__).resolve().parents:
            candidate = parent / "docs" / "spec" / file_name
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8")
        raise FileNotFoundError(file_name)

    return read
