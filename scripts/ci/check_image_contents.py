"""Fail when an image's file system holds a test file, a source map, `.env` or `.azure/` (T-523,
`84-testing.md` section 5.11; `01-repository.md` section 4.1 rule 7).

Reads the tar stream `docker export` writes for a container of the image and names every path
that breaks the rule:

- a final component exactly `.env`, or a directory `.azure`, anywhere in the image;
- in the files the repository puts into the image (the first-party packages `docchat_*` in the
  virtual environment, `/app`, `/home/site/wwwroot` and `/opt/docchat`): a source map
  (`*.map`), a test module (`test_*.py`, `*_test.py`, `conftest.py`) or a directory `tests` or
  `test`;
- the test-support package `docchat_testing`, which no image holds.

Third-party distributions keep their own test folders: the rule is about what the build context
can carry in, which the root `.dockerignore` keeps out and this check proves.

Usage: docker export <container> | uv run --frozen python scripts/ci/check_image_contents.py
       --image docchat/api
Exit codes: 0 nothing forbidden; 1 at least one forbidden path; 2 the input isn't a tar stream.
"""

from __future__ import annotations

import argparse
import sys
import tarfile
from pathlib import PurePosixPath
from typing import IO, TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

FIRST_PARTY_ROOTS = ("app", "home/site/wwwroot", "opt/docchat")
FIRST_PARTY_PACKAGE_PREFIX = "docchat_"
TEST_SUPPORT_PACKAGE = "docchat_testing"
TEST_DIRECTORIES = ("tests", "test")
REPORTED_PATHS = 50


def is_first_party(parts: Sequence[str]) -> bool:
    """Whether the path is a file the repository puts into an image."""
    path = "/".join(parts)
    if any(path == root or path.startswith(root + "/") for root in FIRST_PARTY_ROOTS):
        return True
    return any(part.startswith(FIRST_PARTY_PACKAGE_PREFIX) for part in parts[:-1])


def image_path(member: str) -> str:
    """A tar member's name as a path from the image's root, without `./` or `/` in front."""
    while member.startswith("./"):
        member = member[2:]
    return member.lstrip("/")


def is_test_file(parts: Sequence[str]) -> bool:
    name = parts[-1]
    if name == "conftest.py" or any(part in TEST_DIRECTORIES for part in parts[:-1]):
        return True
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def is_test_support(parts: Sequence[str]) -> bool:
    return any(
        part == TEST_SUPPORT_PACKAGE or part.startswith(f"{TEST_SUPPORT_PACKAGE}-")
        for part in parts
    )


def forbidden_reason(path: str) -> str | None:
    """Why `path` (as a tar member names it) may not be in an image, or None."""
    parts = PurePosixPath(image_path(path)).parts
    if not parts:
        return None
    reason: str | None = None
    if parts[-1] == ".env":
        reason = ".env"
    elif ".azure" in parts:
        reason = ".azure/"
    elif is_test_support(parts):
        reason = "the test-support package"
    elif is_first_party(parts) and parts[-1].lower().endswith(".map"):
        reason = "a source map"
    elif is_first_party(parts) and is_test_file(parts):
        reason = "a test file"
    return reason


def find_forbidden(paths: Iterable[str]) -> list[tuple[str, str]]:
    """(path, reason) for every forbidden path, in order."""
    found: list[tuple[str, str]] = []
    for path in paths:
        reason = forbidden_reason(path)
        if reason is not None:
            found.append((path, reason))
    return found


def member_names(stream: IO[bytes]) -> list[str]:
    """Every member name of a tar stream, read once from start to end."""
    with tarfile.open(fileobj=stream, mode="r|*") as archive:
        return [member.name for member in archive]


def main(argv: Sequence[str] | None = None, stdin: IO[bytes] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check an image's exported file system.")
    parser.add_argument("--image", required=True, help="the image the stream belongs to")
    args = parser.parse_args(argv)
    try:
        names = member_names(stdin if stdin is not None else sys.stdin.buffer)
    except tarfile.TarError as error:
        sys.stderr.write(f"{args.image}: the input isn't a tar stream: {error}\n")
        return 2
    found = find_forbidden(names)
    for path, reason in found[:REPORTED_PATHS]:
        sys.stderr.write(f"::error::{args.image} holds {reason}: /{image_path(path)}\n")
    if len(found) > REPORTED_PATHS:
        sys.stderr.write(f"{args.image}: {len(found) - REPORTED_PATHS} more forbidden paths\n")
    if found:
        return 1
    sys.stdout.write(
        f"{args.image}: no test file, source map, .env or .azure/ among {len(names)} paths\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
