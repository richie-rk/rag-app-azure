"""The ingestion image's offline self-check, `python -I -m docchat_ingestion.selfcheck`
(`14-ingestion.md` section 15.5), and `run_isolation_probe()`.

The image build's last step runs it as root with no network. Every conversion and every partition
runs through the parsing runner, under a parse user of the pool with the system-call filter and
`failOnNetwork`, so a model that tries a download, or a parser or converter that needs a socket
of any family but `AF_UNIX` (the `convert` child's filter, section 15.11 item 7.3), fails the
build. It prints only the names of the checks that failed, never document content.
"""

from __future__ import annotations

import asyncio
import grp
import os
import pwd
import stat
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from docchat_ingestion.isolation import convert, syscall_filter
from docchat_ingestion.isolation.results import (
    DOCX,
    PDF,
    PPTX,
    SOURCE,
    ExtractPartV1,
    InspectOk,
    PageRange,
)
from docchat_ingestion.isolation.runner import (
    Layout,
    ParsePool,
    ParseSlot,
    child_environment,
)

FIXTURES: Final = Path("apps/ingestion/image/selfcheck")
PDF_FIXTURE: Final = "selfcheck.pdf"
OFFICE_FIXTURES: Final = {
    "selfcheck.docx": (DOCX, "DOCCHAT SELFCHECK DOCX MARKER"),
    "selfcheck.pptx": (PPTX, "DOCCHAT SELFCHECK PPTX MARKER"),
}
"""Each Office fixture, its media type and the marker text its converted PDF must carry."""

ENCODINGS: Final = ("cl100k_base", "o200k_base")
ODBC_DRIVER: Final = "ODBC Driver 18 for SQL Server"
LIBREOFFICE_PACKAGE: Final = "libreoffice-core-nogui"
PARSE_USER_PREFIX: Final = "docchat-parse-"
IDENTITY_VARIABLES: Final = ("IDENTITY_ENDPOINT", "IDENTITY_HEADER")
MEMORY_LIMIT_MB: Final = 2048
PARSE_TIMEOUT_SECONDS: Final = 1200
MAX_PAGES: Final = 2000
PART_PAGES: Final = 40
LOCK_MODE: Final = 0o600
SCRATCH_PARENT_MODE: Final = 0o711


PROBE_FILTERS: Final = (syscall_filter.DEFAULT, syscall_filter.CONVERT)
"""The isolation probe's children: the default probe, under which every socket fails, and the
`convert`-mode probe, under the `convert` child's filter (section 15.11 item 7.3)."""
PROBE_FAILURE: Final = "isolation_probe"


def _default_pool() -> ParsePool:
    return ParsePool(
        slots_per_process=1,
        memory_limit_mb=MEMORY_LIMIT_MB,
        parse_timeout_seconds=PARSE_TIMEOUT_SECONDS,
    )


async def failed_probes(pool: ParsePool) -> list[str]:
    """Start each `probe` child of `PROBE_FILTERS` through the parsing runner; the names of those
    that failed: `isolation_probe` for the default probe, `isolation_probe:convert` for the
    `convert`-mode probe."""
    failures: list[str] = []
    for filter_name in PROBE_FILTERS:
        if not await pool.probe(filter_name):
            suffix = "" if filter_name == syscall_filter.DEFAULT else f":{filter_name}"
            failures.append(PROBE_FAILURE + suffix)
    return failures


async def run_isolation_probe(pool: ParsePool | None = None) -> bool:
    """The isolation probe: the default probe and the `convert`-mode probe, each one child
    through the parsing runner; `True` only when both exit with code 0 and a valid result that
    reports every check true. The worker's start-up check 7 and this self-check run it."""
    return not await failed_probes(_default_pool() if pool is None else pool)


def installed_package_version(
    package: str, run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
) -> str:
    """The installed Debian package's version, from `dpkg-query`."""
    completed = run(
        ["dpkg-query", "-W", "-f=${Version}", package],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def check_encodings() -> bool:
    """Both `tiktoken` encodings load from `TIKTOKEN_CACHE_DIR`, with no network."""
    import tiktoken  # noqa: PLC0415 - loaded only by the check that needs it

    for name in ENCODINGS:
        tiktoken.get_encoding(name)
    return True


ODBC_CHECK: Final = f"import docchat_core, pyodbc; pyodbc.drivers().index({ODBC_DRIVER!r})"
"""The image's ODBC smoke check (section 15.2 stage `runtime` step 5). Only the SQL engine
imports `pyodbc` (import contract 5), so the self-check runs it in a fresh interpreter."""


def check_odbc(run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> bool:
    """The ODBC driver the data-access layer uses is registered; no connection is opened."""
    completed = run(
        [sys.executable, "-I", "-c", ODBC_CHECK],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0


def check_pool_layout(layout: Layout) -> list[str]:
    """The image's parse users, lock files and scratch parent (section 15.2 step 2), and the
    worker's UID 0, which `setpriv` needs."""
    failures: list[str] = []
    if os.geteuid() != 0:
        failures.append("uid_0")
    for index, uid in enumerate(layout.uids):
        name = f"{PARSE_USER_PREFIX}{index}"
        try:
            user = pwd.getpwnam(name)
            group = grp.getgrnam(name)
        except KeyError:
            failures.append(f"parse_user:{name}")
            continue
        if (user.pw_uid, user.pw_gid, group.gr_gid) != (uid, uid, uid):
            failures.append(f"parse_user:{name}")
        lock = os.lstat(layout.lock_path(uid)) if layout.lock_path(uid).exists() else None
        if (
            lock is None
            or not stat.S_ISREG(lock.st_mode)
            or lock.st_uid != 0
            or stat.S_IMODE(lock.st_mode) != LOCK_MODE
        ):
            failures.append(f"parse_lock:{uid}")
    scratch = os.lstat(layout.scratch_parent) if layout.scratch_parent.exists() else None
    if (
        scratch is None
        or not stat.S_ISDIR(scratch.st_mode)
        or scratch.st_uid != 0
        or stat.S_IMODE(scratch.st_mode) != SCRATCH_PARENT_MODE
    ):
        failures.append("scratch_parent")
    # A child's environment comes from an allowlist; the worker's identity variables never reach it.
    worker = {**os.environ, **dict.fromkeys((*IDENTITY_VARIABLES, "MSI_ENDPOINT"), "set")}
    environment = child_environment(layout.scratch_parent, worker)
    if any(name in IDENTITY_VARIABLES or name.startswith("MSI_") for name in environment):
        failures.append("child_environment")
    return failures


def check_converter(
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    libreoffice: Path = convert.LIBREOFFICE_DIRECTORY,
) -> list[str]:
    """The pinned converter is the installed one and holds no online-update component."""
    failures: list[str] = []
    if installed_package_version(LIBREOFFICE_PACKAGE, run) != convert.LIBREOFFICE_PACKAGE_VERSION:
        failures.append("libreoffice_version")
    if convert.update_check_present(libreoffice):
        failures.append("libreoffice_update_check")
    return failures


def _texts(part: ExtractPartV1) -> str:
    return "\n".join(element.text for page in part.pages for element in page.elements)


@dataclass
class _Run:
    slot: ParseSlot
    failures: list[str] = field(default_factory=list)

    async def inspect(self, data: bytes, content_type: str, label: str) -> InspectOk | None:
        directory = self.slot.scratch()
        await asyncio.to_thread(directory.write, SOURCE, data)
        outcome = await self.slot.inspect(directory, content_type, max_pages=MAX_PAGES)
        await self.slot.remove(directory)
        if not outcome.ok or not isinstance(outcome.result, InspectOk):
            self.failures.append(f"inspect:{label}")
            return None
        return outcome.result

    async def partition(self, pdf: bytes, page_count: int, label: str) -> ExtractPartV1 | None:
        directory = self.slot.scratch()
        await asyncio.to_thread(directory.write, SOURCE, pdf)
        outcome, _raw, part = await self.slot.partition(
            directory,
            pages=PageRange(first=1, last=min(PART_PAGES, page_count)),
            page_count=page_count,
            parameters={"summarizeTables": False, "ocrLanguages": ["eng"]},
            limit_seconds=PARSE_TIMEOUT_SECONDS,
            max_pages=MAX_PAGES,
        )
        await self.slot.remove(directory)
        if not outcome.ok or part is None:
            self.failures.append(f"partition:{label}")
        return part

    async def office(self, data: bytes, content_type: str, marker: str, label: str) -> None:
        if await self.inspect(data, content_type, label) is None:
            return
        directory = self.slot.scratch()
        await asyncio.to_thread(directory.write, SOURCE, data)
        outcome, converted = await self.slot.convert(
            directory,
            content_type,
            limit_seconds=convert.CONVERT_TIMEOUT_SECONDS,
            max_pages=MAX_PAGES,
        )
        await self.slot.remove(directory)
        if not outcome.ok or converted is None:
            self.failures.append(f"convert:{label}")
            return
        inspected = await self.inspect(converted, PDF, f"{label}.pdf")
        if inspected is None or not inspected.page_count:
            self.failures.append(f"converted_pages:{label}")
            return
        part = await self.partition(converted, inspected.page_count, f"{label}.pdf")
        if part is not None and marker not in _texts(part):
            self.failures.append(f"converted_marker:{label}")

    async def pdf(self, data: bytes, label: str) -> None:
        inspected = await self.inspect(data, PDF, label)
        if inspected is None or not inspected.page_count:
            return
        part = await self.partition(data, inspected.page_count, label)
        if part is None:
            return
        tables = [
            element
            for page in part.pages
            for element in page.elements
            if element.kind == "table" and element.table_html
        ]
        if not tables:
            self.failures.append(f"table_html:{label}")


async def run_documents(pool: ParsePool, fixtures: Path) -> list[str]:
    """Convert the Office fixtures and partition the fixture PDF and both converted PDFs."""
    async with pool.slot(PARSE_TIMEOUT_SECONDS) as slot:
        run = _Run(slot)
        for name, (content_type, marker) in OFFICE_FIXTURES.items():
            data = await asyncio.to_thread((fixtures / name).read_bytes)
            await run.office(data, content_type, marker, name)
        await run.pdf(await asyncio.to_thread((fixtures / PDF_FIXTURE).read_bytes), PDF_FIXTURE)
        return run.failures


async def run_selfcheck(
    fixtures: Path = FIXTURES,
    pool: ParsePool | None = None,
    *,
    checks: Sequence[Callable[[], bool]] = (check_encodings, check_odbc),
    converter: Callable[[], list[str]] = check_converter,
) -> list[str]:
    """Every check of section 15.5; returns the names of those that failed."""
    if pool is None:
        pool = _default_pool()
    failures = check_pool_layout(pool.layout)
    failures.extend(converter())
    for check in checks:
        try:
            passed = await asyncio.to_thread(check)
        except Exception:  # noqa: BLE001 - a check that raises fails, by its name only
            passed = False
        if not passed:
            failures.append(check.__name__)
    failures.extend(await failed_probes(pool))
    failures.extend(await run_documents(pool, fixtures))
    return failures


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    fixtures = Path(arguments[0]) if arguments else FIXTURES
    failures = asyncio.run(run_selfcheck(fixtures))
    for failure in failures:
        print(f"selfcheck failed: {failure}", file=sys.stderr)  # noqa: T201 - the build log
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
