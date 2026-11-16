"""The parsing child's system-call filter (`14-ingestion.md` section 15.6 item 12; ADR 0049
decision 15): a first-party `ctypes` binding to `libseccomp.so.2` of the Debian 12 package
`libseccomp2`.

Two filters exist. `install()` builds the filter of section 15.6 item 12.2, under which `socket()`
and `socketpair()` fail for every address family; every child but `convert` loads it.
`install_convert()` builds the `convert` child's filter (section 15.11 item 7, outcome 3 of the
converter-sandbox spike SP-15): every rule of the first, except that `socket()` and `socketpair()`
fail only for an address family other than `AF_UNIX`, which LibreOffice needs between its own
processes.

`parse_worker` imports this module before anything that isn't the standard library, so it imports
only the standard library itself. No PyPI binding sits in this path: the published ones are built
for one system Python or unmaintained, so the boundary rests on the distribution's library and
this module.
"""

from __future__ import annotations

import ctypes
import errno
from collections.abc import Sequence
from typing import Final, Literal

LIBRARY: Final = "libseccomp.so.2"

# Values of libseccomp 2.5.4's public header `seccomp.h`.
SCMP_ACT_ALLOW: Final = 0x7FFF0000
SCMP_ACT_KILL_PROCESS: Final = 0x80000000
SCMP_FLTATR_ACT_BADARCH: Final = 2
SCMP_FLTATR_CTL_TSYNC: Final = 4
SCMP_CMP_NE: Final = 1
SCMP_CMP_MASKED_EQ: Final = 7
NR_SCMP_ERROR: Final = -1

# The kernel's `socket.h`.
AF_UNIX: Final = 1

type FilterName = Literal["default", "convert"]
DEFAULT: Final = "default"
"""The filter of every child but `convert` (`install()`)."""
CONVERT: Final = "convert"
"""The `convert` child's filter (`install_convert()`)."""


def scmp_act_errno(code: int) -> int:
    """`SCMP_ACT_ERRNO(x)`: fail the call with `errno` `code` without running it."""
    return 0x00050000 | (code & 0x0000FFFF)


REFUSED_CALLS: Final = (
    "socket",
    "socketpair",
    "ptrace",
    "process_vm_readv",
    "process_vm_writev",
    "process_madvise",
    "pidfd_getfd",
    "kcmp",
    "perf_event_open",
    "bpf",
    "userfaultfd",
    "mount",
    "unshare",
    "setns",
    "keyctl",
    "add_key",
    "request_key",
    "io_uring_setup",
    "io_uring_enter",
    "io_uring_register",
    "mq_open",
    "msgget",
    "semget",
    "shmget",
)
"""Calls refused with `EPERM` whatever their arguments (rule 1)."""

UNIX_FAMILY_CALLS: Final = ("socket", "socketpair")
"""In the `convert` filter, refused with `EPERM` only when their first argument, the address
family, isn't `AF_UNIX` (outcome 3 of SP-15)."""

CONVERT_REFUSED_CALLS: Final = tuple(
    name for name in REFUSED_CALLS if name not in UNIX_FAMILY_CALLS
)
"""The `convert` filter's calls refused whatever their arguments: every call of rule 1 but the
two of `UNIX_FAMILY_CALLS`."""

CLONE3: Final = "clone3"
"""Refused with `ENOSYS` (rule 2): its flags sit in a structure the filter can't read, and on
`ENOSYS` glibc falls back to `clone`, whose flags rule 3 reads."""

NAMESPACE_FLAGS: Final = {
    "CLONE_NEWNS": 0x00020000,
    "CLONE_NEWCGROUP": 0x02000000,
    "CLONE_NEWUTS": 0x04000000,
    "CLONE_NEWIPC": 0x08000000,
    "CLONE_NEWUSER": 0x10000000,
    "CLONE_NEWPID": 0x20000000,
    "CLONE_NEWNET": 0x40000000,
}
"""The kernel's `sched.h` namespace flags; a `clone` carrying any of them fails with `EPERM`
(rule 3), as `unshare` does."""


class FilterError(Exception):
    """The filter couldn't be built or loaded; the child then exits with code 4 unread."""


class ScmpArgCmp(ctypes.Structure):
    """`struct scmp_arg_cmp`: the array form of `SCMP_A0(...)` that `ctypes` can pass."""

    _fields_ = (
        ("arg", ctypes.c_uint),
        ("op", ctypes.c_int),
        ("datum_a", ctypes.c_uint64),
        ("datum_b", ctypes.c_uint64),
    )


def open_library() -> ctypes.CDLL:
    """Load `libseccomp.so.2` with the signature of each of the seven calls the filter uses."""
    try:
        lib = ctypes.CDLL(LIBRARY, use_errno=True)
    except OSError as failure:
        raise FilterError("libseccomp can't be loaded") from failure
    lib.seccomp_init.argtypes = (ctypes.c_uint32,)
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_attr_set.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32)
    lib.seccomp_attr_set.restype = ctypes.c_int
    lib.seccomp_syscall_resolve_name.argtypes = (ctypes.c_char_p,)
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    # `seccomp_rule_add` is variadic; with no argument comparisons it takes exactly these four.
    lib.seccomp_rule_add.argtypes = (ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint)
    lib.seccomp_rule_add.restype = ctypes.c_int
    lib.seccomp_rule_add_array.argtypes = (
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.POINTER(ScmpArgCmp),
    )
    lib.seccomp_rule_add_array.restype = ctypes.c_int
    lib.seccomp_load.argtypes = (ctypes.c_void_p,)
    lib.seccomp_load.restype = ctypes.c_int
    lib.seccomp_release.argtypes = (ctypes.c_void_p,)
    lib.seccomp_release.restype = None
    return lib


def resolve(lib: ctypes.CDLL, name: str) -> int:
    """The native number of the call `name`; a name libseccomp doesn't know is a failure."""
    number = int(lib.seccomp_syscall_resolve_name(name.encode("ascii")))
    if number == NR_SCMP_ERROR:
        raise FilterError("a system call name doesn't resolve")
    return number


def _checked(result: int) -> None:
    if result < 0:
        raise FilterError("a libseccomp call failed")


def build_and_load(
    lib: ctypes.CDLL,
    refused: Sequence[str] = REFUSED_CALLS,
    namespace_flags: Sequence[int] = tuple(NAMESPACE_FLAGS.values()),
    *,
    refuse_clone3: bool = True,
    unix_family_only: Sequence[str] = (),
) -> None:
    """Build the filter on a fresh context and load it for every thread of the process.

    Each call of `unix_family_only` is refused with `EPERM` unless its first argument equals
    `AF_UNIX`: the array form of `SCMP_A0(SCMP_CMP_NE, AF_UNIX)`, so every other address family,
    and any value with a bit set above the family, is still refused. The other parameters exist so
    a test can load a filter that leaves one rule out and prove that the isolation probe sees the
    gap; production calls `install()` or `install_convert()`.
    """
    ctx = lib.seccomp_init(SCMP_ACT_ALLOW)
    if not ctx:
        raise FilterError("seccomp_init failed")
    try:
        # A call through another architecture's interface, x32 numbers included, kills the child.
        _checked(lib.seccomp_attr_set(ctx, SCMP_FLTATR_ACT_BADARCH, SCMP_ACT_KILL_PROCESS))
        # A thread that already runs would otherwise stay unfiltered.
        _checked(lib.seccomp_attr_set(ctx, SCMP_FLTATR_CTL_TSYNC, 1))
        eperm = scmp_act_errno(errno.EPERM)
        for name in refused:
            _checked(lib.seccomp_rule_add(ctx, eperm, resolve(lib, name), 0))
        for name in unix_family_only:
            other_family = ScmpArgCmp(arg=0, op=SCMP_CMP_NE, datum_a=AF_UNIX, datum_b=0)
            _checked(
                lib.seccomp_rule_add_array(
                    ctx, eperm, resolve(lib, name), 1, ctypes.byref(other_family)
                )
            )
        if refuse_clone3:
            _checked(
                lib.seccomp_rule_add(ctx, scmp_act_errno(errno.ENOSYS), resolve(lib, CLONE3), 0)
            )
        clone = resolve(lib, "clone")
        for flag in namespace_flags:
            comparison = ScmpArgCmp(arg=0, op=SCMP_CMP_MASKED_EQ, datum_a=flag, datum_b=flag)
            _checked(lib.seccomp_rule_add_array(ctx, eperm, clone, 1, ctypes.byref(comparison)))
        _checked(lib.seccomp_load(ctx))
    finally:
        lib.seccomp_release(ctx)


def install() -> None:
    """Install the parsing child's filter; raises `FilterError` when any step fails."""
    build_and_load(open_library())


def install_convert() -> None:
    """Install the `convert` child's filter: `install()`'s rules, with `socket()` and
    `socketpair()` refused for every address family but `AF_UNIX`; raises `FilterError` when any
    step fails."""
    build_and_load(open_library(), CONVERT_REFUSED_CALLS, unix_family_only=UNIX_FAMILY_CALLS)
