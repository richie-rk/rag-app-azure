"""The parsing child's entry point, `python -I -m docchat_ingestion.isolation.parse_worker
<request path>` (`14-ingestion.md` section 15.6; ADR 0049).

Every child closes the network and cross-process access for itself before it opens a document:
until the filter is loaded, this module has imported only the standard library and
`syscall_filter`. In order it sets `umask 077`, clears its dumpable flag, checks that it runs one
thread (a thread started before the load would run unfiltered) and loads the filter; any failure
exits with code 4 before the request is read. Only then does it read `request.json`, install the
network audit hook its request asks for, import the command's modules and run the command.

The filter is chosen before the request is read, so it comes from the command line: the request
path alone loads the filter of section 15.6 item 12.2, and the request path followed by
`--filter=convert` loads the `convert` child's filter, under which `socket()` and `socketpair()`
are refused for every address family but `AF_UNIX` (section 15.11 item 7, outcome 3 of SP-15).
Any other command line exits with code 4 before anything is loaded. Once the request is read, a
command that its filter wasn't built for exits with code 4 and writes no result: `convert` runs
only under the `convert` filter, every other command only under the default one, and `probe`
under either, as the default probe or the `convert`-mode probe (section 15.5).

The child exchanges data with its parent only through files in its scratch directory, its working
directory: `result.json` and the command's output files. Exit code 0 with a valid result is
success; an unexpected exception ends the child with a non-zero exit and no result.
"""

from __future__ import annotations

import ctypes
import errno
import importlib
import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

from docchat_ingestion.isolation import syscall_filter

if TYPE_CHECKING:
    from docchat_ingestion.isolation.results import (
        ChildRequest,
        ChildResult,
        Command,
        ExtractPartV1,
    )

EXIT_NETWORK: Final = 3
EXIT_FILTER: Final = 4
PR_SET_DUMPABLE: Final = 4
NETWORK_EVENTS: Final = frozenset({"socket.getaddrinfo", "socket.connect"})
TASKS: Final = "/proc/self/task"
PAGEWISE_PARTITION: Final = "docchat_ingestion.methods.pagewise.partition"
"""The page-wise method's module that partitions one part inside this child (`14-ingestion.md`
section 10.2); it exposes `partition_part` and `CorruptPartError`."""

CONVERT_FILTER_ARGUMENT: Final = "--filter=" + syscall_filter.CONVERT
"""Follows the request path on the command line of a `convert` child and of the `convert`-mode
probe; without it the child loads the default filter."""

# Values of the kernel's headers for the isolation probe's calls.
AF_UNIX: Final = 1
AF_INET: Final = 2
AF_INET6: Final = 10
AF_NETLINK: Final = 16
SOCK_STREAM: Final = 1
SOCK_RAW: Final = 3
NETLINK_ROUTE: Final = 0
PTRACE_TRACEME: Final = 0
IO_URING_PARAMS_SIZE: Final = 120


def filter_named(argv: list[str]) -> syscall_filter.FilterName | None:
    """The filter the command line names: the request path alone names the default filter, the
    request path and `--filter=convert` the `convert` filter; any other command line, `None`."""
    if len(argv) == 2:  # noqa: PLR2004 - the program and the request path
        return syscall_filter.DEFAULT
    if len(argv) == 3 and argv[2] == CONVERT_FILTER_ARGUMENT:  # noqa: PLR2004 - and the filter
        return syscall_filter.CONVERT
    return None


def harden(filter_name: syscall_filter.FilterName = syscall_filter.DEFAULT) -> bool:
    """`umask 077`, no dumpable flag, one thread, then the named filter; `False` when any step
    fails."""
    os.umask(0o077)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        return False
    if sum(1 for _ in Path(TASKS).iterdir()) != 1:
        return False
    try:
        if filter_name == syscall_filter.CONVERT:
            syscall_filter.install_convert()
        else:
            syscall_filter.install()
    except syscall_filter.FilterError:
        return False
    return True


def fits_filter(command: Command, filter_name: syscall_filter.FilterName) -> bool:
    """Whether `command` runs under the filter this child loaded: `convert` only under the
    `convert` filter, every other command but `probe` only under the default filter, and `probe`
    under either."""
    from docchat_ingestion.isolation.results import Command  # noqa: PLC0415 - after the filter

    if command is Command.PROBE:
        return True
    return (command is Command.CONVERT) == (filter_name == syscall_filter.CONVERT)


def network_hook(event: str, _args: tuple[object, ...]) -> None:
    """The `failOnNetwork` audit hook: a second layer that ends a child whose Python code
    resolves a name or connects a socket. Native code raises no audit event, so the filter, not
    this hook, is the boundary."""
    if event in NETWORK_EVENTS:
        os._exit(EXIT_NETWORK)


class _Libc:
    """Raw system calls for the isolation probe, by the numbers libseccomp resolves, so a refusal
    is the filter's and not a library wrapper's."""

    def __init__(self) -> None:
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.syscall.restype = ctypes.c_long
        self.seccomp = syscall_filter.open_library()

    def refused(self, name: str, *args: object, code: int = errno.EPERM) -> bool:
        number = syscall_filter.resolve(self.seccomp, name)
        ctypes.set_errno(0)
        result = self.libc.syscall(number, *args)
        return bool(result == -1 and ctypes.get_errno() == code)


def probe_checks(libc: _Libc) -> dict[str, bool]:
    """The isolation probe's checks of the filter's own effect (`14-ingestion.md` section 15.5):
    `socket()` and `socketpair()` refused for every family, `AF_UNIX` included, and the checks of
    the other rules."""
    fds = (ctypes.c_int * 2)()
    return {
        "inetSocketRefused": libc.refused("socket", AF_INET, SOCK_STREAM, 0),
        "unixSocketRefused": libc.refused("socket", AF_UNIX, SOCK_STREAM, 0),
        "socketpairRefused": libc.refused("socketpair", AF_UNIX, SOCK_STREAM, 0, fds),
        **_other_rule_checks(libc),
    }


def convert_probe_checks(libc: _Libc) -> dict[str, bool]:
    """The `convert`-mode probe's checks (section 15.11 item 7.3): under the `convert` filter,
    `socket()` for `AF_INET`, `AF_INET6` and `AF_NETLINK` is still refused, and so is every call
    the checks of the other rules make."""
    return {
        "inetSocketRefused": libc.refused("socket", AF_INET, SOCK_STREAM, 0),
        "inet6SocketRefused": libc.refused("socket", AF_INET6, SOCK_STREAM, 0),
        "netlinkSocketRefused": libc.refused("socket", AF_NETLINK, SOCK_RAW, NETLINK_ROUTE),
        **_other_rule_checks(libc),
    }


def _other_rule_checks(libc: _Libc) -> dict[str, bool]:
    """The checks both probes share: `io_uring_setup`, `PTRACE_TRACEME` and a self
    `process_vm_readv` refused with `EPERM`, `clone3` with `ENOSYS`, and an `AF_INET` socket
    refused in a thread started after the load."""
    params = ctypes.create_string_buffer(IO_URING_PARAMS_SIZE)
    own = ctypes.create_string_buffer(8)
    copy = ctypes.create_string_buffer(8)

    class _Iovec(ctypes.Structure):
        _fields_ = (("base", ctypes.c_void_p), ("length", ctypes.c_size_t))

    local = _Iovec(ctypes.cast(copy, ctypes.c_void_p), 8)
    remote = _Iovec(ctypes.cast(own, ctypes.c_void_p), 8)
    pid = ctypes.c_long(os.getpid())
    one = ctypes.c_ulong(1)
    zero = ctypes.c_ulong(0)
    thread_result: list[bool] = []

    def in_thread() -> None:
        thread_result.append(libc.refused("socket", AF_INET, SOCK_STREAM, 0))

    worker = threading.Thread(target=in_thread)
    worker.start()
    worker.join()
    return {
        "ioUringRefused": libc.refused("io_uring_setup", 1, params),
        "tracemeRefused": libc.refused("ptrace", PTRACE_TRACEME, 0, 0, 0),
        "vmReadRefused": libc.refused(
            "process_vm_readv", pid, ctypes.byref(local), one, ctypes.byref(remote), one, zero
        ),
        # A zero size makes `clone3` fail with EINVAL when unfiltered, so a missing rule never
        # starts a process here.
        "clone3Refused": libc.refused("clone3", None, zero, code=errno.ENOSYS),
        "threadFiltered": thread_result == [True],
    }


def run_probe(filter_name: syscall_filter.FilterName = syscall_filter.DEFAULT) -> ChildResult:
    """The probe under the filter this child loaded: the default probe, or the `convert`-mode
    probe under the `convert` filter."""
    from docchat_ingestion.isolation.results import ConvertProbeOk, ProbeFailed, ProbeOk  # noqa: PLC0415 - after the filter

    libc = _Libc()
    success: type[ProbeOk | ConvertProbeOk]
    if filter_name == syscall_filter.CONVERT:
        checks, success = convert_probe_checks(libc), ConvertProbeOk
    else:
        checks, success = probe_checks(libc), ProbeOk
    if not all(checks.values()):
        return ProbeFailed(ok=False, code="parse_isolation_failed")
    return success.model_validate({"ok": True, "filterInstalled": True, **checks})


class _Partition(Protocol):
    def __call__(
        self, pdf: Path, *, first: int, last: int, page_count: int, parameters: dict[str, object]
    ) -> ExtractPartV1: ...


def run_partition(request: ChildRequest, scratch: Path) -> ChildResult:
    """The page-wise `partition` of one part: the method writes nothing itself; this child writes
    its extract part to `partition.json`. A PDF that `pikepdf` can't open, or whose part it can't
    copy, is the command's own failure result `corrupt_document` (section 15.6 item 5.5), which
    the parent stores as the item's code (section 6.3 step 9), not a crash read as
    `parse_failed`."""
    from docchat_ingestion.isolation.results import PARTITION, PDF, SOURCE, Ok, PartitionFailed  # noqa: PLC0415 - after the filter

    corrupt = PartitionFailed(ok=False, code="corrupt_document")
    if request.content_type != PDF or request.pages is None:
        return corrupt
    page_count = request.parameters.get("pageCount")
    if isinstance(page_count, bool) or not isinstance(page_count, int):
        return corrupt
    method = importlib.import_module(PAGEWISE_PARTITION)
    partition: _Partition = method.partition_part
    corrupt_part: type[Exception] = method.CorruptPartError
    try:
        part = partition(
            scratch / SOURCE,
            first=request.pages.first,
            last=request.pages.last,
            page_count=page_count,
            parameters=dict(request.parameters),
        )
    except corrupt_part:
        return corrupt
    (scratch / PARTITION).write_bytes(part.model_dump_json(by_alias=True).encode("utf-8"))
    return Ok(ok=True)


def dispatch(
    request: ChildRequest,
    scratch: Path,
    filter_name: syscall_filter.FilterName = syscall_filter.DEFAULT,
) -> ChildResult:
    """Import the command's modules and run it; `filter_name` is the filter this child loaded."""
    from docchat_ingestion.isolation.results import SOURCE, Command  # noqa: PLC0415 - after the filter

    handlers: dict[Command, Callable[[], ChildResult]] = {
        Command.PROBE: lambda: run_probe(filter_name),
        Command.PARTITION: lambda: run_partition(request, scratch),
    }
    if request.command in handlers:
        return handlers[request.command]()
    if request.command is Command.INSPECT:
        from docchat_ingestion.isolation.office_checks import inspect_document  # noqa: PLC0415 - after the filter

        return inspect_document(scratch / SOURCE, request.content_type)
    if request.command is Command.CONVERT:
        from docchat_ingestion.isolation.convert import convert_in_child  # noqa: PLC0415 - after the filter

        return convert_in_child(scratch, request.content_type)
    from docchat_ingestion.isolation import pdf_schema  # noqa: PLC0415 - after the filter

    if request.command is Command.RENDER:
        return pdf_schema.render_pages(scratch, request)
    if request.command is Command.ASSEMBLE:
        return pdf_schema.assemble_document(scratch, request)
    return pdf_schema.verify_render(scratch, request)


def main(argv: list[str]) -> int:
    filter_name = filter_named(argv)
    if filter_name is None or not harden(filter_name):
        return EXIT_FILTER
    from docchat_ingestion.isolation.results import RESULT, ChildRequest  # noqa: PLC0415 - after the filter

    request_path = Path(argv[1])
    request = ChildRequest.model_validate_json(request_path.read_bytes())
    if not fits_filter(request.command, filter_name):
        return EXIT_FILTER
    if request.fail_on_network:
        sys.addaudithook(network_hook)
    scratch = Path.cwd()
    result = dispatch(request, scratch, filter_name)
    (scratch / RESULT).write_bytes(result.model_dump_json(by_alias=True).encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
