"""The parsing child-process runner (`14-ingestion.md` sections 14.3, 14.7 and 15.6 to 15.8;
ADR 0049): the parse-user pool, scratch directories, starting and bounding a child, and the
parent's half of Render.

A `ParsePool` exists once per worker process: it holds the semaphore of parse slots and the UIDs
this process quarantined. An activity takes a slot, which holds two parse users, runs its children
through the slot, and leaves it; leaving ends every process of both users, removes their scratch
directories and files, and only then releases their locks. Every file a child wrote is read
through `results.read_child_file` and validated before anything uses it.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import math
import os
import shutil
import stat
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Final, Literal, Protocol

from opentelemetry.trace import Span
from pydantic import JsonValue

from docchat_core.telemetry import LOGGER_NAME, log_event, set_attributes, start_span
from docchat_core.telemetry.instruments import histogram
from docchat_ingestion.isolation import syscall_filter
from docchat_ingestion.isolation.convert import (
    OUTPUT_DIRECTORY,
    PROFILE,
    PROFILE_DIRECTORY,
    PROFILE_FILE,
    PROFILE_USER_DIRECTORY,
)
from docchat_ingestion.isolation.parse_worker import CONVERT_FILTER_ARGUMENT
from docchat_ingestion.isolation.results import (
    CONVERTED,
    DOCUMENT,
    DOCUMENT_VERIFIED,
    PARTITION,
    PARTITION_MAX_BYTES,
    PDF,
    RENDER_MAX_DOCUMENT_BYTES,
    RENDER_MAX_PAGE_BYTES,
    RENDER_MAX_PAGES_ON_DISK,
    REQUEST,
    RESULT,
    RESULT_MAX_BYTES,
    SOURCE,
    AssembleOk,
    ChildFileError,
    ChildFileTooLargeError,
    ChildRequest,
    ChildResult,
    Command,
    ConvertProbeOk,
    ExtractPartV1,
    Limits,
    PageRange,
    ProbeOk,
    memory_code,
    page_name,
    page_number,
    parse_partition,
    parse_result,
    process_failure_code,
    read_child_file,
    render_max_total_bytes,
    timeout_code,
    verified_name,
)

FIRST_PARSE_UID: Final = 10002
PARSE_USERS: Final = 16
USERS_PER_SLOT: Final = 2
FILE_SIZE_LIMIT: Final = 2_147_483_648
OPEN_FILES_LIMIT: Final = 256
PROCESSES_LIMIT: Final = 64
USER_RETRY_SECONDS: Final = 0.5
CLEAR_ROUNDS: Final = 10
CLEAR_ROUND_SECONDS: Final = 0.1
MEMORY_POLL_SECONDS: Final = 0.5
RENDER_POLL_SECONDS: Final = 0.1
INSPECT_TIMEOUT_SECONDS: Final = 120
RENDER_TIMEOUT_CAP_SECONDS: Final = 1200
VERIFY_PAGES_TIMEOUT_SECONDS: Final = 120
VERIFY_PAGES_MEMORY_MB: Final = 256
VERIFY_DOCUMENT_TIMEOUT_SECONDS: Final = 300
ASSEMBLE_TIMEOUT_SECONDS: Final = 300
PROBE_TIMEOUT_SECONDS: Final = 60
SCRATCH_PREFIX: Final = "docchat-"
WORKER_MODULE: Final = "docchat_ingestion.isolation.parse_worker"

CHILD_ENVIRONMENT: Final = (
    "PATH",
    "LANG",
    "HF_HOME",
    "HF_HUB_OFFLINE",
    "HF_HUB_DISABLE_TELEMETRY",
    "DO_NOT_TRACK",
    "SCARF_NO_ANALYTICS",
    "UNSTRUCTURED_DEFAULT_MODEL_INITIALIZE_PARAMS_JSON_PATH",
    "TIKTOKEN_CACHE_DIR",
)
"""Variables a child inherits from the worker's environment when set; `HOME`, `TMPDIR`, `TMP` and
`TEMP` name its scratch directory and `OMP_NUM_THREADS` is 1. No identity variable, endpoint or
connection setting is on the list, so code in a child can't obtain a token."""

SCRATCH_VARIABLES: Final = ("HOME", "TMPDIR", "TMP", "TEMP")

type ExitKind = Literal["ok", "timeout", "memory", "error"]

logger: Final = logging.getLogger(LOGGER_NAME)
PEAK_RSS: Final = histogram("docchat.ingestion.parse.peak_rss")


@dataclass(frozen=True)
class Layout:
    """The image's paths for the pool (`14-ingestion.md` section 15.2), replaceable in tests."""

    python: str = "/opt/venv/bin/python"
    scratch_parent: Path = Path("/run/docchat-scratch")
    lock_directory: Path = Path("/run/docchat-parse")
    proc: Path = Path("/proc")
    world_writable: tuple[Path, ...] = (
        Path("/tmp"),  # noqa: S108 - the world-writable directories the clearing empties
        Path("/var/tmp"),  # noqa: S108
        Path("/dev/shm"),  # noqa: S108
        Path("/dev/mqueue"),
    )
    first_uid: int = FIRST_PARSE_UID
    users: int = PARSE_USERS

    @property
    def uids(self) -> range:
        return range(self.first_uid, self.first_uid + self.users)

    def lock_path(self, uid: int) -> Path:
        return self.lock_directory / f"{uid}.lock"


class ParseUsersUnavailableError(Exception):
    """Fewer than two parse users came free before the first command's wall clock expired; the
    item fails with `parse_timeout`."""

    code: Final = "parse_timeout"


@dataclass(frozen=True)
class ChildOutcome:
    """How one child ended. `code` is the item code of a failure, `None` on success; `result` is
    the validated result; `seconds` is the wall clock rounded up, the parse budget's unit."""

    command: Command
    exit: ExitKind
    code: str | None
    result: ChildResult | None
    seconds: int
    peak_rss_mb: int

    @property
    def ok(self) -> bool:
        return self.code is None


def _status_uids(text: str) -> tuple[int, ...]:
    for line in text.splitlines():
        if line.startswith("Uid:"):
            return tuple(int(part) for part in line.split()[1:4])
    return ()


def _status_rss_kb(text: str) -> int:
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    return 0


def _statuses(proc: Path) -> list[str]:
    statuses: list[str] = []
    with os.scandir(proc) as entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            with suppress(OSError):
                statuses.append((proc / entry.name / "status").read_text())
    return statuses


def _exited(text: str) -> bool:
    # A zombie or dead entry runs no code and holds no memory; it waits only to be reaped.
    for line in text.splitlines():
        if line.startswith("State:"):
            return line.split()[1:2] in (["Z"], ["X"])
    return False


def processes_of(proc: Path, uid: int) -> int:
    """Processes whose real, effective or saved UID is `uid`, zombies left out."""
    return sum(1 for text in _statuses(proc) if uid in _status_uids(text) and not _exited(text))


def resident_kb(proc: Path, uid: int) -> int:
    """`VmRSS` summed over every process whose real UID is `uid`: the child, the processes it
    starts, and any that left its session."""
    total = 0
    for text in _statuses(proc):
        uids = _status_uids(text)
        if uids and uids[0] == uid:
            total += _status_rss_kb(text)
    return total


def remove_files_of(roots: tuple[Path, ...], uid: int) -> None:
    """Remove every file and directory `uid` owns under each root, following no link; a root the
    container runtime didn't mount is skipped."""

    def walk(directory: Path) -> None:
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return
        for entry in entries:
            path = Path(entry.path)
            try:
                status = os.lstat(path)
            except OSError:
                continue
            if status.st_uid == uid:
                if stat.S_ISDIR(status.st_mode):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    with suppress(OSError):
                        path.unlink()
            elif stat.S_ISDIR(status.st_mode):
                walk(path)

    for root in roots:
        if root.is_dir() and not root.is_symlink():
            walk(root)


class Scratch:
    """A scratch directory (`14-ingestion.md` section 15.8): created owned by root with mode
    0700, filled by the parent with root-owned 0644 files, handed to one parse user once, read
    through its descriptor, and removed by the slot."""

    def __init__(self, layout: Layout) -> None:
        self.path = Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX, dir=layout.scratch_parent))
        self.fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)
        self.owner: int | None = None

    def open_new(self, name: str) -> IO[bytes]:
        """A new root-owned file of mode 0644, created relative to the directory's descriptor;
        an existing name or a link fails."""
        if self.owner is not None:
            raise PermissionError("the scratch directory was handed over")
        fd = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=self.fd
        )
        os.fchmod(fd, 0o644)
        return os.fdopen(fd, "wb")

    def write(self, name: str, data: bytes) -> None:
        with self.open_new(name) as file:
            file.write(data)

    def open_own(self, name: str) -> IO[bytes]:
        """A file the parent wrote, opened for reading while no child has had the directory, so
        its bytes are the parent's; once a child had it, its files are read only through
        `read`."""
        if self.owner is not None:
            raise PermissionError("the scratch directory was handed over")
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.fd)
        return os.fdopen(fd, "rb")

    def make_directory(self, name: str) -> None:
        os.mkdir(name, 0o700, dir_fd=self.fd)

    def hand_over(self, uid: int, subdirectories: tuple[str, ...] = ()) -> None:
        """Give the directory (and the named subdirectories) to `uid` with mode 0700."""
        for name in subdirectories:
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd)
            try:
                os.fchown(fd, uid, uid)
                os.fchmod(fd, 0o700)
            finally:
                os.close(fd)
        os.fchown(self.fd, uid, uid)
        os.fchmod(self.fd, 0o700)
        self.owner = uid

    def read(self, name: str, *, limit: int) -> bytes:
        if self.owner is None:
            raise ChildFileError
        return read_child_file(self.fd, name, uid=self.owner, limit=limit)

    def names(self) -> list[str]:
        """The directory's entry names only; no entry is examined, so no link is followed."""
        return os.listdir(self.fd)  # noqa: PTH208 - listed through its descriptor

    def delete(self, name: str) -> None:
        os.unlink(name, dir_fd=self.fd)

    def remove(self) -> None:
        with suppress(OSError):
            os.close(self.fd)
        shutil.rmtree(self.path, ignore_errors=True)


def child_environment(scratch: Path, source: Mapping[str, str]) -> dict[str, str]:
    environment = {name: source[name] for name in CHILD_ENVIRONMENT if name in source}
    environment.update(dict.fromkeys(SCRATCH_VARIABLES, str(scratch)))
    environment["OMP_NUM_THREADS"] = "1"
    return environment


def filter_for(command: Command) -> syscall_filter.FilterName:
    """The filter a child of `command` loads: the `convert` filter, which allows `AF_UNIX`
    sockets, for `convert` (section 15.11 item 7, outcome 3 of SP-15), the full filter of section
    15.6 item 12.2 for every other command."""
    return syscall_filter.CONVERT if command is Command.CONVERT else syscall_filter.DEFAULT


def child_command(
    layout: Layout,
    uid: int,
    request_path: Path,
    filter_name: syscall_filter.FilterName = syscall_filter.DEFAULT,
) -> list[str]:
    """`setpriv` to the parse user with no groups, `no_new_privs` and no inheritable
    capabilities, then `prlimit` with the file, open-file and process limits, then the worker;
    a child that loads the `convert` filter gets `--filter=convert` after its request path,
    because it chooses its filter before it reads the request."""
    command = [
        "setpriv",
        f"--reuid={uid}",
        f"--regid={uid}",
        "--clear-groups",
        "--no-new-privs",
        "--inh-caps=-all",
        "--",
        "prlimit",
        f"--fsize={FILE_SIZE_LIMIT}",
        f"--nofile={OPEN_FILES_LIMIT}",
        f"--nproc={PROCESSES_LIMIT}",
        "--",
        layout.python,
        "-I",
        "-m",
        WORKER_MODULE,
        str(request_path),
    ]
    if filter_name == syscall_filter.CONVERT:
        command.append(CONVERT_FILTER_ARGUMENT)
    return command


class RenditionSink(Protocol):
    """Where Render puts what `verify-render` passed: the activity's Blob uploads."""

    async def upload_page(self, number: int, data: bytes) -> None: ...

    async def upload_document(self, data: bytes) -> None: ...


@dataclass(frozen=True)
class RenditionOutcome:
    """Render's result: `code` is `None`, `render_failed`, `render_too_large`, `parse_timeout` or
    `parse_memory_exceeded`; `document_pdf` is `built` or `too_large` on success."""

    code: str | None
    page_count: int | None = None
    document_pdf: Literal["built", "too_large"] | None = None
    uploaded_pages: tuple[int, ...] = ()


class _RenditionError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class ParseSlot:
    """Two parse users of the pool, held by one activity."""

    pool: ParsePool
    first_uid: int
    second_uid: int
    waited: float
    scratches: list[Scratch] = field(default_factory=list)
    _ran: set[int] = field(default_factory=set)

    def scratch(self) -> Scratch:
        directory = Scratch(self.pool.layout)
        self.scratches.append(directory)
        return directory

    async def remove(self, directory: Scratch) -> None:
        """End every process of the directory's user, then remove the directory."""
        if directory.owner is not None:
            await self.pool.end_processes(directory.owner)
        await asyncio.to_thread(directory.remove)
        with suppress(ValueError):
            self.scratches.remove(directory)

    def _uid(self, user: Literal["first", "second"]) -> int:
        return self.first_uid if user == "first" else self.second_uid

    async def run(
        self,
        directory: Scratch,
        request: ChildRequest,
        *,
        limit_seconds: float,
        memory_limit_mb: int,
        user: Literal["first", "second"] = "first",
        page_count: int | None = None,
        office_source: bool = False,
        subdirectories: tuple[str, ...] = (),
        filter_name: syscall_filter.FilterName | None = None,
    ) -> ChildOutcome:
        """Write `request.json`, hand the directory to the user, run the child to its end and
        validate its result. The first command of the slot counts the wait for free users. The
        child loads `filter_name`, by default its command's filter (`filter_for`); only the
        probe names one."""
        uid = self._uid(user)
        chosen = filter_for(request.command) if filter_name is None else filter_name
        limit = await self.begin(uid, limit_seconds)
        directory.write(REQUEST, request.encode())
        directory.hand_over(uid, subdirectories)
        with self.pool.span(request) as span:
            child = await self.pool.start(uid, directory, chosen)
            exit_kind, seconds, peak = await self.pool.wait(child, uid, limit, memory_limit_mb)
            return self.outcome(
                request,
                directory,
                exit_kind=exit_kind,
                returncode=child.returncode,
                seconds=seconds,
                peak=peak,
                page_count=page_count,
                office_source=office_source,
                filter_name=chosen,
                span=span,
            )

    async def begin(self, uid: int, limit_seconds: float) -> float:
        """Prepare `uid` for its next child: between two children under one user only the
        processes are ended; the slot's first command counts the wait for free users."""
        if uid in self._ran:
            await self.pool.end_processes(uid)
        elif not self._ran:
            limit_seconds -= self.waited
        self._ran.add(uid)
        return limit_seconds

    def outcome(
        self,
        request: ChildRequest,
        directory: Scratch,
        *,
        exit_kind: ExitKind,
        returncode: int | None,
        seconds: int,
        peak: int,
        page_count: int | None,
        office_source: bool,
        span: Span | None,
        filter_name: syscall_filter.FilterName = syscall_filter.DEFAULT,
    ) -> ChildOutcome:
        command = request.command
        result: ChildResult | None = None
        if exit_kind == "timeout":
            code: str | None = timeout_code(command)
        elif exit_kind == "memory":
            code = memory_code(command)
        elif returncode != 0:
            code = process_failure_code(command)
            exit_kind = "error"
        else:
            try:
                raw = directory.read(RESULT, limit=RESULT_MAX_BYTES)
                result = parse_result(
                    command,
                    raw,
                    page_count=page_count,
                    office_source=office_source,
                    filter_name=filter_name,
                )
            except ChildFileError:
                result = None
            if result is None:
                code = process_failure_code(command)
                exit_kind = "error"
            else:
                code = None if result.ok else str(getattr(result, "code", ""))
        if span is not None:
            self.pool.record(span, request, exit_kind, peak)
        return ChildOutcome(
            command, exit_kind, code, result if code is None else None, seconds, peak
        )

    async def inspect(
        self, directory: Scratch, content_type: str, *, max_pages: int
    ) -> ChildOutcome:
        """The `inspect` child on the directory's `source.bin` (120 seconds)."""
        request = _request(Command.INSPECT, content_type, SOURCE, max_pages=max_pages)
        return await self.run(
            directory,
            request,
            limit_seconds=INSPECT_TIMEOUT_SECONDS,
            memory_limit_mb=self.pool.memory_limit_mb,
            office_source=content_type != PDF,
        )

    async def convert(
        self, directory: Scratch, content_type: str, *, limit_seconds: float, max_pages: int
    ) -> tuple[ChildOutcome, bytes | None]:
        """The `convert` child (section 15.11) on the directory's `source.bin`, with the
        parent-written profile and the `convert` filter (`filter_for`); returns the
        `converted.pdf` bytes the parent read and checked."""
        directory.make_directory(PROFILE_DIRECTORY)
        directory.make_directory(PROFILE_USER_DIRECTORY)
        directory.make_directory(OUTPUT_DIRECTORY)
        directory.write(PROFILE_FILE, PROFILE)
        request = _request(Command.CONVERT, content_type, SOURCE, max_pages=max_pages)
        outcome = await self.run(
            directory,
            request,
            limit_seconds=limit_seconds,
            memory_limit_mb=self.pool.memory_limit_mb,
            subdirectories=(PROFILE_DIRECTORY, PROFILE_USER_DIRECTORY, OUTPUT_DIRECTORY),
        )
        if not outcome.ok:
            return outcome, None
        try:
            data = await asyncio.to_thread(
                directory.read, CONVERTED, limit=request.limits.convert_max_bytes
            )
        except ChildFileError:
            return _failed(outcome, "convert_failed"), None
        return outcome, data

    async def partition(
        self,
        directory: Scratch,
        *,
        pages: PageRange,
        page_count: int,
        parameters: Mapping[str, JsonValue],
        limit_seconds: float,
        max_pages: int,
    ) -> tuple[ChildOutcome, bytes | None, ExtractPartV1 | None]:
        """The page-wise `partition` child of one part; returns the validated `partition.json`
        bytes, which the parent uploads without reading the file again."""
        request = _request(
            Command.PARTITION,
            PDF,
            SOURCE,
            max_pages=max_pages,
            pages=pages,
            parameters={**parameters, "pageCount": page_count},
        )
        outcome = await self.run(
            directory,
            request,
            limit_seconds=limit_seconds,
            memory_limit_mb=self.pool.memory_limit_mb,
        )
        if not outcome.ok:
            return outcome, None, None
        try:
            raw = await asyncio.to_thread(directory.read, PARTITION, limit=PARTITION_MAX_BYTES)
            part = parse_partition(raw, page_count=page_count, first=pages.first, last=pages.last)
        except ChildFileError:
            return _failed(outcome, "parse_failed"), None, None
        return outcome, raw, part

    async def render(
        self,
        directory: Scratch,
        *,
        page_count: int,
        pdf_size: int,
        max_pages: int,
        sink: RenditionSink,
    ) -> RenditionOutcome:
        """Render's pages and whole-document rendition (sections 14.3, 14.4 and 14.7) from the
        version's PDF in the directory's `source.bin`."""
        job = _Render(self, directory, self.scratch(), page_count, pdf_size, max_pages, sink)
        try:
            await job.pages()
            if not job.assembling:
                return job.outcome("too_large")
            document = await job.assemble()
            if document is None:
                return job.outcome("too_large")
            await sink.upload_document(document)
            return job.outcome("built")
        except _RenditionError as failure:
            return RenditionOutcome(failure.code, uploaded_pages=tuple(job.uploaded))
        finally:
            await self.remove(job.assembly)


@dataclass
class _Render:
    """One Render's state: the uploaded pages and the running total of verified bytes, which
    decides whether the assembly directory still holds every page."""

    slot: ParseSlot
    directory: Scratch
    assembly: Scratch
    page_count: int
    pdf_size: int
    max_pages: int
    sink: RenditionSink
    uploaded: list[int] = field(default_factory=list)
    total: int = 0
    assembling: bool = True

    def outcome(self, document_pdf: Literal["built", "too_large"]) -> RenditionOutcome:
        return RenditionOutcome(None, self.page_count, document_pdf, tuple(self.uploaded))

    def _request(
        self,
        command: Command,
        *,
        pages: PageRange | None = None,
        mode: Literal["pages", "document"] | None = None,
        page_count: bool = False,
    ) -> ChildRequest:
        return _request(
            command,
            PDF,
            SOURCE if command is Command.RENDER else None,
            max_pages=self.max_pages,
            pdf_size=self.pdf_size,
            pages=pages,
            mode=mode,
            parameters={"pageCount": self.page_count} if page_count else None,
        )

    async def pages(self) -> None:
        """Run `render` and hand its finished pages, in batches of at most 4, to `verify-render`
        and the sink."""
        slot = self.slot
        pool = slot.pool
        request = self._request(Command.RENDER)
        uid = slot.first_uid
        limit = await slot.begin(uid, min(pool.parse_timeout_seconds, RENDER_TIMEOUT_CAP_SECONDS))
        self.directory.write(REQUEST, request.encode())
        self.directory.hand_over(uid)
        with pool.span(request) as span:
            child = await pool.start(uid, self.directory)
            waiter = asyncio.create_task(pool.wait(child, uid, limit, pool.memory_limit_mb))
            exit_kind, seconds, peak = await self._batches(waiter)
            outcome = slot.outcome(
                request,
                self.directory,
                exit_kind=exit_kind,
                returncode=child.returncode,
                seconds=seconds,
                peak=peak,
                page_count=self.page_count,
                office_source=False,
                span=span,
            )
        if not outcome.ok:
            raise _RenditionError(outcome.code or "render_failed")
        if sorted(self.uploaded) != list(range(1, self.page_count + 1)):
            raise _RenditionError("render_failed")

    async def _batches(
        self, waiter: asyncio.Task[tuple[ExitKind, int, int]]
    ) -> tuple[ExitKind, int, int]:
        """List the render directory by name every 100 ms and handle the finished pages."""
        try:
            while True:
                exited = waiter.done()
                names = await asyncio.to_thread(self.directory.names)
                batch = sorted(
                    number
                    for number in (page_number(name, self.page_count) for name in names)
                    if number is not None
                )[:RENDER_MAX_PAGES_ON_DISK]
                if batch:
                    await self._batch(batch)
                    continue
                if exited:
                    return waiter.result()
                await asyncio.sleep(RENDER_POLL_SECONDS)
        finally:
            if not waiter.done():
                waiter.cancel()
                with suppress(asyncio.CancelledError):
                    await waiter

    async def _batch(self, batch: list[int]) -> None:
        pages = await asyncio.to_thread(self._take, batch)
        verified = await self._verify(pages)
        del pages
        await asyncio.gather(*(self.sink.upload_page(number, data) for number, data in verified))
        self.uploaded.extend(number for number, _ in verified)
        for number, data in verified:
            self.total += len(data)
            if self.assembling and self.total <= RENDER_MAX_DOCUMENT_BYTES:
                await asyncio.to_thread(self.assembly.write, page_name(number), data)
            elif self.assembling:
                self.assembling = False
                await asyncio.to_thread(_empty, self.assembly)

    def _take(self, batch: list[int]) -> list[tuple[int, bytes]]:
        """Read each finished page as item 5 requires and delete it, so the child can't change it
        afterwards."""
        pages: list[tuple[int, bytes]] = []
        for number in batch:
            pages.append((number, _read_page(self.directory, page_name(number))))
            self.directory.delete(page_name(number))
        return pages

    async def _verify(self, pages: list[tuple[int, bytes]]) -> list[tuple[int, bytes]]:
        verifier = self.slot.scratch()
        try:
            for number, data in pages:
                await asyncio.to_thread(verifier.write, page_name(number), data)
            request = self._request(
                Command.VERIFY_RENDER,
                pages=PageRange(first=pages[0][0], last=pages[-1][0]),
                mode="pages",
            )
            outcome = await self.slot.run(
                verifier,
                request,
                limit_seconds=VERIFY_PAGES_TIMEOUT_SECONDS,
                memory_limit_mb=VERIFY_PAGES_MEMORY_MB,
                user="second",
            )
            if not outcome.ok:
                raise _RenditionError("render_failed")
            return [
                (number, await asyncio.to_thread(_read_page, verifier, verified_name(number)))
                for number, _ in pages
            ]
        finally:
            await self.slot.remove(verifier)

    async def assemble(self) -> bytes | None:
        """`assemble` under the first user, then `verify-render` in document mode under the
        second; `None` when the whole-document rendition is too large."""
        pool = self.slot.pool
        outcome = await self.slot.run(
            self.assembly,
            self._request(Command.ASSEMBLE, page_count=True),
            limit_seconds=ASSEMBLE_TIMEOUT_SECONDS,
            memory_limit_mb=pool.memory_limit_mb,
            page_count=self.page_count,
        )
        if not outcome.ok or not isinstance(outcome.result, AssembleOk):
            raise _RenditionError("render_failed")
        if outcome.result.document_pdf == "too_large":
            return None
        document = await asyncio.to_thread(_read_document, self.assembly, DOCUMENT)
        if document is None:
            return None
        verifier = self.slot.scratch()
        try:
            await asyncio.to_thread(verifier.write, DOCUMENT, document)
            del document
            outcome = await self.slot.run(
                verifier,
                self._request(Command.VERIFY_RENDER, mode="document", page_count=True),
                limit_seconds=VERIFY_DOCUMENT_TIMEOUT_SECONDS,
                memory_limit_mb=pool.memory_limit_mb,
                user="second",
            )
            if not outcome.ok:
                raise _RenditionError("render_failed")
            return await asyncio.to_thread(_read_document, verifier, DOCUMENT_VERIFIED)
        finally:
            await self.slot.remove(verifier)


def _read_page(directory: Scratch, name: str) -> bytes:
    """A page or verified page file: above `renderMaxPageBytes` is `render_too_large`, any other
    refusal `render_failed`."""
    try:
        return directory.read(name, limit=RENDER_MAX_PAGE_BYTES)
    except ChildFileTooLargeError:
        raise _RenditionError("render_too_large") from None
    except ChildFileError:
        raise _RenditionError("render_failed") from None


def _read_document(directory: Scratch, name: str) -> bytes | None:
    """`document.pdf` or `document.verified.pdf`: above `renderMaxDocumentBytes` skips the
    whole-document rendition (`None`), any other refusal is `render_failed`."""
    try:
        return directory.read(name, limit=RENDER_MAX_DOCUMENT_BYTES)
    except ChildFileTooLargeError:
        return None
    except ChildFileError:
        raise _RenditionError("render_failed") from None


def _empty(directory: Scratch) -> None:
    for name in directory.names():
        with suppress(OSError):
            directory.delete(name)


def _failed(outcome: ChildOutcome, code: str) -> ChildOutcome:
    return ChildOutcome(outcome.command, "error", code, None, outcome.seconds, outcome.peak_rss_mb)


def _request(
    command: Command,
    content_type: str,
    source: str | None,
    *,
    max_pages: int,
    pdf_size: int = 0,
    pages: PageRange | None = None,
    mode: Literal["pages", "document"] | None = None,
    parameters: Mapping[str, JsonValue] | None = None,
) -> ChildRequest:
    return ChildRequest(
        command=command,
        content_type=content_type,
        source_path=source,
        pages=pages,
        mode=mode,
        parameters=dict(parameters or {}),
        limits=Limits(max_pages=max_pages, render_max_total_bytes=render_max_total_bytes(pdf_size)),
        fail_on_network=True,
    )


@dataclass(frozen=True)
class _Child:
    process: asyncio.subprocess.Process
    started: float

    @property
    def returncode(self) -> int | None:
        return self.process.returncode


class ParsePool:
    """The worker process's parse slots and parse-user pool (`14-ingestion.md` section 15.6
    items 8 and 11)."""

    def __init__(
        self,
        *,
        slots_per_process: int,
        memory_limit_mb: int,
        parse_timeout_seconds: int,
        layout: Layout | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.layout = layout or Layout()
        self.memory_limit_mb = memory_limit_mb
        self.parse_timeout_seconds = parse_timeout_seconds
        self.clock = clock
        self.sleep = sleep
        self.environment = dict(os.environ if environment is None else environment)
        self.quarantined: set[int] = set()
        self._slots = asyncio.Semaphore(slots_per_process)
        self._locks: dict[int, int] = {}

    @asynccontextmanager
    async def slot(self, first_timeout: float) -> AsyncIterator[ParseSlot]:
        """Take a parse slot and two parse users, waiting at most `first_timeout`, the wall clock
        of the activity's first command; leave it with both users cleared and released."""
        started = self.clock()
        async with self._slots:
            first, second = await self._take_users(started + first_timeout)
            held = ParseSlot(self, first, second, waited=self.clock() - started)
            try:
                yield held
            finally:
                for directory in list(held.scratches):
                    await held.remove(directory)
                await self._release(first)
                await self._release(second)

    async def _take_users(self, deadline: float) -> tuple[int, int]:
        while True:
            taken: list[int] = []
            for uid in self.layout.uids:
                if uid in self.quarantined or uid in self._locks:
                    continue
                if not self._try_lock(uid):
                    continue
                if await self.clear(uid, remove_files=True):
                    taken.append(uid)
                    if len(taken) == USERS_PER_SLOT:
                        return taken[0], taken[1]
            for uid in taken:
                self._unlock(uid)
            if self.clock() >= deadline:
                raise ParseUsersUnavailableError
            await self.sleep(USER_RETRY_SECONDS)

    def _try_lock(self, uid: int) -> bool:
        try:
            fd = os.open(self.layout.lock_path(uid), os.O_RDWR | os.O_NOFOLLOW)
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._locks[uid] = fd
        return True

    def _unlock(self, uid: int) -> None:
        fd = self._locks.pop(uid, None)
        if fd is not None:
            os.close(fd)

    async def _release(self, uid: int) -> None:
        if await self.clear(uid, remove_files=True):
            self._unlock(uid)

    async def end_processes(self, uid: int) -> bool:
        """End every process of `uid`: `kill -KILL -1` as that user, then a scan of `/proc`,
        at most 10 rounds 100 ms apart; `False` when a process remains."""
        for _round in range(CLEAR_ROUNDS):
            killer = await asyncio.create_subprocess_exec(
                "setpriv",
                f"--reuid={uid}",
                f"--regid={uid}",
                "--clear-groups",
                "--",
                "kill",
                "-KILL",
                "-1",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            # The scan decides, so kill's exit status is ignored.
            await killer.wait()
            if not await asyncio.to_thread(processes_of, self.layout.proc, uid):
                return True
            await self.sleep(CLEAR_ROUND_SECONDS)
        return False

    async def clear(self, uid: int, *, remove_files: bool) -> bool:
        """Clear a user (item 11.4); a user with a process left after the tenth round is
        quarantined for this worker process and keeps its lock."""
        if not await self.end_processes(uid):
            self.quarantined.add(uid)
            log_event(
                logger,
                "parse_uid_quarantined",
                logging.WARNING,
                **{"docchat.ingestion.parse_uid": uid},
            )
            return False
        if remove_files:
            roots = (*self.layout.world_writable, self.layout.scratch_parent)
            await asyncio.to_thread(remove_files_of, roots, uid)
        return True

    async def start(
        self,
        uid: int,
        directory: Scratch,
        filter_name: syscall_filter.FilterName = syscall_filter.DEFAULT,
    ) -> _Child:
        process = await asyncio.create_subprocess_exec(
            *child_command(self.layout, uid, directory.path / REQUEST, filter_name),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=directory.path,
            env=child_environment(directory.path, self.environment),
            start_new_session=True,
            close_fds=True,
        )
        return _Child(process, self.clock())

    async def wait(
        self, child: _Child, uid: int, limit_seconds: float, memory_limit_mb: int
    ) -> tuple[ExitKind, int, int]:
        """Wait for the child within its wall clock while summing its UID's resident memory every
        500 ms; on expiry, on the memory limit and on cancellation every process of the UID is
        ended. Returns the exit kind, the wall-clock seconds rounded up and the peak in MB."""
        peak_kb = 0
        over = asyncio.Event()

        async def watch() -> None:
            nonlocal peak_kb
            while True:
                used = await asyncio.to_thread(resident_kb, self.layout.proc, uid)
                peak_kb = max(peak_kb, used)
                if used > memory_limit_mb * 1024:
                    over.set()
                    return
                await asyncio.sleep(MEMORY_POLL_SECONDS)

        watcher = asyncio.create_task(watch())
        exited = asyncio.create_task(child.process.wait())
        exceeded = asyncio.create_task(over.wait())
        kind: ExitKind = "ok"
        try:
            done, _ = await asyncio.wait(
                {exited, exceeded},
                timeout=max(limit_seconds, 0),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if exited in done:
                kind = "ok" if child.process.returncode == 0 else "error"
            elif exceeded in done:
                kind = "memory"
            else:
                kind = "timeout"
        finally:
            for task in (watcher, exceeded):
                task.cancel()
            if not exited.done():
                await asyncio.shield(self.end_processes(uid))
                with suppress(asyncio.CancelledError):
                    await exited
        seconds = max(1, math.ceil(self.clock() - child.started))
        return kind, seconds, math.ceil(peak_kb / 1024)

    @contextmanager
    def span(self, request: ChildRequest) -> Iterator[Span | None]:
        """Span `docchat.ingestion.parse` around one child, metadata only; the probe, whose
        command isn't a value of `docchat.ingestion.parse_command`, records none."""
        if request.command is Command.PROBE:
            yield None
            return
        attributes: dict[str, object] = {
            "docchat.ingestion.parse_command": request.command.value,
            "docchat.ingestion.content_type": request.content_type,
        }
        if request.pages is not None:
            attributes["docchat.ingestion.pages"] = request.pages.last - request.pages.first + 1
        with start_span("docchat.ingestion.parse", **attributes) as span:
            yield span

    def record(self, span: Span, request: ChildRequest, exit_kind: ExitKind, peak_mb: int) -> None:
        set_attributes(
            span,
            **{
                "docchat.ingestion.parse_peak_rss_mb": peak_mb,
                "docchat.ingestion.parse_exit": exit_kind,
            },
        )
        PEAK_RSS.record(peak_mb, parse_command=request.command.value)

    async def probe(self, filter_name: syscall_filter.FilterName = syscall_filter.DEFAULT) -> bool:
        """One `probe` child under a parse user (section 15.5), passing only on exit code 0 with
        a valid result that reports every check true: the default probe, or with the `convert`
        filter the `convert`-mode probe (section 15.11 item 7.3)."""
        expected = ConvertProbeOk if filter_name == syscall_filter.CONVERT else ProbeOk
        async with self.slot(PROBE_TIMEOUT_SECONDS) as held:
            directory = held.scratch()
            request = _request(Command.PROBE, PDF, None, max_pages=1)
            outcome = await held.run(
                directory,
                request,
                limit_seconds=PROBE_TIMEOUT_SECONDS,
                memory_limit_mb=self.memory_limit_mb,
                filter_name=filter_name,
            )
            return outcome.ok and isinstance(outcome.result, expected)
