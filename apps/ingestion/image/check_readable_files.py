"""Build-time check that no file a parse user can read holds a secret (`14-ingestion.md` section
15.2 stage `runtime` step 5 and section 15.11 item 6).

Run by the ingestion image's last step as `python -I apps/ingestion/image/check_readable_files.py`,
with the standard library only. It lists every regular file that any user can read (mode bit
`o+r`) outside the directories the image's packages and code own, and fails, naming each file,
when one is a private key, a `.pfx`, `.p12`, `.pem` or `.key` file, a `.env` file or a file named
`credentials`, or matches a secret pattern of the repository's gitleaks configuration.

The repository's configuration extends gitleaks' default rules, which live inside the gitleaks
binary, so the image step passes the pinned binary with `--gitleaks` and every listed file is also
scanned by it; the `[[rules]]` a configuration file declares are matched here directly.

Exit codes: 0 when nothing is found, 1 when a file is found, 2 for a usage or configuration error.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import subprocess
import sys
import tomllib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

ALLOWED: Final = (
    "/usr",
    "/lib",
    "/lib64",
    "/bin",
    "/sbin",
    "/etc/alternatives",
    "/etc/fonts",
    "/opt/venv",
    "/opt/docchat",
    "/opt/microsoft",
    "/FuncExtensionBundles",
    "/azure-functions-host",
    "/home/site/wwwroot",
)
"""Directories whose world-readable files are the image's packages, models and code."""

PSEUDO: Final = ("/proc", "/sys", "/dev")
KEY_SUFFIXES: Final = (".pfx", ".p12", ".pem", ".key")
CREDENTIALS: Final = "credentials"
PRIVATE_KEY: Final = re.compile(rb"-----BEGIN[ A-Z0-9]*PRIVATE KEY( BLOCK)?-----")
READ_LIMIT: Final = 16 * 1024 * 1024
GITLEAKS_LEAK_EXIT: Final = 2


@dataclass(frozen=True)
class Rule:
    rule_id: str
    pattern: re.Pattern[bytes]
    keywords: tuple[bytes, ...]

    def matches(self, data: bytes) -> bool:
        lowered = data.lower()
        if self.keywords and not any(keyword in lowered for keyword in self.keywords):
            return False
        return self.pattern.search(data) is not None


class ConfigurationError(Exception):
    """A gitleaks configuration that can't be read or whose rule can't be compiled."""


def load_rules(path: Path) -> list[Rule]:
    """The `[[rules]]` of a gitleaks configuration file: each `regex`, with its `keywords` as the
    prefilter gitleaks uses."""
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as failure:
        raise ConfigurationError(f"can't read {path}") from failure
    rules: list[Rule] = []
    for entry in document.get("rules", []):
        regex = entry.get("regex")
        if not isinstance(regex, str):
            continue
        try:
            pattern = re.compile(regex.encode("utf-8"))
        except re.error as failure:
            raise ConfigurationError(f"rule {entry.get('id')} doesn't compile") from failure
        keywords = tuple(
            str(keyword).lower().encode("utf-8") for keyword in entry.get("keywords", [])
        )
        rules.append(Rule(str(entry.get("id", "")), pattern, keywords))
    return rules


def extends_defaults(path: Path) -> bool:
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as failure:
        raise ConfigurationError(f"can't read {path}") from failure
    extend = document.get("extend", {})
    return isinstance(extend, dict) and extend.get("useDefault") is True


def _allowed(image_path: PurePosixPath) -> bool:
    text = str(image_path)
    return any(text == prefix or text.startswith(prefix + "/") for prefix in (*ALLOWED, *PSEUDO))


def readable_files(
    root: Path, excluded: Sequence[Path] = ()
) -> Iterator[tuple[PurePosixPath, Path]]:
    """Every regular file under `root` that others can read, outside the allowed directories, on
    the root's file system only, following no link; yields its path in the image and on disk."""
    device = os.lstat(root).st_dev
    skipped = {path.resolve() for path in excluded}
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            path = Path(entry.path)
            image_path = PurePosixPath("/") / path.relative_to(root)
            if _allowed(image_path) or path.resolve() in skipped:
                continue
            try:
                status = os.lstat(path)
            except OSError:
                continue
            if status.st_dev != device:
                continue
            if stat.S_ISDIR(status.st_mode):
                stack.append(path)
            elif stat.S_ISREG(status.st_mode) and status.st_mode & stat.S_IROTH:
                yield image_path, path


def finding(  # noqa: PLR0911 - one return per kind of finding
    image_path: PurePosixPath, path: Path, rules: Sequence[Rule]
) -> str | None:
    """Why a readable file fails the check, or `None`."""
    name = image_path.name
    if name.lower().endswith(KEY_SUFFIXES):
        return "key file"
    if name == ".env" or name.endswith(".env"):
        return ".env file"
    if name == CREDENTIALS:
        return "credentials file"
    try:
        with path.open("rb") as file:
            data = file.read(READ_LIMIT)
    except OSError:
        return None
    if PRIVATE_KEY.search(data):
        return "private key"
    for rule in rules:
        if rule.matches(data):
            return f"gitleaks rule {rule.rule_id}"
    return None


def gitleaks_finds(gitleaks: Path, config: Path, path: Path) -> bool:
    """Whether the pinned gitleaks binary reports a leak in one file."""
    completed = subprocess.run(  # noqa: S603 - the pinned binary and paths this script built
        [
            str(gitleaks),
            "dir",
            "--no-banner",
            "--redact",
            "--log-level",
            "error",
            "--exit-code",
            str(GITLEAKS_LEAK_EXIT),
            "--config",
            str(config),
            str(path),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if completed.returncode not in (0, GITLEAKS_LEAK_EXIT):
        raise ConfigurationError("gitleaks failed")
    return completed.returncode == GITLEAKS_LEAK_EXIT


def arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--root", type=Path, default=Path("/"), help="the image's root directory")
    parser.add_argument(
        "--config", type=Path, default=Path(".gitleaks.toml"), help="the gitleaks configuration"
    )
    parser.add_argument("--gitleaks", type=Path, help="the pinned gitleaks binary")
    parser.add_argument(
        "--exclude", type=Path, action="append", default=[], help="a directory to leave out"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    options = arguments(sys.argv[1:] if argv is None else argv)
    try:
        found = scan(options)
    except ConfigurationError as failure:
        print(f"check_readable_files: {failure}", file=sys.stderr)  # noqa: T201
        return 2
    return 1 if found else 0


def scan(options: argparse.Namespace) -> int:
    """Name every readable file that fails the check; returns how many did."""
    rules = load_rules(options.config)
    if extends_defaults(options.config) and options.gitleaks is None:
        raise ConfigurationError("the configuration extends gitleaks' defaults: pass --gitleaks")
    found = 0
    for image_path, path in readable_files(options.root, options.exclude):
        reason = finding(image_path, path, rules)
        if (
            reason is None
            and options.gitleaks is not None
            and gitleaks_finds(options.gitleaks, options.config, path)
        ):
            reason = "gitleaks"
        if reason is not None:
            found += 1
            print(f"readable secret: {image_path} ({reason})", file=sys.stderr)  # noqa: T201
    return found


if __name__ == "__main__":
    sys.exit(main())
