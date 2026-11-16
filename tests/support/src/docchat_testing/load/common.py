"""What the `docchat-load` commands share (`84-testing.md` section 7): the load users, the azd
environment file, private output files, the job summary and times."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from docchat_testing.tokens import REPOSITORY_ROOT

LOAD_USERS: Final = tuple(f"load-{number:02d}" for number in range(1, 21))
"""The 20 load users' aliases; chat and cheap requests take them round-robin."""
ADMIN_ALIAS: Final = "load-admin"
SESSION_USER: Final = "load-01"
"""The user whose 500 seeded sessions S1's `sessions` scenario lists."""
PROJECT_NAME: Final = "Load test"
RESOURCE_GROUP: Final = "rg-docchat-load"
ENVIRONMENT: Final = "load"

QUESTIONS_FILE: Final = REPOSITORY_ROOT / "tests" / "fixtures" / "questions" / "load.json"
K6_LOCK_FILE: Final = REPOSITORY_ROOT / "tests" / "load" / "k6.lock.json"

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_USAGE: Final = 2

SUMMARY_VARIABLE: Final = "GITHUB_STEP_SUMMARY"
OUTPUT_VARIABLE: Final = "GITHUB_OUTPUT"

_KEY: Final = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_RANGE: Final = re.compile(r"^(?P<prefix>[a-z-]*?)(?P<first>\d+)\.\.(?P=prefix)(?P<last>\d+)$")


class LoadError(Exception):
    """A command can't do its work; `code` names why and the message says what to look at. The
    message never holds a token, a password or a header."""

    exit_code: int = EXIT_FAILED

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class UsageError(LoadError):
    """An argument or an input file the command refuses before it calls anything."""

    exit_code = EXIT_USAGE


def parse_aliases(text: str) -> list[str]:
    """`load-01..load-20,load-admin`: comma-separated aliases, where `<prefix><n>..<prefix><m>`
    stands for every alias from n to m with the digits of n."""
    aliases: list[str] = []
    for part in (item.strip() for item in text.split(",")):
        if not part:
            continue
        found = _RANGE.match(part)
        if found is None:
            aliases.append(part)
            continue
        first, last = int(found["first"]), int(found["last"])
        if last < first:
            raise UsageError("aliases_invalid", f"the range {part} runs backwards")
        width = len(found["first"])
        aliases += [f"{found['prefix']}{n:0{width}d}" for n in range(first, last + 1)]
    if not aliases:
        raise UsageError("aliases_invalid", "no alias given")
    return list(dict.fromkeys(aliases))


def _unquote(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        return raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1]
    return raw


def parse_dotenv(text: str) -> dict[str, str]:
    """The `KEY=value` lines of an azd environment file; values are usually double-quoted."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        found = _KEY.match(stripped)
        if found:
            values[found.group(1)] = _unquote(found.group(2))
    return values


def read_azd_environment(
    environment: str = ENVIRONMENT, root: Path = REPOSITORY_ROOT
) -> dict[str, str]:
    """`.azure/<env>/.env`, which the job's `azd env refresh` wrote (84-testing.md section 7.5
    step 2)."""
    path = root / ".azure" / environment / ".env"
    try:
        return parse_dotenv(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise UsageError(
            "azd_environment_missing",
            f".azure/{environment}/.env can't be read: run azd env refresh",
        ) from error


def setting(
    name: str, *, environ: Mapping[str, str] | None = None, azd: Mapping[str, str] | None = None
) -> str:
    """A value from the job's environment, else from the azd environment file."""
    value = (os.environ if environ is None else environ).get(name) or (azd or {}).get(name, "")
    if not value:
        raise UsageError("setting_missing", f"{name} is set neither in the job nor in the azd file")
    return value


def write_private(path: Path, text: str) -> None:
    """Write `text` to a file only its owner can read (mode 0600, whatever the umask)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(text)


def write_json(path: Path, value: object, *, private: bool = False) -> None:
    text = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if private:
        write_private(path, text)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def read_json(path: Path, what: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise UsageError("input_unreadable", f"{what} ({path}) can't be read") from error


def read_tokens(path: Path) -> dict[str, str]:
    """The token file `docchat-load tokens` wrote: `{"tokens": {alias: token}}`."""
    content = read_json(path, "the token file")
    tokens = content.get("tokens") if isinstance(content, dict) else None
    if not isinstance(tokens, dict) or not all(
        isinstance(key, str) and isinstance(value, str) and value for key, value in tokens.items()
    ):
        raise UsageError("tokens_invalid", f"{path} isn't a token file of docchat-load tokens")
    return dict(tokens)


def token_of(tokens: Mapping[str, str], alias: str) -> str:
    token = tokens.get(alias)
    if not token:
        raise UsageError("tokens_invalid", f"the token file holds no token for {alias}")
    return token


def load_questions(path: Path = QUESTIONS_FILE) -> list[str]:
    """The load questions of `tests/fixtures/questions/load.json`."""
    content = read_json(path, "the load questions")
    if (
        not isinstance(content, list)
        or not content
        or not all(isinstance(item, str) and item for item in content)
    ):
        raise UsageError("questions_invalid", f"{path} isn't a list of questions")
    return list(content)


def parse_time(value: str) -> datetime:
    """An ISO 8601 time; one without an offset is UTC."""
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError as error:
        raise UsageError("time_invalid", f"{value!r} isn't an ISO 8601 time") from error
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def iso(moment: datetime) -> str:
    """UTC with milliseconds and `Z`."""
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def utc_now() -> datetime:
    return datetime.now(UTC)


def append_summary(markdown: str, environ: Mapping[str, str] | None = None) -> None:
    """Append to the job summary when the command runs in a workflow job."""
    path = (os.environ if environ is None else environ).get(SUMMARY_VARIABLE)
    if path:
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(markdown.rstrip("\n") + "\n")


def emit(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def emit_error(error: LoadError) -> None:
    sys.stderr.write(json.dumps({"code": error.code, "message": str(error)}) + "\n")
    sys.stderr.flush()
