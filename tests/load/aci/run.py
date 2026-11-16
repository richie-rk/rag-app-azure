"""The load generator's bootstrap (84-testing.md section 7.2; ADR 0135; T-579).

The container group runs `python /work/run.py` in place of the image's entrypoint. Standard library
only: it downloads `K6_URL` with `urllib.request.urlopen` (timeout 120 s, 3 attempts 15 s apart),
refuses the file unless its SHA-256 equals `K6_SHA256`, reads exactly the member `K6_MEMBER` with
`tarfile.extractfile` and writes its bytes to `/tmp/k6` (nothing is extracted by path, and the
tarball itself stays in memory), sets mode 0755, runs
`["/tmp/k6", "run", "--quiet", "/work/scenario.js"]` with inherited standard output, prints
`K6_EXIT <k6 exit code>` and exits 0.

A download, checksum or extraction failure prints `K6_BOOTSTRAP_FAILED <reason>`
(`download_failed`, `checksum_mismatch`, `member_missing`) and exits 0 as well: Container Instances
can restart a container that exits nonzero even under `restartPolicy: 'Never'`, which would run the
scenario twice, so the verdict reads the result from the marker lines instead (`docchat-load
verdict` fails a log with `K6_BOOTSTRAP_FAILED` or without `K6_EXIT`).

k6 runs with `K6_LOG_OUTPUT=stdout` and `K6_LOG_FORMAT=raw`, so the scenario's `console.log` marker
lines (`K6_FAILED_REQUEST`, `K6_SESSION`) reach standard output as bare lines beside
`K6_SUMMARY_JSON`, which `handleSummary` writes there itself.
"""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Final

RELEASE_BASE: Final = "https://github.com/grafana/k6/releases/download/"
DOWNLOAD_TIMEOUT_SECONDS: Final = 120
DOWNLOAD_ATTEMPTS: Final = 3
DOWNLOAD_RETRY_SECONDS: Final = 15.0
K6_PATH: Final = Path("/tmp/k6")  # noqa: S108 - the one file the bootstrap writes (ADR 0135)
SCENARIO: Final = "/work/scenario.js"
EXIT_NOT_STARTED: Final = 127
"""`K6_EXIT` when the binary can't be started at all, the shell's code for that case."""

type Fetch = Callable[[str], bytes]
type Sleep = Callable[[float], None]
type Runner = Callable[[Sequence[str], Mapping[str, str]], int]


class BootstrapError(Exception):
    """k6 couldn't be made ready; `reason` is the marker's reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def fetch(url: str) -> bytes:
    """One download of the release tarball; the release host redirects to its storage."""
    if not url.startswith(RELEASE_BASE):
        raise BootstrapError("download_failed")
    # The scheme and host are the fixed release base checked above (01-repository.md section 6.4).
    with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:  # noqa: S310
        body: bytes = response.read()
    return body


def download(url: str, *, fetcher: Fetch = fetch, sleep: Sleep = time.sleep) -> bytes:
    """The tarball's bytes after at most 3 attempts 15 s apart."""
    for attempt in range(DOWNLOAD_ATTEMPTS):
        if attempt:
            sleep(DOWNLOAD_RETRY_SECONDS)
        try:
            return fetcher(url)
        except (urllib.error.URLError, OSError, ValueError):
            continue
    raise BootstrapError("download_failed")


def verify(data: bytes, expected_sha256: str) -> None:
    if not expected_sha256 or hashlib.sha256(data).hexdigest() != expected_sha256.lower():
        raise BootstrapError("checksum_mismatch")


def member_bytes(data: bytes, member: str) -> bytes:
    """The bytes of exactly the regular-file member `member`, read without extracting anything."""
    if not member:
        raise BootstrapError("member_missing")
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            info = archive.getmember(member)
            stream = archive.extractfile(info) if info.isfile() else None
            if stream is None:
                raise BootstrapError("member_missing")
            with stream:
                return stream.read()
    except (KeyError, tarfile.TarError, EOFError, OSError) as error:
        raise BootstrapError("member_missing") from error


def write_binary(content: bytes, path: Path = K6_PATH) -> None:
    """Write the binary with mode 0755, replacing any earlier file at the path."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o755)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
        os.fchmod(handle.fileno(), 0o755)


def run_k6(command: Sequence[str], environment: Mapping[str, str]) -> int:
    # Standard output and standard error are inherited, so k6's lines reach the container log.
    return subprocess.run(list(command), env=dict(environment), check=False).returncode  # noqa: S603


def k6_environment(base: Mapping[str, str]) -> dict[str, str]:
    return {**base, "K6_LOG_OUTPUT": "stdout", "K6_LOG_FORMAT": "raw"}


def emit(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def main(
    environment: Mapping[str, str] | None = None,
    *,
    fetcher: Fetch = fetch,
    sleep: Sleep = time.sleep,
    runner: Runner = run_k6,
    binary: Path = K6_PATH,
) -> int:
    env = dict(os.environ if environment is None else environment)
    try:
        data = download(env.get("K6_URL", ""), fetcher=fetcher, sleep=sleep)
        verify(data, env.get("K6_SHA256", ""))
        write_binary(member_bytes(data, env.get("K6_MEMBER", "")), binary)
    except BootstrapError as error:
        emit(f"K6_BOOTSTRAP_FAILED {error.reason}")
        return 0
    try:
        code = runner([str(binary), "run", "--quiet", SCENARIO], k6_environment(env))
    except OSError:
        code = EXIT_NOT_STARTED
    emit(f"K6_EXIT {code}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
