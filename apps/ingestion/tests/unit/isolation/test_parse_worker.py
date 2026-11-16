"""`docchat_ingestion.isolation.parse_worker`: the child's entry point (`14-ingestion.md` section
15.6 items 5 and 12, section 15.5).

Whole children run as `setpriv --no-new-privs -- python -I -m ...parse_worker request.json` in a
temporary directory, which needs no other user, and with `--filter=convert` after the request
path for the `convert` child's filter (section 15.11 item 7.3); the steps also run in-process with
the filter and the audit hook replaced, because neither can be undone in the test process.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import importlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from docchat_ingestion.isolation import convert, parse_worker, syscall_filter
from docchat_ingestion.isolation.results import (
    DOCX,
    PDF,
    RESULT,
    ChildRequest,
    Command,
    ConvertProbeOk,
    ExtractPartV1,
    InspectOk,
    Limits,
    Ok,
    PartitionFailed,
    ProbeFailed,
    ProbeOk,
)
from docchat_ingestion.methods.pagewise import partition

type PdfFactory = Callable[..., bytes]

needs_filter = pytest.mark.skipif(
    ctypes.util.find_library("seccomp") is None or shutil.which("setpriv") is None,
    reason="libseccomp.so.2 and setpriv are needed to load the filter",
)


def request(command: str, **fields: object) -> ChildRequest:
    value: dict[str, object] = {
        "command": command,
        "contentType": PDF,
        "sourcePath": "source.bin",
        "pages": None,
        "mode": None,
        "parameters": {},
        "limits": Limits(max_pages=2000, render_max_total_bytes=524_288_000),
    }
    value.update(fields)
    return ChildRequest.model_validate(value)


def run_child(
    directory: Path,
    child_request: ChildRequest,
    prelude: str | None = None,
    extra: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    """Run a whole child on `child_request`; `extra` follows the request path on its command
    line."""
    (directory / "request.json").write_bytes(child_request.encode())
    request_path = str(directory / "request.json")
    if prelude is None:
        arguments = ["-I", "-m", parse_worker.__name__, request_path, *extra]
    else:
        arguments = [
            "-I",
            "-c",
            prelude
            + "\nimport runpy, sys\n"
            + f"sys.argv = {['parse_worker', request_path, *extra]!r}\n"
            + f"runpy.run_module({parse_worker.__name__!r}, run_name='__main__')\n",
        ]
    return subprocess.run(  # noqa: S603 - the test's own interpreter and script
        ["setpriv", "--no-new-privs", "--", sys.executable, *arguments],  # noqa: S607
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@needs_filter
def test_t206_the_probe_child_reports_every_check(tmp_path: Path) -> None:
    completed = run_child(tmp_path, request("probe", sourcePath=None))

    assert completed.returncode == 0, completed.stderr
    result = ProbeOk.model_validate_json((tmp_path / RESULT).read_bytes())
    assert all(result.model_dump().values())


@needs_filter
def test_an_inspect_child_writes_its_result(tmp_path: Path, make_pdf: PdfFactory) -> None:
    (tmp_path / "source.bin").write_bytes(make_pdf(pages=2))

    completed = run_child(tmp_path, request("inspect"))

    assert completed.returncode == 0, completed.stderr
    assert json.loads((tmp_path / RESULT).read_text()) == {"ok": True, "pageCount": 2}


@needs_filter
def test_t203_a_second_thread_before_the_load_exits_4_unread(tmp_path: Path) -> None:
    prelude = (
        "import threading, time\n"
        "threading.Thread(target=time.sleep, args=(3,), daemon=True).start()"
    )

    completed = run_child(tmp_path, request("probe", sourcePath=None), prelude)

    assert completed.returncode == parse_worker.EXIT_FILTER
    assert not (tmp_path / RESULT).exists()


@needs_filter
def test_t203_a_filter_that_cant_load_exits_4_unread(tmp_path: Path) -> None:
    prelude = (
        "from docchat_ingestion.isolation import syscall_filter\n"
        "syscall_filter.LIBRARY = 'libdocchat-no-such-library.so.0'"
    )

    completed = run_child(tmp_path, request("probe", sourcePath=None), prelude)

    assert completed.returncode == parse_worker.EXIT_FILTER
    assert not (tmp_path / RESULT).exists()


@needs_filter
def test_the_convert_mode_probe_child_reports_every_check(tmp_path: Path) -> None:
    completed = run_child(
        tmp_path, request("probe", sourcePath=None), extra=(parse_worker.CONVERT_FILTER_ARGUMENT,)
    )

    assert completed.returncode == 0, completed.stderr
    result = ConvertProbeOk.model_validate_json((tmp_path / RESULT).read_bytes())
    assert all(result.model_dump().values())


CONVERTER_STAND_IN = r"""#!{python} -I
import json, os, socket, sys

def opened(make):
    try:
        make().close()
    except OSError as error:
        return error.errno
    return 0

report = {{
    "AF_UNIX": opened(lambda: socket.socket(socket.AF_UNIX)),
    "socketpair": opened(lambda: socket.socketpair()[0]),
    "AF_INET": opened(lambda: socket.socket(socket.AF_INET)),
    "AF_INET6": opened(lambda: socket.socket(socket.AF_INET6)),
    "AF_NETLINK": opened(lambda: socket.socket(socket.AF_NETLINK, socket.SOCK_RAW)),
}}
outdir = sys.argv[sys.argv.index("--outdir") + 1]
with open(os.path.join(outdir, "source.pdf"), "w") as out:
    json.dump(report, out)
"""


@needs_filter
def test_t203_a_convert_childs_converter_opens_af_unix_sockets_alone(tmp_path: Path) -> None:
    """The converter process a `convert` child starts inherits the `convert` filter: its
    `AF_UNIX` sockets open and every other family fails with `EPERM` (acceptance criterion 46)."""
    stand_in = tmp_path / "soffice"
    stand_in.write_text(CONVERTER_STAND_IN.format(python=sys.executable))
    stand_in.chmod(0o755)
    (tmp_path / "out").mkdir()
    (tmp_path / "source.bin").write_bytes(b"PK office source")
    prelude = (
        f"from docchat_ingestion.isolation import convert\nconvert.SOFFICE = {str(stand_in)!r}"
    )

    completed = run_child(
        tmp_path,
        request("convert", contentType=DOCX),
        prelude,
        extra=(parse_worker.CONVERT_FILTER_ARGUMENT,),
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads((tmp_path / RESULT).read_text()) == {"ok": True}
    assert json.loads((tmp_path / "converted.pdf").read_text()) == {
        "AF_UNIX": 0,
        "socketpair": 0,
        "AF_INET": errno.EPERM,
        "AF_INET6": errno.EPERM,
        "AF_NETLINK": errno.EPERM,
    }


@needs_filter
@pytest.mark.parametrize(
    ("command", "extra"),
    [
        ("inspect", (parse_worker.CONVERT_FILTER_ARGUMENT,)),
        ("render", (parse_worker.CONVERT_FILTER_ARGUMENT,)),
        ("convert", ()),
    ],
)
def test_a_command_its_filter_wasnt_built_for_exits_4_without_a_result(
    tmp_path: Path, make_pdf: PdfFactory, command: str, extra: tuple[str, ...]
) -> None:
    (tmp_path / "source.bin").write_bytes(make_pdf(pages=1))
    content_type = DOCX if command == "convert" else PDF

    completed = run_child(tmp_path, request(command, contentType=content_type), extra=extra)

    assert completed.returncode == parse_worker.EXIT_FILTER
    assert sorted(path.name for path in tmp_path.iterdir()) == ["request.json", "source.bin"]


@needs_filter
@pytest.mark.parametrize(
    "extra",
    [("--filter=default",), ("convert",), (parse_worker.CONVERT_FILTER_ARGUMENT, "--again")],
)
def test_a_command_line_that_names_no_known_filter_exits_4_unread(
    tmp_path: Path, extra: tuple[str, ...]
) -> None:
    completed = run_child(tmp_path, request("probe", sourcePath=None), extra=extra)

    assert completed.returncode == parse_worker.EXIT_FILTER
    assert not (tmp_path / RESULT).exists()


@needs_filter
def test_the_network_audit_hook_ends_a_child_that_connects(tmp_path: Path) -> None:
    # The command's module is shared with the run module, so the stand-in replaces the check.
    prelude = (
        "from docchat_ingestion.isolation import office_checks\n"
        "def connect(path, content_type):\n"
        "    import socket\n"
        "    socket.getaddrinfo('localhost', 80)\n"
        "office_checks.inspect_document = connect"
    )

    completed = run_child(tmp_path, request("inspect"), prelude)

    assert completed.returncode == parse_worker.EXIT_NETWORK
    assert not (tmp_path / RESULT).exists()


def test_harden_sets_the_umask_clears_dumpable_and_loads_the_filter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[object] = []
    (tmp_path / "1").mkdir()

    def umask(mask: int) -> int:
        calls.append(("umask", mask))
        return 0

    def prctl(*args: int) -> int:
        calls.append(args)
        return 0

    monkeypatch.setattr(parse_worker, "TASKS", str(tmp_path))
    monkeypatch.setattr(os, "umask", umask)
    monkeypatch.setattr(ctypes, "CDLL", lambda *_args, **_kwargs: SimpleNamespace(prctl=prctl))
    monkeypatch.setattr(syscall_filter, "install", lambda: calls.append("filter"))

    assert parse_worker.harden()
    assert calls == [("umask", 0o077), (parse_worker.PR_SET_DUMPABLE, 0, 0, 0, 0), "filter"]


def test_harden_fails_on_prctl_a_second_thread_or_the_filter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(os, "umask", lambda _mask: 0)
    prctl_result = [1]
    monkeypatch.setattr(
        ctypes,
        "CDLL",
        lambda *_args, **_kwargs: SimpleNamespace(prctl=lambda *_args: prctl_result[0]),
    )
    monkeypatch.setattr(parse_worker, "TASKS", str(tmp_path))
    (tmp_path / "1").mkdir()

    assert not parse_worker.harden()
    prctl_result[0] = 0
    (tmp_path / "2").mkdir()
    assert not parse_worker.harden()
    (tmp_path / "2").rmdir()

    def refuse() -> None:
        raise syscall_filter.FilterError("refused")

    monkeypatch.setattr(syscall_filter, "install", refuse)
    assert not parse_worker.harden()


def test_main_exits_4_when_hardening_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(parse_worker, "harden", lambda _filter_name: False)

    assert parse_worker.main(["parse_worker", "/nonexistent/request.json"]) == 4


def test_main_installs_the_hook_runs_the_command_and_writes_the_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, make_pdf: PdfFactory
) -> None:
    hooks: list[object] = []
    monkeypatch.setattr(parse_worker, "harden", lambda _filter_name: True)
    monkeypatch.setattr(sys, "addaudithook", hooks.append)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "source.bin").write_bytes(make_pdf(pages=1))
    (tmp_path / "request.json").write_bytes(request("inspect").encode())

    assert parse_worker.main(["parse_worker", str(tmp_path / "request.json")]) == 0
    assert hooks == [parse_worker.network_hook]
    assert json.loads((tmp_path / RESULT).read_text()) == {"ok": True, "pageCount": 1}


def test_the_command_line_names_the_filter() -> None:
    named = parse_worker.filter_named

    assert named(["parse_worker", "request.json"]) == "default"
    assert named(["parse_worker", "request.json", "--filter=convert"]) == "convert"
    assert parse_worker.CONVERT_FILTER_ARGUMENT == "--filter=convert"
    for argv in (
        ["parse_worker"],
        ["parse_worker", "request.json", "--filter=default"],
        ["parse_worker", "request.json", "convert"],
        ["parse_worker", "request.json", "--filter=convert", "--filter=convert"],
    ):
        assert named(argv) is None


def test_harden_loads_the_filter_it_is_given(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    loaded: list[str] = []
    (tmp_path / "1").mkdir()
    monkeypatch.setattr(parse_worker, "TASKS", str(tmp_path))
    monkeypatch.setattr(os, "umask", lambda _mask: 0)
    monkeypatch.setattr(
        ctypes, "CDLL", lambda *_args, **_kwargs: SimpleNamespace(prctl=lambda *_args: 0)
    )
    monkeypatch.setattr(syscall_filter, "install", lambda: loaded.append("default"))
    monkeypatch.setattr(syscall_filter, "install_convert", lambda: loaded.append("convert"))

    assert parse_worker.harden("convert")
    assert parse_worker.harden("default")
    assert parse_worker.harden()
    assert loaded == ["convert", "default", "default"]

    def refuse() -> None:
        raise syscall_filter.FilterError("refused")

    monkeypatch.setattr(syscall_filter, "install_convert", refuse)
    assert not parse_worker.harden("convert")


def hardening_into(hardened: list[str]) -> Callable[[str], bool]:
    """A stand-in for `harden` that succeeds and records the filter it was asked for."""

    def harden(filter_name: str) -> bool:
        hardened.append(filter_name)
        return True

    return harden


def test_main_exits_4_before_hardening_on_a_command_line_without_a_known_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hardened: list[str] = []
    monkeypatch.setattr(parse_worker, "harden", hardening_into(hardened))

    # The request path doesn't exist: reading it would raise.
    assert parse_worker.main(["parse_worker", "/nonexistent/request.json", "--filter=all"]) == 4
    assert parse_worker.main(["parse_worker"]) == 4
    assert hardened == []


@pytest.mark.parametrize(
    ("command", "extra"),
    [("inspect", ("--filter=convert",)), ("partition", ("--filter=convert",)), ("convert", ())],
)
def test_main_exits_4_without_a_result_when_the_command_doesnt_fit_its_filter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str, extra: tuple[str, ...]
) -> None:
    hardened: list[str] = []
    monkeypatch.setattr(parse_worker, "harden", hardening_into(hardened))
    monkeypatch.setattr(sys, "addaudithook", lambda _hook: None)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "request.json").write_bytes(request(command).encode())

    assert parse_worker.main(["parse_worker", str(tmp_path / "request.json"), *extra]) == 4
    assert hardened == ["convert" if extra else "default"]
    assert not (tmp_path / RESULT).exists()


def test_main_runs_the_convert_mode_probe_under_the_convert_filter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    hardened: list[str] = []
    probed: list[str] = []
    monkeypatch.setattr(parse_worker, "harden", hardening_into(hardened))
    monkeypatch.setattr(sys, "addaudithook", lambda _hook: None)

    def run_probe(name: str) -> ProbeFailed:
        probed.append(name)
        return ProbeFailed(ok=False, code="parse_isolation_failed")

    monkeypatch.setattr(parse_worker, "run_probe", run_probe)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "request.json").write_bytes(request("probe", sourcePath=None).encode())

    assert parse_worker.main(["parse_worker", str(tmp_path / "request.json")]) == 0
    assert (
        parse_worker.main(["parse_worker", str(tmp_path / "request.json"), "--filter=convert"]) == 0
    )
    assert hardened == probed == ["default", "convert"]


def test_only_convert_runs_under_the_convert_filter_and_the_probe_under_either() -> None:
    fits = {
        command: (
            parse_worker.fits_filter(command, "default"),
            parse_worker.fits_filter(command, "convert"),
        )
        for command in Command
    }

    assert fits == {
        command: (command is not Command.CONVERT, command in {Command.CONVERT, Command.PROBE})
        for command in Command
    }


def test_the_hook_ends_the_child_only_on_network_events(monkeypatch: pytest.MonkeyPatch) -> None:
    exits: list[int] = []
    monkeypatch.setattr(os, "_exit", exits.append)

    parse_worker.network_hook("open", ("file",))
    parse_worker.network_hook("socket.connect", ())
    parse_worker.network_hook("socket.getaddrinfo", ())

    assert exits == [3, 3]


def test_dispatch_runs_inspect_render_assemble_and_verify(
    tmp_path: Path, make_pdf: PdfFactory
) -> None:
    (tmp_path / "source.bin").write_bytes(make_pdf(pages=1))

    assert parse_worker.dispatch(request("inspect"), tmp_path) == InspectOk(ok=True, pageCount=1)
    assert parse_worker.dispatch(request("render"), tmp_path).ok
    assert parse_worker.dispatch(
        request("verify-render", pages={"first": 1, "last": 1}, mode="pages", sourcePath=None),
        tmp_path,
    ).ok
    assert parse_worker.dispatch(
        request("assemble", parameters={"pageCount": 1}, sourcePath=None), tmp_path
    ).ok


def test_dispatch_converts_with_the_converter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub = tmp_path / "soffice"
    stub.write_text('#!/bin/sh\nfor last; do :; done\nmkdir -p out && cp "$last" out/source.pdf\n')
    stub.chmod(0o755)
    monkeypatch.setattr(convert, "SOFFICE", str(stub))
    (tmp_path / "source.bin").write_bytes(b"%PDF-1.7 converted")

    result = parse_worker.dispatch(request("convert", contentType=DOCX), tmp_path)

    assert result == Ok(ok=True)
    assert (tmp_path / "converted.pdf").read_bytes() == b"%PDF-1.7 converted"


def test_the_probe_result_follows_its_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    checks = dict.fromkeys(
        (
            "inetSocketRefused",
            "unixSocketRefused",
            "socketpairRefused",
            "ioUringRefused",
            "tracemeRefused",
            "vmReadRefused",
            "clone3Refused",
            "threadFiltered",
        ),
        True,
    )
    monkeypatch.setattr(parse_worker, "_Libc", lambda: None)
    monkeypatch.setattr(parse_worker, "probe_checks", lambda _libc: dict(checks))

    assert isinstance(parse_worker.run_probe(), ProbeOk)
    checks["tracemeRefused"] = False
    assert parse_worker.run_probe() == ProbeFailed(ok=False, code="parse_isolation_failed")


def test_probe_checks_ask_for_each_refusal() -> None:
    class Calls:
        def __init__(self) -> None:
            self.names: list[tuple[str, int]] = []

        def refused(self, name: str, *_args: object, code: int = 1) -> bool:
            self.names.append((name, code))
            return True

    calls = Calls()
    checks = parse_worker.probe_checks(calls)  # type: ignore[arg-type]

    assert all(checks.values())
    assert ("clone3", 38) in calls.names
    assert ("socket", 1) in calls.names


class RecordedCalls:
    """A stand-in for the probe's raw calls that refuses every one and records its arguments."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], int]] = []

    def refused(self, name: str, *args: object, code: int = errno.EPERM) -> bool:
        self.calls.append((name, args, code))
        return True


def test_the_convert_mode_probe_asks_for_the_other_families_and_every_other_rule() -> None:
    calls = RecordedCalls()

    checks = parse_worker.convert_probe_checks(calls)  # type: ignore[arg-type]

    assert set(checks) == {
        "inetSocketRefused",
        "inet6SocketRefused",
        "netlinkSocketRefused",
        "ioUringRefused",
        "tracemeRefused",
        "vmReadRefused",
        "clone3Refused",
        "threadFiltered",
    }
    families = {args[0] for name, args, _code in calls.calls if name == "socket"}
    assert families == {parse_worker.AF_INET, parse_worker.AF_INET6, parse_worker.AF_NETLINK}
    assert (parse_worker.AF_INET6, parse_worker.AF_NETLINK) == (10, 16)
    assert "socketpair" not in {name for name, _args, _code in calls.calls}
    assert {name for name, _args, _code in calls.calls} >= {
        "io_uring_setup",
        "ptrace",
        "process_vm_readv",
        "clone3",
    }
    assert ("clone3", errno.ENOSYS) in {(name, code) for name, _args, code in calls.calls}


def test_the_default_probe_asks_for_af_unix_and_socketpair() -> None:
    calls = RecordedCalls()

    parse_worker.probe_checks(calls)  # type: ignore[arg-type]

    families = {args[0] for name, args, _code in calls.calls if name == "socket"}
    assert families == {parse_worker.AF_INET, parse_worker.AF_UNIX}
    assert "socketpair" in {name for name, _args, _code in calls.calls}


def test_the_convert_mode_probe_result_follows_its_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    checks = dict.fromkeys(
        (
            "inetSocketRefused",
            "inet6SocketRefused",
            "netlinkSocketRefused",
            "ioUringRefused",
            "tracemeRefused",
            "vmReadRefused",
            "clone3Refused",
            "threadFiltered",
        ),
        True,
    )
    monkeypatch.setattr(parse_worker, "_Libc", lambda: None)
    monkeypatch.setattr(parse_worker, "convert_probe_checks", lambda _libc: dict(checks))

    assert isinstance(parse_worker.run_probe("convert"), ConvertProbeOk)
    checks["netlinkSocketRefused"] = False
    assert parse_worker.run_probe("convert") == ProbeFailed(ok=False, code="parse_isolation_failed")


def test_partition_writes_the_methods_extract_part(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, object] = {}

    def partition_part(
        pdf: Path, *, first: int, last: int, page_count: int, parameters: dict[str, object]
    ) -> ExtractPartV1:
        seen.update(pdf=pdf, first=first, last=last, page_count=page_count)
        return ExtractPartV1.model_validate(
            {
                "formatVersion": 1,
                "method": "pagewise",
                "pageCount": page_count,
                "firstPage": first,
                "lastPage": last,
                "pages": [],
            }
        )

    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda _name: SimpleNamespace(
            partition_part=partition_part, CorruptPartError=partition.CorruptPartError
        ),
    )
    part_request = request("partition", pages={"first": 1, "last": 2}, parameters={"pageCount": 9})

    assert parse_worker.dispatch(part_request, tmp_path) == Ok(ok=True)
    assert seen == {"pdf": tmp_path / "source.bin", "first": 1, "last": 2, "page_count": 9}
    assert json.loads((tmp_path / "partition.json").read_text())["pageCount"] == 9


def test_a_pdf_pikepdf_cant_open_is_the_partitions_corrupt_document(tmp_path: Path) -> None:
    """`pikepdf` refusing the part is the command's failure result, which the parent stores as
    `corrupt_document` (`14-ingestion.md` sections 6.3 step 9 and 15.6 item 5.5), not a crash
    read as `parse_failed`."""
    (tmp_path / "source.bin").write_bytes(b"%PDF-1.7 no objects")
    part_request = request(
        "partition",
        pages={"first": 1, "last": 1},
        parameters={"summarizeTables": True, "ocrLanguages": ["eng"], "pageCount": 1},
    )

    result = parse_worker.run_partition(part_request, tmp_path)

    assert result == PartitionFailed(ok=False, code="corrupt_document")
    assert not (tmp_path / "partition.json").exists()


def test_partition_refuses_a_request_it_cant_serve(tmp_path: Path) -> None:
    failed = PartitionFailed(ok=False, code="corrupt_document")

    assert parse_worker.run_partition(request("partition", contentType=DOCX), tmp_path) == failed
    assert (
        parse_worker.run_partition(request("partition", pages={"first": 1, "last": 1}), tmp_path)
        == failed
    )


def test_until_the_filter_loads_the_worker_imports_only_the_standard_library() -> None:
    script = (
        "import sys\n"
        "before = set(sys.modules)\n"
        "import docchat_ingestion.isolation.parse_worker\n"
        "loaded = set(sys.modules) - before\n"
        "first_party = {'docchat_ingestion', 'docchat_ingestion.isolation',\n"
        "               'docchat_ingestion.isolation.parse_worker',\n"
        "               'docchat_ingestion.isolation.syscall_filter'}\n"
        "names = {name.split('.')[0] for name in loaded - first_party}\n"
        "print(sorted(name for name in names if name not in sys.stdlib_module_names))\n"
    )
    completed = subprocess.run(  # noqa: S603 - the test's own interpreter and script
        [sys.executable, "-I", "-c", script], capture_output=True, text=True, check=True
    )

    assert completed.stdout.strip() == "[]"
