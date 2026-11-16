"""Where a command finds its targets (`86-operations.md` section 2.2 item 5; `81-config.md`
section 4.9; ADR 0139 item 3).

Endpoints and names come only from the azd environment file `.azure/<env>/.env` that
`azd env refresh` writes, and from files of the checkout, never from an argument, so no operator
types an endpoint and no command accepts a credential. `--env local` works on the repository's own
`.env` instead.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from docchat_core.models import parse_uuid
from docchat_core.sql.settings import SQL_DATABASE_NAME, SQL_SERVER_PATTERN
from docchat_ops.framework.exitcodes import UsageError

type Environment = Literal["dev", "prod", "load", "local"]

ENVIRONMENTS: Final[tuple[Environment, ...]] = ("dev", "prod", "load", "local")
AZURE_ENVIRONMENTS: Final[frozenset[Environment]] = frozenset({"dev", "prod", "load"})

PROJECT_MARKER: Final = "azure.yaml"
"""The azd project file at the repository root; azd writes `.azure/` beside it."""
LOCAL_ENV_FILE: Final = ".env"
LOCAL_ENV_EXAMPLE: Final = ".env.example"

SQL_VALUES: Final = ("DOCCHAT_SQL_SERVER", "DOCCHAT_SQL_DATABASE")

REQUIRED_VALUES: Final[dict[str, tuple[str, ...]]] = {
    "config": SQL_VALUES,
    "override": SQL_VALUES,
    "reindex": (*SQL_VALUES, "DOCCHAT_SEARCH_MAX_INDEXES", "DOCCHAT_OPENAI_EMBEDDING_MODELS"),
    "chat": SQL_VALUES,
    # `AZURE_TENANT_ID` and the operator's object ID make the calls' safety identifier.
    "chat model-check": (
        *SQL_VALUES,
        "DOCCHAT_OPENAI_ENDPOINT",
        "DOCCHAT_CONTENTSAFETY_ENDPOINT",
        "AZURE_TENANT_ID",
    ),
    "ingestion": (*SQL_VALUES, "DOCCHAT_SEARCH_MAX_INDEXES", "DOCCHAT_OPENAI_EMBEDDING_MODELS"),
    "ingestion drain-poison": (
        *SQL_VALUES,
        "DOCCHAT_SEARCH_MAX_INDEXES",
        "DOCCHAT_OPENAI_EMBEDDING_MODELS",
        "DOCCHAT_STORAGE_QUEUE_ENDPOINT",
    ),
    "user": (*SQL_VALUES, "DOCCHAT_COSMOS_ENDPOINT", "DOCCHAT_COSMOS_DATABASE"),
    "jit": (
        "DOCCHAT_COSMOS_GROUP_DATA_ACCESS",
        "DOCCHAT_COSMOS_ACCOUNT_NAME",
        "AZURE_RESOURCE_GROUP",
    ),
    # `AZURE_TENANT_ID` names the tenant of the sign-in proof's token endpoint.
    "testusers rotate": ("TEST_CLIENT_ID", "API_SCOPE", "AZURE_TENANT_ID", *SQL_VALUES),
    "testusers check": (),
    "trace": ("DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID",),
    # Section 2.2's table has no row for `document`: `fetch-source` reads the version's row in
    # SQL and its source from the data account's blob endpoint.
    "document": (*SQL_VALUES, "DOCCHAT_STORAGE_BLOB_ENDPOINT"),
}
"""The values each group (or command) reads from `.azure/<env>/.env`, section 2.2's table."""

_KEY_RE: Final = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
_DOUBLE_QUOTED_ESCAPES: Final = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\", "$": "$"}
_SQL_SERVER_RE: Final = re.compile(SQL_SERVER_PATTERN)
_NIL_UUID: Final = "00000000-0000-0000-0000-000000000000"


def _refresh(environment: str) -> str:
    return f"run azd env refresh -e {environment}"


def repository_root(start: Path) -> Path:
    """The checkout's root: the nearest directory at or above `start` holding `azure.yaml`."""
    for directory in (start, *start.parents):
        if (directory / PROJECT_MARKER).is_file():
            return directory
    raise UsageError(
        "no checkout of the repository at or above the working directory",
        "run docchat-ops from a checkout synced with uv sync --locked",
    )


def _double_quoted(body: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if char == "\\" and index + 1 < len(body):
            following = body[index + 1]
            out.append(_DOUBLE_QUOTED_ESCAPES.get(following, "\\" + following))
            index += 2
            continue
        if char == '"':
            break
        out.append(char)
        index += 1
    return "".join(out)


def _value(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith('"'):
        return _double_quoted(raw[1:])
    if raw.startswith("'"):
        end = raw.find("'", 1)
        return raw[1:] if end < 0 else raw[1:end]
    # An unquoted value ends where a comment starts, which needs a blank before the `#`.
    comment = re.search(r"\s#", raw)
    return (raw[: comment.start()] if comment else raw).strip()


def parse_dotenv(text: str) -> dict[str, str]:
    """The `KEY=value` lines of a dotenv file as azd writes them (values usually double-quoted);
    blank lines and `#` comments are skipped."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        found = _KEY_RE.match(stripped)
        if found:
            values[found.group(1)] = _value(found.group(2))
    return values


@dataclass(frozen=True)
class SqlTarget:
    """The SQL server and database a command connects to."""

    server: str
    database: str


@dataclass(frozen=True)
class AzdEnvironment:
    """The values of one `.azure/<env>/.env`."""

    environment: str
    values: dict[str, str]

    def require(self, name: str) -> str:
        """A value that must be present and not empty (exit 2 otherwise)."""
        value = self.values.get(name, "")
        if not value:
            raise UsageError(
                f"{name} is missing from .azure/{self.environment}/.env",
                _refresh(self.environment),
            )
        return value

    def require_all(self, names: tuple[str, ...]) -> None:
        for name in names:
            self.require(name)

    def require_uuid(self, name: str) -> str:
        """A UUID value in lower-case canonical form; it may become part of a URL path."""
        value = self.require(name)
        try:
            return str(parse_uuid(value))
        except ValueError:
            raise UsageError(
                f"{name} in .azure/{self.environment}/.env isn't a UUID",
                _refresh(self.environment),
            ) from None

    def require_flag(self, name: str) -> bool:
        """A boolean output (`true` or `false`, any case)."""
        value = self.require(name).lower()
        if value not in {"true", "false"}:
            raise UsageError(
                f"{name} in .azure/{self.environment}/.env isn't true or false",
                _refresh(self.environment),
            )
        return value == "true"

    def sql_target(self) -> SqlTarget:
        server = self.require("DOCCHAT_SQL_SERVER")
        database = self.require("DOCCHAT_SQL_DATABASE")
        if not _SQL_SERVER_RE.fullmatch(server) or database != SQL_DATABASE_NAME:
            raise UsageError(
                f"the SQL values in .azure/{self.environment}/.env aren't a DocChat database",
                _refresh(self.environment),
            )
        return SqlTarget(server=server, database=database)


def load_azd_environment(root: Path, environment: str) -> AzdEnvironment:
    """Read `.azure/<env>/.env` of the checkout (exit 2 when it is absent)."""
    path = root / ".azure" / environment / ".env"
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        raise UsageError(
            f"the azd environment file .azure/{environment}/.env is missing",
            _refresh(environment),
        ) from None
    return AzdEnvironment(environment=environment, values=parse_dotenv(text))


def read_checkout_json(root: Path, relative: str) -> object:
    """A JSON file of the checkout, such as `infra/env/<env>.json` (exit 2 when it is missing or
    isn't JSON)."""
    try:
        return json.loads((root / relative).read_text(encoding="utf-8"))
    except (FileNotFoundError, NotADirectoryError):
        raise UsageError(
            f"{relative} is missing", "run docchat-ops from a checkout of main"
        ) from None
    except json.JSONDecodeError:
        raise UsageError(f"{relative} isn't valid JSON", "restore it from main") from None


def object_id(value: object, where: str) -> str:
    """An object ID read from a checkout file: a UUID that isn't the nil UUID the repository
    commits until the bootstrap writes the real one."""
    text = value if isinstance(value, str) else ""
    try:
        canonical = str(parse_uuid(text))
    except ValueError:
        raise UsageError(f"{where} isn't an object ID", "restore it from main") from None
    if canonical == _NIL_UUID:
        raise UsageError(f"{where} isn't set yet", "run the bootstrap's groups step first")
    return canonical
