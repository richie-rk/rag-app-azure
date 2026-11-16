"""Hold `tests/load/k6.lock.json` to the k6 release's checksums file (84-testing.md section 7.1).

The lock pins the k6 release the load generator downloads: `version`, the tarball `url`, its
`sha256` and the binary's `member` path inside the tarball. The check reads the official
`k6-v<version>-checksums.txt` of that release from GitHub and fails when the lock's SHA-256 differs
from the line for the tarball, or when the URL or member path isn't the one the version implies, so
the lock can only name the official linux-amd64 build of its own version (ADR 0135).

Usage: uv run --frozen python scripts/ci/check_k6_lock.py [--lock tests/load/k6.lock.json]
Exit codes: 0 the lock matches; 1 it doesn't, or it or the checksums file can't be read.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RELEASE_BASE = "https://github.com/grafana/k6/releases/download"
LOCK_KEYS = frozenset({"version", "url", "sha256", "member"})
VERSION = re.compile(r"^\d+\.\d+\.\d+$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
CHECKSUM_LINE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(\S+)$")
FETCH_TIMEOUT_SECONDS = 30
FETCH_ATTEMPTS = 3
FETCH_RETRY_SECONDS = 10.0


class ChecksumsUnavailableError(RuntimeError):
    """The release's checksums file couldn't be downloaded."""


def tarball_name(version: str) -> str:
    return f"k6-v{version}-linux-amd64.tar.gz"


def checksums_url(version: str) -> str:
    return f"{RELEASE_BASE}/v{version}/k6-v{version}-checksums.txt"


def fetch_checksums(url: str) -> str:
    """Download a release checksums file; the release host redirects to its storage."""
    if not url.startswith(f"{RELEASE_BASE}/"):
        raise ChecksumsUnavailableError(f"{url} is not a k6 release file")
    last_error: Exception | None = None
    for attempt in range(FETCH_ATTEMPTS):
        if attempt:
            time.sleep(FETCH_RETRY_SECONDS)
        try:
            # The URL is built from the fixed https release base above, never from input.
            with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_SECONDS) as response:  # noqa: S310
                body: bytes = response.read()
            return body.decode("utf-8")
        except (urllib.error.URLError, TimeoutError, UnicodeDecodeError) as error:
            last_error = error
    raise ChecksumsUnavailableError(f"{url}: {last_error}")


def parse_checksums(text: str) -> dict[str, str]:
    """Map each file name of a `sha256sum`-style checksums file to its lower-case SHA-256."""
    checksums: dict[str, str] = {}
    for line in text.splitlines():
        match = CHECKSUM_LINE.match(line.strip())
        if match:
            checksums[match.group(2)] = match.group(1).lower()
    return checksums


def lock_problems(lock: Any) -> list[str]:
    """Return the shape problems of a parsed lock: keys, version, URL, SHA-256 and member."""
    if not isinstance(lock, dict):
        return ["the lock must be a JSON object"]
    problems: list[str] = []
    if set(lock) != LOCK_KEYS:
        problems.append(f"the lock must have exactly the keys {', '.join(sorted(LOCK_KEYS))}")
    version = lock.get("version")
    if not isinstance(version, str) or not VERSION.match(version):
        return [*problems, f"version {version!r} is not a release version such as 2.3.0"]
    expected_url = f"{RELEASE_BASE}/v{version}/{tarball_name(version)}"
    if lock.get("url") != expected_url:
        problems.append(f"url is {lock.get('url')!r}; version {version} needs {expected_url}")
    expected_member = f"k6-v{version}-linux-amd64/k6"
    if lock.get("member") != expected_member:
        problems.append(
            f"member is {lock.get('member')!r}; version {version} needs {expected_member}"
        )
    sha256 = lock.get("sha256")
    if not isinstance(sha256, str) or not SHA256.match(sha256):
        problems.append("sha256 must be 64 lower-case hexadecimal characters")
    return problems


def check_lock(lock: Any, checksums_text: str) -> list[str]:
    """Compare a parsed lock with the text of its version's checksums file."""
    problems = lock_problems(lock)
    if problems:
        return problems
    version = lock["version"]
    recorded = parse_checksums(checksums_text).get(tarball_name(version))
    if recorded is None:
        return [f"{checksums_url(version)} lists no {tarball_name(version)}"]
    if recorded != lock["sha256"]:
        return [
            (
                f"sha256 {lock['sha256']} differs from {recorded}, the checksum the k6 "
                f"v{version} release publishes for {tarball_name(version)}"
            )
        ]
    return []


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hold the k6 lock to the release checksums.")
    parser.add_argument("--lock", type=Path, default=REPOSITORY_ROOT / "tests/load/k6.lock.json")
    args = parser.parse_args(argv)
    try:
        lock = json.loads(args.lock.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        sys.stdout.write(f"check_k6_lock: can't read {args.lock}: {error}\n")
        return 1
    problems = lock_problems(lock)
    if not problems:
        try:
            problems = check_lock(lock, fetch_checksums(checksums_url(lock["version"])))
        except ChecksumsUnavailableError as error:
            problems = [f"can't read the release checksums file: {error}"]
    for problem in problems:
        sys.stdout.write(f"{args.lock.name}: {problem}\n")
    if problems:
        return 1
    sys.stdout.write(f"{args.lock.name} matches the k6 v{lock['version']} checksums file\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
