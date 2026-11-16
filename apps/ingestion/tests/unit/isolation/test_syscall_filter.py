"""`docchat_ingestion.isolation.syscall_filter`: the parsing child's system-call filter
(`14-ingestion.md` section 15.6 item 12).

The filter is loaded in a fresh interpreter (`setpriv --no-new-privs`, as the child has the bit),
never in the test process; that interpreter reports each call's outcome as JSON (the cases of
T-203 that need no other user). The same report runs under the `convert` child's filter
(`14-ingestion.md` section 15.11 item 7.3; acceptance criterion 46): `AF_UNIX` sockets alone are
allowed, and everything else the default filter refuses is still refused. The binding's error
paths and the rules each filter adds run in-process on a stand-in library.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import json
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable
from typing import Any, cast

import pytest

from docchat_ingestion.isolation import syscall_filter
from docchat_ingestion.isolation.syscall_filter import FilterError, ScmpArgCmp

pytestmark = pytest.mark.skipif(
    ctypes.util.find_library("seccomp") is None or shutil.which("setpriv") is None,
    reason="libseccomp.so.2 and setpriv are needed to load the filter",
)

REPORT = r"""
import ctypes, errno, json, os, socket, subprocess, sys, threading
from docchat_ingestion.isolation import syscall_filter as sf

libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
lib = sf.open_library()
numbers = {name: sf.resolve(lib, name)
           for name in (*sf.REFUSED_CALLS, "clone", "clone3", "getpid")}
getattr(sf, sys.argv[1])()
report = {}

def errno_of(name, *args):
    ctypes.set_errno(0)
    result = libc.syscall(numbers[name], *args)
    if result == 0 and name == "clone":
        os._exit(0)
    return errno.errorcode.get(ctypes.get_errno(), str(result)) if result == -1 else "succeeded"

for family in ("AF_INET", "AF_INET6", "AF_UNIX", "AF_NETLINK", "AF_PACKET"):
    kind = socket.SOCK_RAW if family in ("AF_NETLINK", "AF_PACKET") else socket.SOCK_STREAM
    report["socket " + family] = errno_of("socket", getattr(socket, family), kind, 0)
# The family is an int: a value with a bit above it set is AF_UNIX to the kernel, not to the rule.
report["socket AF_UNIX with a high bit"] = errno_of(
    "socket", ctypes.c_long(socket.AF_UNIX | 1 << 32), socket.SOCK_STREAM, 0)
fds = (ctypes.c_int * 2)()
report["socketpair"] = errno_of("socketpair", socket.AF_UNIX, socket.SOCK_STREAM, 0, fds)
report["socketpair AF_INET"] = errno_of("socketpair", socket.AF_INET, socket.SOCK_STREAM, 0, fds)
report["io_uring_setup"] = errno_of("io_uring_setup", 1, ctypes.create_string_buffer(120))
report["ptrace"] = errno_of("ptrace", 0, 0, 0, 0)
for name in sf.REFUSED_CALLS:
    if name not in report and name not in sf.UNIX_FAMILY_CALLS:
        report[name] = errno_of(name, 0, 0, 0, 0, 0)
for flag, value in sf.NAMESPACE_FLAGS.items():
    report["clone " + flag] = errno_of("clone", ctypes.c_ulong(value | 17), 0, 0, 0, 0)
report["clone3"] = errno_of("clone3", None, ctypes.c_ulong(0))
thread = {}
worker = threading.Thread(target=lambda: thread.update(socket=errno_of("socket", 2, 1, 0)))
worker.start(); worker.join()
report["thread socket"] = thread["socket"]
pid = os.fork()
if pid == 0:
    os._exit(0)
report["fork"] = os.waitpid(pid, 0)[1]
probe = ("import socket, sys\ntry:\n socket.socket(int(sys.argv[1]))\n"
         "except OSError as e:\n raise SystemExit(e.errno)")
for family in ("AF_INET", "AF_UNIX"):
    family_number = str(int(getattr(socket, family)))
    report["subprocess " + family] = subprocess.run(
        [sys.executable, "-c", probe, family_number]).returncode
report["subprocess"] = report["subprocess AF_INET"]
print(json.dumps(report))
"""


def run_filtered(script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - the test's own interpreter and script
        ["setpriv", "--no-new-privs", "--", sys.executable, "-I", "-c", script, *arguments],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def filtered_report(installer: str) -> dict[str, Any]:
    completed = run_filtered(REPORT, installer)
    assert completed.returncode == 0, completed.stderr
    value: dict[str, Any] = json.loads(completed.stdout)
    return value


@pytest.fixture(scope="module")
def report() -> dict[str, Any]:
    """Each call's outcome under the default filter, `install()`."""
    return filtered_report("install")


@pytest.fixture(scope="module")
def convert_report() -> dict[str, Any]:
    """Each call's outcome under the `convert` filter, `install_convert()`."""
    return filtered_report("install_convert")


@pytest.mark.parametrize("family", ["AF_INET", "AF_INET6", "AF_UNIX", "AF_NETLINK"])
def test_t203_socket_fails_for_every_family(report: dict[str, Any], family: str) -> None:
    assert report[f"socket {family}"] == "EPERM"


@pytest.mark.parametrize(
    "name",
    [
        "socketpair",
        "io_uring_setup",
        "userfaultfd",
        "add_key",
        "request_key",
        "pidfd_getfd",
        "process_madvise",
        "kcmp",
        "mq_open",
        "msgget",
        "semget",
        "shmget",
        "ptrace",
    ],
)
def test_t203_refused_calls_fail_with_eperm(report: dict[str, Any], name: str) -> None:
    assert report[name] == "EPERM"


@pytest.mark.parametrize("flag", list(syscall_filter.NAMESPACE_FLAGS))
def test_t203_a_clone_with_a_namespace_flag_fails(report: dict[str, Any], flag: str) -> None:
    assert report[f"clone {flag}"] == "EPERM"


def test_t203_clone3_fails_with_enosys_while_threads_forks_and_processes_work(
    report: dict[str, Any],
) -> None:
    assert report["clone3"] == "ENOSYS"
    assert report["fork"] == 0
    # The grandchild inherits the filter: its socket() fails with EPERM (exit status 1).
    assert report["subprocess"] == errno.EPERM


def test_t203_a_thread_started_after_the_load_is_filtered(report: dict[str, Any]) -> None:
    assert report["thread socket"] == "EPERM"


def test_t203_the_default_filter_refuses_af_unix_in_a_grandchild_too(
    report: dict[str, Any],
) -> None:
    assert report["subprocess AF_UNIX"] == errno.EPERM
    assert report["socketpair AF_INET"] == "EPERM"


# --- The `convert` child's filter (section 15.11 item 7.3) -------------------------------------


def test_t203_the_convert_filter_allows_af_unix_sockets_alone(
    convert_report: dict[str, Any],
) -> None:
    assert convert_report["socket AF_UNIX"] == "succeeded"
    assert convert_report["socketpair"] == "succeeded"
    # A grandchild inherits the variant: its AF_UNIX socket opens, its AF_INET socket doesn't.
    assert convert_report["subprocess AF_UNIX"] == 0
    assert convert_report["subprocess AF_INET"] == errno.EPERM


@pytest.mark.parametrize(
    "case",
    [
        "socket AF_INET",
        "socket AF_INET6",
        "socket AF_NETLINK",
        "socket AF_PACKET",
        "socket AF_UNIX with a high bit",
        "socketpair AF_INET",
        "thread socket",
    ],
)
def test_t203_the_convert_filter_refuses_every_other_family(
    convert_report: dict[str, Any], case: str
) -> None:
    assert convert_report[case] == "EPERM"


@pytest.mark.parametrize("name", syscall_filter.CONVERT_REFUSED_CALLS)
def test_t203_the_convert_filter_refuses_every_other_call_with_eperm(
    convert_report: dict[str, Any], name: str
) -> None:
    assert convert_report[name] == "EPERM"


@pytest.mark.parametrize("flag", list(syscall_filter.NAMESPACE_FLAGS))
def test_t203_the_convert_filter_refuses_a_clone_with_a_namespace_flag(
    convert_report: dict[str, Any], flag: str
) -> None:
    assert convert_report[f"clone {flag}"] == "EPERM"


def test_t203_the_convert_filter_refuses_clone3_with_enosys_and_still_forks(
    convert_report: dict[str, Any],
) -> None:
    assert convert_report["clone3"] == "ENOSYS"
    assert convert_report["fork"] == 0


@pytest.mark.parametrize("installer", ["install", "install_convert"])
def test_t203_an_x32_call_kills_the_process(installer: str) -> None:
    script = (
        "import ctypes, sys\n"
        "from docchat_ingestion.isolation import syscall_filter as sf\n"
        "lib = sf.open_library(); getpid = sf.resolve(lib, 'getpid')\n"
        "getattr(sf, sys.argv[1])()\n"
        "ctypes.CDLL(None).syscall(getpid | 0x40000000)\n"
        "print('survived')\n"
    )
    completed = run_filtered(script, installer)

    assert completed.returncode == -signal.SIGSYS
    assert "survived" not in completed.stdout


def test_constants_follow_the_libseccomp_header() -> None:
    assert syscall_filter.scmp_act_errno(errno.EPERM) == 0x00050001
    assert syscall_filter.scmp_act_errno(errno.ENOSYS) == 0x00050000 | errno.ENOSYS
    assert len(syscall_filter.REFUSED_CALLS) == 24
    assert syscall_filter.REFUSED_CALLS[:2] == ("socket", "socketpair")
    assert syscall_filter.SCMP_CMP_NE == 1
    assert syscall_filter.AF_UNIX == 1
    assert syscall_filter.UNIX_FAMILY_CALLS == ("socket", "socketpair")
    convert_refused = syscall_filter.CONVERT_REFUSED_CALLS
    assert convert_refused == syscall_filter.REFUSED_CALLS[2:]
    assert (syscall_filter.DEFAULT, syscall_filter.CONVERT) == ("default", "convert")


def test_every_name_resolves_and_an_unknown_one_fails() -> None:
    lib = syscall_filter.open_library()

    for name in (*syscall_filter.REFUSED_CALLS, syscall_filter.CLONE3, "clone"):
        assert syscall_filter.resolve(lib, name) >= 0
    with pytest.raises(FilterError):
        syscall_filter.resolve(lib, "no_such_call")


@pytest.mark.parametrize("installer", ["install", "install_convert"])
def test_a_library_that_doesnt_load_fails(monkeypatch: pytest.MonkeyPatch, installer: str) -> None:
    monkeypatch.setattr(syscall_filter, "LIBRARY", "libdocchat-no-such-library.so.0")

    with pytest.raises(FilterError):
        getattr(syscall_filter, installer)()


class StandIn:
    """A library whose calls return the values a test chooses, recording each call, its
    arguments and the number each name resolved to."""

    def __init__(self, *, init: int = 1, failing: str | None = None) -> None:
        self.calls: list[str] = []
        self.arguments: list[tuple[str, tuple[object, ...]]] = []
        self.numbers: dict[str, int] = {}
        self._init = init
        self._failing = failing
        self._numbers = iter(range(1, 1000))

    def __getattr__(self, name: str) -> Callable[..., int | None]:
        def call(*args: object) -> int | None:
            self.calls.append(name)
            self.arguments.append((name, args))
            if name == "seccomp_init":
                return self._init
            if name == "seccomp_syscall_resolve_name":
                number = next(self._numbers)
                assert isinstance(args[0], bytes)
                self.numbers[args[0].decode()] = number
                return number
            if name == "seccomp_release":
                return None
            return -errno.EINVAL if name == self._failing else 0

        return call

    def rules(self) -> tuple[list[tuple[int, int]], list[tuple[int, int, ScmpArgCmp]]]:
        """The plain rules as (action, number) and the rules with an argument comparison as
        (action, number, comparison), in the order they were added."""
        plain: list[tuple[int, int]] = []
        compared: list[tuple[int, int, ScmpArgCmp]] = []
        for name, args in self.arguments:
            if name == "seccomp_rule_add":
                plain.append((cast(int, args[1]), cast(int, args[2])))
            elif name == "seccomp_rule_add_array":
                # `ctypes.byref` returns a reference whose `_obj` is the structure it points at.
                comparison = cast(ScmpArgCmp, cast(Any, args[4])._obj)
                compared.append((cast(int, args[1]), cast(int, args[2]), comparison))
        return plain, compared


def installed_with(monkeypatch: pytest.MonkeyPatch, installer: str, lib: StandIn) -> StandIn:
    """Run the production `installer` against the stand-in library."""
    monkeypatch.setattr(syscall_filter, "open_library", lambda: lib)
    getattr(syscall_filter, installer)()
    return lib


def test_the_default_filter_refuses_every_socket_call_whatever_its_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lib = installed_with(monkeypatch, "install", StandIn())
    plain, compared = lib.rules()
    eperm = syscall_filter.scmp_act_errno(errno.EPERM)

    assert (eperm, lib.numbers["socket"]) in plain
    assert (eperm, lib.numbers["socketpair"]) in plain
    assert len(plain) == 25
    assert [comparison.op for _, _, comparison in compared] == [
        syscall_filter.SCMP_CMP_MASKED_EQ
    ] * 7


def test_the_convert_filter_refuses_socket_calls_only_for_other_families(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lib = installed_with(monkeypatch, "install_convert", StandIn())
    plain, compared = lib.rules()
    eperm = syscall_filter.scmp_act_errno(errno.EPERM)
    sockets = {lib.numbers["socket"], lib.numbers["socketpair"]}

    # Every other call of rule 1 and clone3 stay plain rules; neither socket call is one.
    assert {number for _, number in plain} == {
        lib.numbers[name] for name in (*syscall_filter.CONVERT_REFUSED_CALLS, "clone3")
    }
    assert not sockets & {number for _, number in plain}
    family_rules = [rule for rule in compared if rule[1] in sockets]
    assert sorted(number for _, number, _ in family_rules) == sorted(sockets)
    for action, _number, comparison in family_rules:
        assert action == eperm
        assert (comparison.arg, comparison.op, comparison.datum_a, comparison.datum_b) == (
            0,
            syscall_filter.SCMP_CMP_NE,
            syscall_filter.AF_UNIX,
            0,
        )
    # The seven namespace-flag rules on clone are unchanged.
    namespace_rules = [rule for rule in compared if rule[1] == lib.numbers["clone"]]
    assert [(rule[2].op, rule[2].datum_a) for rule in namespace_rules] == [
        (syscall_filter.SCMP_CMP_MASKED_EQ, flag)
        for flag in syscall_filter.NAMESPACE_FLAGS.values()
    ]
    assert lib.calls[:3] == ["seccomp_init", "seccomp_attr_set", "seccomp_attr_set"]
    assert lib.calls[-2:] == ["seccomp_load", "seccomp_release"]


@pytest.mark.parametrize("failing", ["seccomp_rule_add_array", "seccomp_load"])
def test_a_failing_call_fails_the_convert_filter_and_releases_the_context(
    monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    lib = StandIn(failing=failing)

    with pytest.raises(FilterError):
        installed_with(monkeypatch, "install_convert", lib)
    assert lib.calls[-1] == "seccomp_release"
    # A failing rule leaves nothing loaded.
    assert lib.calls.count("seccomp_load") == (failing == "seccomp_load")


def test_the_filter_is_built_in_the_documented_order() -> None:
    lib = StandIn()
    syscall_filter.build_and_load(lib)  # type: ignore[arg-type]

    assert lib.calls[:3] == ["seccomp_init", "seccomp_attr_set", "seccomp_attr_set"]
    assert lib.calls.count("seccomp_rule_add") == 25
    assert lib.calls.count("seccomp_rule_add_array") == 7
    assert lib.calls[-2:] == ["seccomp_load", "seccomp_release"]


@pytest.mark.parametrize(
    "failing", ["seccomp_attr_set", "seccomp_rule_add", "seccomp_rule_add_array", "seccomp_load"]
)
def test_a_failing_call_fails_the_filter_and_releases_the_context(failing: str) -> None:
    lib = StandIn(failing=failing)

    with pytest.raises(FilterError):
        syscall_filter.build_and_load(lib)  # type: ignore[arg-type]
    assert lib.calls[-1] == "seccomp_release"


def test_a_context_that_doesnt_initialise_fails() -> None:
    with pytest.raises(FilterError):
        syscall_filter.build_and_load(StandIn(init=0))  # type: ignore[arg-type]
