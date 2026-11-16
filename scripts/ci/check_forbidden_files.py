"""Fail when a tracked path matches a forbidden pattern (01-repository.md sections 1.12 and 5.7).

Runs `git ls-files -z` in the repository root and names every tracked path that matches `*.zip`,
`*.tar`, `*.tgz`, `*.gz`, `*.7z`, `*.rar`, `*.jar`, `*.whl`, `*.map`, `*.pfx`, `*.p12`,
`**/dist/**`, `**/build/**`, `**/node_modules/**`, a final component exactly `.env`, `.azure/**` or
`apps/web/config.local.json`. There is no allowlist: archives and build output can carry keys and
source maps that no text scanner reads, so a file forced past `.gitignore` fails too.

Usage: uv run --frozen python scripts/ci/check_forbidden_files.py [--root <repository>]
Exit codes: 0 nothing forbidden is tracked; 1 at least one forbidden path; 2 git failed.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

FORBIDDEN_SUFFIXES = (
    ".zip",
    ".tar",
    ".tgz",
    ".gz",
    ".7z",
    ".rar",
    ".jar",
    ".whl",
    ".map",
    ".pfx",
    ".p12",
)
FORBIDDEN_DIRECTORIES = ("dist", "build", "node_modules", ".azure")
FORBIDDEN_FILE_NAME = ".env"
FORBIDDEN_PATHS = ("apps/web/config.local.json",)


def forbidden_pattern(path: str) -> str | None:
    """Return the forbidden pattern `path` matches, or None when it matches none."""
    if path in FORBIDDEN_PATHS:
        return path
    parts = PurePosixPath(path).parts
    name = parts[-1]
    if name == FORBIDDEN_FILE_NAME:
        return "a final component exactly .env"
    # Upper-case extensions are still archives or key stores, so the suffix test ignores case.
    lowered = name.lower()
    for suffix in FORBIDDEN_SUFFIXES:
        if lowered.endswith(suffix):
            return f"*{suffix}"
    for directory in parts[:-1]:
        if directory in FORBIDDEN_DIRECTORIES:
            return f"**/{directory}/**" if directory != ".azure" else ".azure/**"
    return None


def find_forbidden(paths: Iterable[str]) -> list[tuple[str, str]]:
    """Return (path, pattern) for every path that matches a forbidden pattern, in order."""
    found: list[tuple[str, str]] = []
    for path in paths:
        pattern = forbidden_pattern(path)
        if pattern is not None:
            found.append((path, pattern))
    return found


def tracked_paths(root: Path) -> list[str]:
    """List the paths git tracks under `root`, exactly as `git ls-files -z` prints them."""
    git = shutil.which("git")
    if git is None:
        raise FileNotFoundError("git is not on PATH")
    # The arguments are constants; only the working directory varies.
    completed = subprocess.run(  # noqa: S603
        [git, "ls-files", "-z"], cwd=root, check=True, capture_output=True
    )
    return [os.fsdecode(entry) for entry in completed.stdout.split(b"\0") if entry]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail when git tracks a forbidden path.")
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT)
    args = parser.parse_args(argv)
    try:
        paths = tracked_paths(args.root)
    except (OSError, subprocess.CalledProcessError) as error:
        sys.stderr.write(f"check_forbidden_files: git ls-files failed: {error}\n")
        return 2
    found = find_forbidden(paths)
    for path, pattern in found:
        sys.stdout.write(f"forbidden tracked file: {path} (matches {pattern})\n")
    if found:
        sys.stdout.write(
            f"{len(found)} tracked file(s) match a forbidden pattern of "
            "01-repository.md section 1.12; remove them with git rm --cached.\n"
        )
        return 1
    sys.stdout.write(f"no forbidden file among {len(paths)} tracked paths\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
