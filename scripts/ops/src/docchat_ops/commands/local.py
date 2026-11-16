"""`local init` (`86-operations.md` section 2.8.1; `81-config.md` section 2.11; `01-repository.md`
section 3.6): writes the git-ignored `.env` at the repository root from `.env.example`, with a new
`DOCCHAT_LOCAL_SQL_SA_PASSWORD` for the compose SQL Server.

The password is 24 characters drawn with `secrets.choice` from upper-case letters, lower-case
letters, digits and `!#%+-=?@^_`, redrawn until it holds one of each class, which is SQL Server's
complexity rule; none of the symbols needs quoting in a dotenv file or in Compose interpolation.
The command reads no Azure store and writes no audit row.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import string
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from docchat_core.settings import LOCAL_SQL_SA_PASSWORD_LENGTH
from docchat_ops.framework.endpoints import LOCAL_ENV_EXAMPLE, LOCAL_ENV_FILE
from docchat_ops.framework.exitcodes import UsageError

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from docchat_ops.cli import Run

PASSWORD_NAME: Final = "DOCCHAT_LOCAL_SQL_SA_PASSWORD"  # noqa: S105 - a setting's name
PASSWORD_SYMBOLS: Final = "!#%+-=?@^_"  # noqa: S105 - the alphabet, not a password
PASSWORD_CLASSES: Final = (
    string.ascii_uppercase,
    string.ascii_lowercase,
    string.digits,
    PASSWORD_SYMBOLS,
)
PASSWORD_ALPHABET: Final = "".join(PASSWORD_CLASSES)
FILE_MODE: Final = 0o600


def new_password(choice: Callable[[Sequence[str]], str] = secrets.choice) -> str:
    """24 characters from `secrets.choice`, holding at least one character of each class."""
    while True:
        password = "".join(choice(PASSWORD_ALPHABET) for _ in range(LOCAL_SQL_SA_PASSWORD_LENGTH))
        if all(any(char in characters for char in password) for characters in PASSWORD_CLASSES):
            return password


def render(example: str, password: str) -> str:
    """`.env.example` with the password line filled in (appended when the template lacks it)."""
    prefix = f"{PASSWORD_NAME}="
    lines = example.splitlines()
    filled = [f"{prefix}{password}" if line.startswith(prefix) else line for line in lines]
    if filled == lines:
        filled.append(f"{prefix}{password}")
    return "\n".join(filled) + "\n"


def write_private(path: Path, content: str) -> None:
    """Write the file with mode 0600 from its first byte, then move it into place, so neither a
    readable copy nor a half-written `.env` ever exists."""
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, FILE_MODE)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


@dataclass(frozen=True)
class _Files:
    example: str
    exists: bool


def _read(root: Path) -> _Files:
    try:
        example = (root / LOCAL_ENV_EXAMPLE).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise UsageError(
            f"{LOCAL_ENV_EXAMPLE} is missing", "run docchat-ops from a checkout of main"
        ) from None
    return _Files(example=example, exists=(root / LOCAL_ENV_FILE).exists())


def init_arguments(parser: argparse.ArgumentParser) -> None:
    """`local init` takes no argument beyond the global options."""
    del parser


async def init(run: Run, args: argparse.Namespace) -> None:
    del args
    root = run.root()
    files = await asyncio.to_thread(_read, root)
    if files.exists:
        await run.confirm(
            [
                f"target: {LOCAL_ENV_FILE} exists and is replaced",
                (
                    f"change: a new {PASSWORD_NAME}; the SQL Server volume keeps the old one "
                    "until docker compose down -v"
                ),
            ]
        )
    content = render(files.example, new_password())
    await asyncio.to_thread(write_private, root / LOCAL_ENV_FILE, content)
    run.report("written", {"path": LOCAL_ENV_FILE, "replaced": files.exists})
