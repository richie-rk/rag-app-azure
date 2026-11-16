"""`docchat_ingestion.isolation.runner`: the parse-user pool, scratch directories, starting and
bounding a child, and the parent's half of Render (`14-ingestion.md` sections 14.3, 14.7 and 15.6
to 15.8).

Two kinds of test. Stand-in children replace `ParsePool.start` and `ParsePool.wait` and write
their files as the current user, so the parent's logic runs anywhere. Real children run under the
pool's UIDs with the filter (the cases of T-190, T-205, T-206 and T-210 this host can run), which
needs root; the stand-in Python of `stub_python` plays a hostile or greedy child.
"""

from __future__ import annotations

import asyncio
import fcntl
import io
import json
import logging
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pikepdf
import pytest

from docchat_core.telemetry import TestExporters
from docchat_ingestion.isolation import convert, pdf_schema, runner
from docchat_ingestion.isolation.results import (
    DOCX,
    PDF,
    RENDER_MAX_PAGE_BYTES,
    ChildFileError,
    ChildRequest,
    Command,
    ExtractPartV1,
    InspectOk,
    PageRange,
)
from docchat_ingestion.isolation.runner import Layout, ParsePool, ParseSlot, Scratch

type PdfFactory = Callable[..., bytes]
type Behaviour = Callable[[Path, ChildRequest], tuple[str, int]]


# --- Pure helpers ------------------------------------------------------------------------------


def test_child_command_switches_user_limits_and_runs_the_worker() -> None:
    command = runner.child_command(Layout(), 10003, Path("/run/docchat-scratch/d/request.json"))

    assert command == [
        "setpriv",
        "--reuid=10003",
        "--regid=10003",
        "--clear-groups",
        "--no-new-privs",
        "--inh-caps=-all",
        "--",
        "prlimit",
        "--fsize=2147483648",
        "--nofile=256",
        "--nproc=64",
        "--",
        "/opt/venv/bin/python",
        "-I",
        "-m",
        "docchat_ingestion.isolation.parse_worker",
        "/run/docchat-scratch/d/request.json",
    ]


def test_a_child_with_the_convert_filter_names_it_after_its_request_path() -> None:
    request_path = Path("/run/docchat-scratch/d/request.json")
    default = runner.child_command(Layout(), 10003, request_path)

    assert runner.child_command(Layout(), 10003, request_path, "default") == default
    assert runner.child_command(Layout(), 10003, request_path, "convert") == [
        *default,
        "--filter=convert",
    ]


def test_only_a_convert_child_loads_the_convert_filter() -> None:
    assert {command: runner.filter_for(command) for command in Command} == {
        command: "convert" if command is Command.CONVERT else "default" for command in Command
    }


def test_t190_the_child_environment_is_an_allowlist() -> None:
    worker = {
        "PATH": "/opt/venv/bin:/usr/bin",
        "LANG": "C.UTF-8",
        "HF_HOME": "/opt/docchat/models/hf",
        "IDENTITY_ENDPOINT": "http://127.0.0.1:41000/msi/token",
        "IDENTITY_HEADER": "secret",
        "MSI_ENDPOINT": "x",
        "DOCCHAT_STORAGE_BLOB_ENDPOINT": "https://x.blob.core.windows.net/",
        "OMP_NUM_THREADS": "8",
    }

    environment = runner.child_environment(Path("/run/docchat-scratch/d"), worker)

    assert environment == {
        "PATH": "/opt/venv/bin:/usr/bin",
        "LANG": "C.UTF-8",
        "HF_HOME": "/opt/docchat/models/hf",
        "HOME": "/run/docchat-scratch/d",
        "TMPDIR": "/run/docchat-scratch/d",
        "TMP": "/run/docchat-scratch/d",
        "TEMP": "/run/docchat-scratch/d",
        "OMP_NUM_THREADS": "1",
    }


def test_the_layout_names_sixteen_users_and_their_locks() -> None:
    layout = Layout()

    assert list(layout.uids) == list(range(10002, 10018))
    assert layout.lock_path(10002) == Path("/run/docchat-parse/10002.lock")


def write_status(
    proc: Path, pid: int, uids: tuple[int, int, int], rss_kb: int | None, state: str = "S"
) -> None:
    (proc / str(pid)).mkdir()
    lines = [
        f"Name:\tchild{pid}",
        f"State:\t{state} (state)",
        f"Uid:\t{uids[0]}\t{uids[1]}\t{uids[2]}\t{uids[0]}",
    ]
    if rss_kb is not None:
        lines.append(f"VmRSS:\t{rss_kb} kB")
    (proc / str(pid) / "status").write_text("\n".join(lines) + "\n")


def test_the_proc_scan_counts_processes_and_sums_resident_memory(tmp_path: Path) -> None:
    write_status(tmp_path, 10, (10002, 10002, 10002), 1000)
    write_status(tmp_path, 11, (10002, 10002, 10002), 2000)
    write_status(tmp_path, 12, (0, 10002, 0), 5000)  # effective UID only: counted, not summed
    write_status(tmp_path, 13, (10003, 10003, 10003), 7000)
    write_status(tmp_path, 14, (10002, 10002, 10002), None)
    write_status(tmp_path, 15, (10002, 10002, 10002), None, state="Z")  # a zombie isn't counted
    (tmp_path / "self").mkdir()
    (tmp_path / "99").mkdir()  # vanished before its status was read

    assert runner.processes_of(tmp_path, 10002) == 4
    assert runner.resident_kb(tmp_path, 10002) == 3000
    assert runner.processes_of(tmp_path, 10004) == 0


def test_removal_takes_every_file_of_the_user_and_follows_no_link(tmp_path: Path) -> None:
    shared = tmp_path / "tmp"
    (shared / "other" / "deep").mkdir(parents=True)
    target = tmp_path / "kept.txt"
    target.write_text("kept")
    (shared / "left.txt").write_text("x")
    (shared / "dir").mkdir()
    (shared / "dir" / "inner").write_text("x")
    (shared / "link").symlink_to(target)
    (shared / "other" / "deep" / "file").write_text("x")
    uid = os.getuid()

    runner.remove_files_of((shared, tmp_path / "unmounted"), uid)

    assert target.read_text() == "kept"
    assert list(shared.iterdir()) == [] or all(
        path.stat().st_uid != uid for path in shared.iterdir()
    )


def test_a_scratch_directory_is_written_handed_over_read_and_removed(tmp_path: Path) -> None:
    tmp_path.chmod(0o711)
    directory = Scratch(Layout(scratch_parent=tmp_path))
    directory.write("source.bin", b"data")
    directory.make_directory("out")

    assert oct(directory.path.stat().st_mode & 0o777) == "0o700"
    assert oct((directory.path / "source.bin").stat().st_mode & 0o777) == "0o644"
    with pytest.raises(FileExistsError):
        directory.write("source.bin", b"again")
    with pytest.raises(ChildFileError):
        directory.read("source.bin", limit=10)
    with directory.open_own("source.bin") as own:
        assert own.read() == b"data"
    directory.hand_over(os.getuid(), ("out",))
    with pytest.raises(PermissionError):
        directory.write("late.bin", b"x")
    with pytest.raises(PermissionError):
        directory.open_own("source.bin")
    assert directory.read("source.bin", limit=10) == b"data"
    assert sorted(directory.names()) == ["out", "source.bin"]
    directory.delete("source.bin")
    directory.remove()
    assert not directory.path.exists()


# --- Stand-in children -------------------------------------------------------------------------


@dataclass
class StandIns:
    """Behaviours by command; each writes the child's files and returns (exit kind, exit code)."""

    behaviours: dict[Command, Behaviour] = field(default_factory=dict)
    requests: list[ChildRequest] = field(default_factory=list)
    filters: list[str] = field(default_factory=list)
    ended: list[int] = field(default_factory=list)


def ok_result(directory: Path, value: object) -> tuple[str, int]:
    """Write `result.json` and end as a child that exits 0."""
    (directory / "result.json").write_text(json.dumps(value))
    return "ok", 0


def render_child(pages: list[bytes]) -> Behaviour:
    def behave(directory: Path, request: ChildRequest) -> tuple[str, int]:
        for number, data in enumerate(pages, start=1):
            (directory / f"{number}.pdf").write_bytes(data)
        return ok_result(directory, {"ok": True, "pageCount": len(pages)})

    return behave


def verify_child(directory: Path, request: ChildRequest) -> tuple[str, int]:
    if request.mode == "pages" and request.pages is not None:
        for number in range(request.pages.first, request.pages.last + 1):
            data = (directory / f"{number}.pdf").read_bytes()
            (directory / f"{number}.verified.pdf").write_bytes(b"verified:" + data)
    else:
        data = (directory / "document.pdf").read_bytes()
        (directory / "document.verified.pdf").write_bytes(b"verified:" + data)
    return ok_result(directory, {"ok": True})


def assemble_child(directory: Path, request: ChildRequest) -> tuple[str, int]:
    count = request.parameters["pageCount"]
    assert isinstance(count, int)
    pages = [(directory / f"{number}.pdf").read_bytes() for number in range(1, count + 1)]
    (directory / "document.pdf").write_bytes(b"|".join(pages))
    return ok_result(directory, {"ok": True, "pageCount": count, "documentPdf": "built"})


@pytest.fixture
def stand_ins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ParsePool, StandIns]:
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o711)
    locks = tmp_path / "locks"
    locks.mkdir()
    uid = os.getuid()
    for each in (uid, uid + 1):
        (locks / f"{each}.lock").write_text("")
    layout = Layout(
        scratch_parent=scratch, lock_directory=locks, world_writable=(), first_uid=uid, users=2
    )
    record = StandIns()

    async def start(
        self: ParsePool, child_uid: int, directory: Scratch, filter_name: str = "default"
    ) -> SimpleNamespace:
        request = ChildRequest.model_validate_json((directory.path / "request.json").read_bytes())
        record.requests.append(request)
        record.filters.append(filter_name)
        kind, code = record.behaviours[request.command](directory.path, request)
        return SimpleNamespace(returncode=code, kind=kind, started=0.0)

    async def wait(
        self: ParsePool, child: SimpleNamespace, child_uid: int, limit: float, memory: int
    ) -> tuple[str, int, int]:
        return child.kind, 1, 3

    async def end(self: ParsePool, child_uid: int) -> bool:
        record.ended.append(child_uid)
        return True

    async def clear(self: ParsePool, child_uid: int, *, remove_files: bool) -> bool:
        return True

    hand_over = Scratch.hand_over

    def own_hand_over(self: Scratch, child_uid: int, subdirectories: tuple[str, ...] = ()) -> None:
        # Stand-in children write as the current user, so every directory is handed to it.
        hand_over(self, uid, subdirectories)

    monkeypatch.setattr(Scratch, "hand_over", own_hand_over)
    monkeypatch.setattr(ParsePool, "start", start)
    monkeypatch.setattr(ParsePool, "wait", wait)
    monkeypatch.setattr(ParsePool, "end_processes", end)
    monkeypatch.setattr(ParsePool, "clear", clear)
    pool = ParsePool(
        slots_per_process=1, memory_limit_mb=2048, parse_timeout_seconds=1200, layout=layout
    )
    return pool, record


class Sink:
    def __init__(self) -> None:
        self.pages: dict[int, bytes] = {}
        self.document: bytes | None = None

    async def upload_page(self, number: int, data: bytes) -> None:
        self.pages[number] = data

    async def upload_document(self, data: bytes) -> None:
        self.document = data


async def test_inspect_validates_the_childs_result(stand_ins: tuple[ParsePool, StandIns]) -> None:
    pool, record = stand_ins
    record.behaviours[Command.INSPECT] = lambda d, r: ok_result(d, {"ok": True, "pageCount": 7})

    async with pool.slot(120) as slot:
        directory = slot.scratch()
        directory.write("source.bin", b"%PDF-1.7")
        outcome = await slot.inspect(directory, PDF, max_pages=2000)

    assert outcome.ok
    assert outcome.result == InspectOk(ok=True, pageCount=7)
    assert record.requests[0].limits.max_pages == 2000
    assert list(pool.layout.scratch_parent.iterdir()) == []


async def test_t147_a_failure_after_writing_the_source_leaves_no_scratch_directory(
    stand_ins: tuple[ParsePool, StandIns],
) -> None:
    pool, record = stand_ins
    written: list[Path] = []

    async def fail_after_the_source() -> None:
        async with pool.slot(120) as slot:
            directory = slot.scratch()
            directory.write("source.bin", b"%PDF-1.7")
            written.append(directory.path)
            raise RuntimeError("forced")

    with pytest.raises(RuntimeError, match="forced"):
        await fail_after_the_source()

    assert written
    assert written[0].parent == pool.layout.scratch_parent
    assert not written[0].exists()
    assert list(pool.layout.scratch_parent.iterdir()) == []
    assert record.requests == []


@pytest.mark.parametrize(
    ("behaviour", "code", "exit_kind"),
    [
        (lambda d, r: ("timeout", -9), "parse_timeout", "timeout"),
        (lambda d, r: ("memory", -9), "parse_memory_exceeded", "memory"),
        (lambda d, r: ("error", 4), "parse_failed", "error"),
        (lambda d, r: ("ok", 0), "parse_failed", "error"),
        (
            lambda d, r: ok_result(d, {"ok": True, "pageCount": 1, "x": 1}),
            "parse_failed",
            "error",
        ),
        (
            lambda d, r: ok_result(d, {"ok": False, "code": "corrupt_document"}),
            "corrupt_document",
            "ok",
        ),
    ],
)
async def test_each_ending_of_a_child_maps_to_its_code(
    stand_ins: tuple[ParsePool, StandIns], behaviour: Behaviour, code: str, exit_kind: str
) -> None:
    pool, record = stand_ins
    record.behaviours[Command.INSPECT] = behaviour

    async with pool.slot(120) as slot:
        outcome = await slot.inspect(slot.scratch(), PDF, max_pages=2000)

    assert (outcome.code, outcome.exit) == (code, exit_kind)
    assert outcome.result is None


async def test_the_parse_span_and_histogram_carry_metadata_only(
    stand_ins: tuple[ParsePool, StandIns], telemetry: TestExporters
) -> None:
    pool, record = stand_ins
    record.behaviours[Command.INSPECT] = lambda d, r: ok_result(d, {"ok": True, "pageCount": 1})

    async with pool.slot(120) as slot:
        await slot.inspect(slot.scratch(), PDF, max_pages=2000)

    (span,) = [
        span
        for span in telemetry.spans.get_finished_spans()
        if span.name == "docchat.ingestion.parse"
    ]
    assert {k: v for k, v in (span.attributes or {}).items() if not k.startswith("_MS.")} == {
        "docchat.ingestion.parse_command": "inspect",
        "docchat.ingestion.content_type": PDF,
        "docchat.ingestion.parse_peak_rss_mb": 3,
        "docchat.ingestion.parse_exit": "ok",
    }


async def test_a_second_child_under_one_user_ends_the_users_processes_first(
    stand_ins: tuple[ParsePool, StandIns],
) -> None:
    pool, record = stand_ins
    record.behaviours[Command.INSPECT] = lambda d, r: ok_result(d, {"ok": True, "pageCount": 1})

    async with pool.slot(120) as slot:
        await slot.inspect(slot.scratch(), PDF, max_pages=2000)
        before = list(record.ended)
        await slot.inspect(slot.scratch(), PDF, max_pages=2000)

    assert record.ended[len(before)] == slot.first_uid


async def test_convert_writes_the_profile_and_reads_the_converted_pdf(
    stand_ins: tuple[ParsePool, StandIns],
) -> None:
    pool, record = stand_ins
    seen: dict[str, object] = {}

    def behave(directory: Path, request: ChildRequest) -> tuple[str, int]:
        seen["profile"] = (directory / "lo-profile/user/registrymodifications.xcu").read_bytes()
        seen["mode"] = oct((directory / "lo-profile").stat().st_mode & 0o777)
        (directory / "converted.pdf").write_bytes(b"%PDF-converted")
        ok_result(directory, {"ok": True})
        return "ok", 0

    record.behaviours[Command.CONVERT] = behave

    async with pool.slot(120) as slot:
        outcome, data = await slot.convert(slot.scratch(), DOCX, limit_seconds=900, max_pages=2000)

    assert outcome.ok
    assert data == b"%PDF-converted"
    assert seen["profile"] == convert.PROFILE
    assert seen["mode"] == "0o700"
    assert record.requests[0].content_type == DOCX
    assert record.filters == ["convert"]


async def test_the_convert_child_alone_starts_with_the_convert_filter(
    stand_ins: tuple[ParsePool, StandIns],
) -> None:
    pool, record = stand_ins
    record.behaviours[Command.INSPECT] = lambda d, r: ok_result(d, {"ok": True, "pageCount": 1})

    def converted(directory: Path, request: ChildRequest) -> tuple[str, int]:
        (directory / "converted.pdf").write_bytes(b"%PDF-converted")
        return ok_result(directory, {"ok": True})

    record.behaviours[Command.CONVERT] = converted

    async with pool.slot(120) as slot:
        await slot.inspect(slot.scratch(), PDF, max_pages=2000)
        await slot.convert(slot.scratch(), DOCX, limit_seconds=900, max_pages=2000)
        await slot.inspect(slot.scratch(), PDF, max_pages=2000)

    assert [request.command for request in record.requests] == [
        Command.INSPECT,
        Command.CONVERT,
        Command.INSPECT,
    ]
    assert record.filters == ["default", "convert", "default"]


async def test_t612_a_converted_pdf_that_fails_the_parents_checks_is_convert_failed(
    stand_ins: tuple[ParsePool, StandIns],
) -> None:
    pool, record = stand_ins

    def behave(directory: Path, request: ChildRequest) -> tuple[str, int]:
        (directory / "converted.pdf").symlink_to("/etc/hostname")
        ok_result(directory, {"ok": True})
        return "ok", 0

    record.behaviours[Command.CONVERT] = behave

    async with pool.slot(120) as slot:
        outcome, data = await slot.convert(slot.scratch(), DOCX, limit_seconds=900, max_pages=2000)

    assert (outcome.code, data) == ("convert_failed", None)


async def test_partition_returns_the_validated_bytes(stand_ins: tuple[ParsePool, StandIns]) -> None:
    pool, record = stand_ins
    part = {
        "formatVersion": 1,
        "method": "pagewise",
        "pageCount": 9,
        "firstPage": 1,
        "lastPage": 2,
        "pages": [],
    }

    def behave(directory: Path, request: ChildRequest) -> tuple[str, int]:
        (directory / "partition.json").write_text(json.dumps(part))
        ok_result(directory, {"ok": True})
        return "ok", 0

    record.behaviours[Command.PARTITION] = behave

    async with pool.slot(120) as slot:
        outcome, raw, parsed = await slot.partition(
            slot.scratch(),
            pages=PageRange(first=1, last=2),
            page_count=9,
            parameters={"summarizeTables": True},
            limit_seconds=1200,
            max_pages=2000,
        )

    assert outcome.ok
    assert raw == json.dumps(part).encode()
    assert isinstance(parsed, ExtractPartV1)
    assert record.requests[0].parameters == {"summarizeTables": True, "pageCount": 9}
    part["pageCount"] = 8
    async with pool.slot(120) as slot:
        outcome, raw, parsed = await slot.partition(
            slot.scratch(),
            pages=PageRange(first=1, last=2),
            page_count=9,
            parameters={},
            limit_seconds=1200,
            max_pages=2000,
        )
    assert (outcome.code, raw, parsed) == ("parse_failed", None, None)


async def test_render_verifies_uploads_and_assembles(stand_ins: tuple[ParsePool, StandIns]) -> None:
    pool, record = stand_ins
    pages = [f"page {number}".encode() for number in range(1, 7)]
    record.behaviours.update(
        {
            Command.RENDER: render_child(pages),
            Command.VERIFY_RENDER: verify_child,
            Command.ASSEMBLE: assemble_child,
        }
    )
    sink = Sink()

    async with pool.slot(1200) as slot:
        directory = slot.scratch()
        directory.write("source.bin", b"%PDF")
        outcome = await slot.render(directory, page_count=6, pdf_size=4, max_pages=2000, sink=sink)

    assert outcome == runner.RenditionOutcome(None, 6, "built", (1, 2, 3, 4, 5, 6))
    assert sink.pages == {number: b"verified:" + pages[number - 1] for number in range(1, 7)}
    assert sink.document == b"verified:" + b"|".join(b"verified:" + page for page in pages)
    batches = [
        r.pages for r in record.requests if r.command is Command.VERIFY_RENDER and r.mode == "pages"
    ]
    assert batches == [PageRange(first=1, last=4), PageRange(first=5, last=6)]
    assert list(pool.layout.scratch_parent.iterdir()) == []


async def test_render_fails_when_a_page_or_the_verifier_fails(
    stand_ins: tuple[ParsePool, StandIns],
) -> None:
    pool, record = stand_ins

    def hostile(directory: Path, request: ChildRequest) -> tuple[str, int]:
        (directory / "1.pdf").symlink_to("/etc/hostname")
        ok_result(directory, {"ok": True, "pageCount": 1})
        return "ok", 0

    record.behaviours.update({Command.RENDER: hostile, Command.VERIFY_RENDER: verify_child})
    sink = Sink()

    async with pool.slot(1200) as slot:
        outcome = await slot.render(
            slot.scratch(), page_count=1, pdf_size=4, max_pages=2000, sink=sink
        )

    assert outcome.code == "render_failed"
    assert sink.pages == {}

    record.behaviours[Command.RENDER] = render_child([b"p"])
    record.behaviours[Command.VERIFY_RENDER] = lambda d, r: ok_result(
        d, {"ok": False, "code": "render_failed"}
    )
    async with pool.slot(1200) as slot:
        outcome = await slot.render(
            slot.scratch(), page_count=1, pdf_size=4, max_pages=2000, sink=sink
        )
    assert outcome.code == "render_failed"
    assert sink.pages == {}


async def test_render_maps_the_render_childs_limits_and_a_missing_page(
    stand_ins: tuple[ParsePool, StandIns],
) -> None:
    pool, record = stand_ins
    record.behaviours[Command.VERIFY_RENDER] = verify_child
    sink = Sink()

    record.behaviours[Command.RENDER] = lambda d, r: ("timeout", -9)
    async with pool.slot(1200) as slot:
        assert (
            await slot.render(slot.scratch(), page_count=1, pdf_size=1, max_pages=9, sink=sink)
        ).code == "parse_timeout"
    record.behaviours[Command.RENDER] = lambda d, r: ok_result(
        d, {"ok": False, "code": "render_too_large"}
    )
    async with pool.slot(1200) as slot:
        assert (
            await slot.render(slot.scratch(), page_count=1, pdf_size=1, max_pages=9, sink=sink)
        ).code == "render_too_large"
    record.behaviours[Command.RENDER] = render_child([b"only one"])
    record.behaviours[Command.ASSEMBLE] = assemble_child
    async with pool.slot(1200) as slot:
        # The child claims two pages but wrote one: its result fails validation.
        assert (
            await slot.render(slot.scratch(), page_count=2, pdf_size=1, max_pages=9, sink=sink)
        ).code == "render_failed"


async def test_t614_the_whole_document_is_skipped_above_its_cap(
    stand_ins: tuple[ParsePool, StandIns], monkeypatch: pytest.MonkeyPatch
) -> None:
    pool, record = stand_ins
    monkeypatch.setattr(runner, "RENDER_MAX_DOCUMENT_BYTES", 20)
    record.behaviours.update(
        {
            Command.RENDER: render_child([b"x" * 8, b"y" * 8, b"z" * 8]),
            Command.VERIFY_RENDER: verify_child,
        }
    )
    sink = Sink()

    async with pool.slot(1200) as slot:
        outcome = await slot.render(
            slot.scratch(), page_count=3, pdf_size=1, max_pages=9, sink=sink
        )

    assert outcome.document_pdf == "too_large"
    assert sorted(sink.pages) == [1, 2, 3]
    assert sink.document is None
    assert not any(r.command is Command.ASSEMBLE for r in record.requests)


async def test_t614_an_assemble_too_large_result_skips_and_a_failure_fails(
    stand_ins: tuple[ParsePool, StandIns],
) -> None:
    pool, record = stand_ins
    record.behaviours.update(
        {Command.RENDER: render_child([b"a"]), Command.VERIFY_RENDER: verify_child}
    )
    sink = Sink()

    record.behaviours[Command.ASSEMBLE] = lambda d, r: ok_result(
        d, {"ok": True, "pageCount": 1, "documentPdf": "too_large"}
    )
    async with pool.slot(1200) as slot:
        assert (
            await slot.render(slot.scratch(), page_count=1, pdf_size=1, max_pages=9, sink=sink)
        ).document_pdf == "too_large"
    record.behaviours[Command.ASSEMBLE] = lambda d, r: ("timeout", -9)
    async with pool.slot(1200) as slot:
        assert (
            await slot.render(slot.scratch(), page_count=1, pdf_size=1, max_pages=9, sink=sink)
        ).code == "render_failed"
    record.behaviours[Command.ASSEMBLE] = assemble_child
    record.behaviours[Command.VERIFY_RENDER] = lambda d, r: (
        verify_child(d, r) if r.mode == "pages" else ("error", 1)
    )
    async with pool.slot(1200) as slot:
        assert (
            await slot.render(slot.scratch(), page_count=1, pdf_size=1, max_pages=9, sink=sink)
        ).code == "render_failed"


async def test_the_probe_passes_only_on_a_full_result(
    stand_ins: tuple[ParsePool, StandIns],
) -> None:
    pool, record = stand_ins
    checks = dict.fromkeys(
        (
            "filterInstalled",
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
    record.behaviours[Command.PROBE] = lambda d, r: ok_result(d, {"ok": True, **checks})

    assert await pool.probe()
    record.behaviours[Command.PROBE] = lambda d, r: ("error", 4)
    assert not await pool.probe()
    assert record.filters == ["default", "default"]


SHARED_PROBE_CHECKS = (
    "filterInstalled",
    "inetSocketRefused",
    "ioUringRefused",
    "tracemeRefused",
    "vmReadRefused",
    "clone3Refused",
    "threadFiltered",
)
DEFAULT_PROBE_CHECKS = dict.fromkeys(
    (*SHARED_PROBE_CHECKS, "unixSocketRefused", "socketpairRefused"), True
)
CONVERT_PROBE_CHECKS = dict.fromkeys(
    (*SHARED_PROBE_CHECKS, "inet6SocketRefused", "netlinkSocketRefused"), True
)


async def test_the_convert_mode_probe_runs_under_the_convert_filter_and_needs_its_own_result(
    stand_ins: tuple[ParsePool, StandIns],
) -> None:
    pool, record = stand_ins
    convert_result = {"ok": True, **CONVERT_PROBE_CHECKS}
    record.behaviours[Command.PROBE] = lambda d, r: ok_result(d, convert_result)

    assert await pool.probe("convert")
    assert record.filters == ["convert"]
    # Each probe's result is validated with its own model: neither passes for the other.
    assert not await pool.probe("default")
    record.behaviours[Command.PROBE] = lambda d, r: ok_result(
        d, {"ok": True, **DEFAULT_PROBE_CHECKS}
    )
    assert await pool.probe("default")
    assert not await pool.probe("convert")
    record.behaviours[Command.PROBE] = lambda d, r: ok_result(
        d, {**convert_result, "netlinkSocketRefused": False}
    )
    assert not await pool.probe("convert")
    record.behaviours[Command.PROBE] = lambda d, r: ok_result(
        d, {"ok": False, "code": "parse_isolation_failed"}
    )
    assert not await pool.probe("convert")


async def test_users_are_taken_in_uid_order_and_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    locks = tmp_path / "locks"
    locks.mkdir()
    for uid in range(10002, 10006):
        (locks / f"{uid}.lock").write_text("")
    cleared: list[tuple[int, bool]] = []

    async def clear(self: ParsePool, uid: int, *, remove_files: bool) -> bool:
        cleared.append((uid, remove_files))
        return uid != 10003

    monkeypatch.setattr(ParsePool, "clear", clear)
    pool = ParsePool(
        slots_per_process=2,
        memory_limit_mb=2048,
        parse_timeout_seconds=1200,
        layout=Layout(lock_directory=locks, scratch_parent=tmp_path, users=4),
    )
    held = os.open(locks / "10002.lock", os.O_RDWR)
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        async with pool.slot(1) as slot:
            # 10002 is held elsewhere; 10003 is quarantined at take and keeps its lock.
            assert (slot.first_uid, slot.second_uid) == (10004, 10005)
    finally:
        os.close(held)

    assert cleared[:3] == [(10003, True), (10004, True), (10005, True)]
    assert cleared[3:] == [(10004, True), (10005, True)]
    assert 10003 in pool._locks


async def test_t205_with_no_two_free_users_the_wait_ends_in_parse_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    locks = tmp_path / "locks"
    locks.mkdir()
    (locks / "10002.lock").write_text("")
    sleeps: list[float] = []
    now = [0.0]

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds
        # Each of the 240 rounds tries 16 lock files; yielding keeps one loop step short.
        await asyncio.sleep(0)

    async def clear(self: ParsePool, uid: int, *, remove_files: bool) -> bool:
        return True

    monkeypatch.setattr(ParsePool, "clear", clear)
    pool = ParsePool(
        slots_per_process=1,
        memory_limit_mb=2048,
        parse_timeout_seconds=1200,
        layout=Layout(lock_directory=locks, scratch_parent=tmp_path),
        clock=lambda: now[0],
        sleep=sleep,
    )

    with pytest.raises(runner.ParseUsersUnavailableError):
        async with pool.slot(runner.INSPECT_TIMEOUT_SECONDS):
            pass

    assert set(sleeps) == {0.5}
    assert len(sleeps) == 240
    assert pool._locks == {}


async def test_t205_a_user_whose_process_survives_ten_rounds_is_quarantined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    killed: list[list[str]] = []

    class Killer:
        async def wait(self) -> int:
            return 0

    async def exec_(*argv: str, **_kwargs: object) -> Killer:
        killed.append(list(argv))
        return Killer()

    async def sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_)
    monkeypatch.setattr(runner, "processes_of", lambda proc, uid: 1)
    pool = ParsePool(
        slots_per_process=1,
        memory_limit_mb=2048,
        parse_timeout_seconds=1200,
        layout=Layout(lock_directory=tmp_path, scratch_parent=tmp_path),
        sleep=sleep,
    )

    with caplog.at_level(logging.WARNING, logger="docchat"):
        assert not await pool.clear(10007, remove_files=True)

    assert len(killed) == 10
    assert killed[0] == [
        "setpriv",
        "--reuid=10007",
        "--regid=10007",
        "--clear-groups",
        "--",
        "kill",
        "-KILL",
        "-1",
    ]
    assert pool.quarantined == {10007}
    assert [record.message for record in caplog.records] == ["parse_uid_quarantined"]


async def test_clearing_removes_the_users_files_when_no_process_is_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    removed: list[tuple[tuple[Path, ...], int]] = []

    async def end(self: ParsePool, uid: int) -> bool:
        return True

    monkeypatch.setattr(ParsePool, "end_processes", end)
    monkeypatch.setattr(runner, "remove_files_of", lambda roots, uid: removed.append((roots, uid)))
    shared = tmp_path / "tmp"
    pool = ParsePool(
        slots_per_process=1,
        memory_limit_mb=2048,
        parse_timeout_seconds=1200,
        layout=Layout(lock_directory=tmp_path, scratch_parent=tmp_path, world_writable=(shared,)),
    )

    assert await pool.clear(10002, remove_files=True)
    assert await pool.clear(10002, remove_files=False)
    assert removed == [((shared, tmp_path), 10002)]


# --- Real children (root, as in the image) -----------------------------------------------------


def real_pool(layout: Layout, memory_limit_mb: int = 2048) -> ParsePool:
    return ParsePool(
        slots_per_process=8,
        memory_limit_mb=memory_limit_mb,
        parse_timeout_seconds=1200,
        layout=layout,
    )


async def test_t206_the_isolation_probe_passes_under_a_parse_user(parse_layout: Layout) -> None:
    assert await real_pool(parse_layout).probe()


async def test_t206_the_convert_mode_probe_passes_under_a_parse_user(parse_layout: Layout) -> None:
    assert await real_pool(parse_layout).probe("convert")
    assert list(parse_layout.scratch_parent.iterdir()) == []


async def test_a_real_inspect_and_render_under_two_parse_users(
    parse_layout: Layout, make_pdf: PdfFactory
) -> None:
    pool = real_pool(parse_layout)
    data = make_pdf(pages=5)
    sink = Sink()

    async with pool.slot(120) as slot:
        assert slot.first_uid != slot.second_uid
        assert {slot.first_uid, slot.second_uid} <= set(range(10002, 10018))
        inspect_directory = slot.scratch()
        inspect_directory.write("source.bin", data)
        inspected = await slot.inspect(inspect_directory, PDF, max_pages=2000)
        render_directory = slot.scratch()
        render_directory.write("source.bin", data)
        rendered = await slot.render(
            render_directory, page_count=5, pdf_size=len(data), max_pages=2000, sink=sink
        )

    assert inspected.result == InspectOk(ok=True, pageCount=5)
    assert rendered == runner.RenditionOutcome(None, 5, "built", (1, 2, 3, 4, 5))
    for written in (*sink.pages.values(), sink.document):
        assert written is not None
        with pikepdf.open(io.BytesIO(written)) as pdf:
            assert set(pdf.Root.keys()) == {"/Type", "/Pages"}
    assert list(parse_layout.scratch_parent.iterdir()) == []


async def test_t205_eight_slots_hold_sixteen_distinct_users(parse_layout: Layout) -> None:
    pool = real_pool(parse_layout)
    held: list[ParseSlot] = []
    ready = asyncio.Event()

    async def take() -> None:
        async with pool.slot(60) as slot:
            held.append(slot)
            if len(held) == 8:
                ready.set()
            await ready.wait()

    await asyncio.wait_for(asyncio.gather(*(take() for _ in range(8))), 60)

    uids = [uid for slot in held for uid in (slot.first_uid, slot.second_uid)]
    assert sorted(uids) == list(range(10002, 10018))


async def test_t190_a_child_runs_as_its_parse_user_with_no_groups_and_no_identity(
    stub_python: Callable[[str], Layout],
) -> None:
    layout = stub_python(
        "{ id -u; id -g; id -G; env; cat /proc/1/environ 2>&1 || echo DENIED; "
        "touch /run/x 2>&1 || echo NOWRITE; } > out.txt; "
        'echo \'{"ok": true, "pageCount": 1}\' > result.json'
    )
    pool = real_pool(layout)
    worker = {
        **os.environ,
        "IDENTITY_ENDPOINT": "http://127.0.0.1:1/x",
        "IDENTITY_HEADER": "secret",
    }
    pool.environment = worker

    async with pool.slot(120) as slot:
        directory = slot.scratch()
        outcome = await slot.inspect(directory, PDF, max_pages=2000)
        report = (directory.path / "out.txt").read_text().splitlines()

    assert outcome.ok
    uid = str(slot.first_uid)
    assert report[:3] == [uid, uid, uid]
    assert not any(line.startswith(("IDENTITY_", "MSI_")) for line in report)
    assert f"TMPDIR={directory.path}" in report
    assert f"HOME={directory.path}" in report
    assert "DENIED" in report or any("Permission denied" in line for line in report)


async def test_t190_memory_above_the_limit_ends_every_process_of_the_user(
    stub_python: Callable[[str], Layout],
) -> None:
    python = shutil.which("python3") or "python3"
    layout = stub_python(
        f"{python} -c 'import time; x = bytearray(160 * 1024 * 1024); "
        'x[::4096] = b"x" * len(x[::4096]); time.sleep(30)\' & wait'
    )
    pool = real_pool(layout, memory_limit_mb=64)

    async with pool.slot(120) as slot:
        outcome = await slot.inspect(slot.scratch(), PDF, max_pages=2000)
        assert runner.processes_of(Path("/proc"), slot.first_uid) == 0

    assert (outcome.exit, outcome.code) == ("memory", "parse_memory_exceeded")
    assert outcome.peak_rss_mb > 64


async def test_t190_a_child_past_its_wall_clock_is_ended(
    stub_python: Callable[[str], Layout], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "INSPECT_TIMEOUT_SECONDS", 1)
    pool = real_pool(stub_python("setsid sleep 30 & sleep 30"))

    async with pool.slot(1) as slot:
        outcome = await slot.inspect(slot.scratch(), PDF, max_pages=2000)
        assert runner.processes_of(Path("/proc"), slot.first_uid) == 0

    assert (outcome.exit, outcome.code) == ("timeout", "parse_timeout")


@pytest.mark.parametrize(
    "script",
    [
        "ln -s /proc/1/environ result.json",
        "mkfifo result.json",
        'echo \'{"ok": true, "pageCount": 1}\' > r && ln r result.json',
        'echo \'{"ok": true, "pageCount": 1}\' > result.json; exit 3',
    ],
)
async def test_t210_a_result_in_a_forbidden_form_fails_the_item(
    stub_python: Callable[[str], Layout], script: str
) -> None:
    pool = real_pool(stub_python(script))

    async with pool.slot(120) as slot:
        outcome = await asyncio.wait_for(slot.inspect(slot.scratch(), PDF, max_pages=2000), 30)

    assert outcome.code == "parse_failed"


async def test_t210_a_render_page_that_is_a_link_uploads_nothing(
    stub_python: Callable[[str], Layout], make_pdf: PdfFactory
) -> None:
    pool = real_pool(
        stub_python(
            'ln -s /proc/1/environ 1.pdf; echo \'{"ok": true, "pageCount": 1}\' > result.json'
        )
    )
    sink = Sink()

    async with pool.slot(120) as slot:
        outcome = await slot.render(
            slot.scratch(), page_count=1, pdf_size=10, max_pages=2000, sink=sink
        )

    assert outcome.code == "render_failed"
    assert sink.pages == {}


async def test_t205_what_a_user_left_is_gone_before_its_lock_is_released(
    parse_layout: Layout, stub_python: Callable[[str], Layout]
) -> None:
    layout = stub_python(
        f"setsid sleep 60 & touch {parse_layout.world_writable[0]}/left-behind; "
        'echo \'{"ok": true, "pageCount": 1}\' > result.json'
    )
    pool = real_pool(layout)

    async with pool.slot(120) as slot:
        await slot.inspect(slot.scratch(), PDF, max_pages=2000)
        assert (layout.world_writable[0] / "left-behind").exists()
        uid = slot.first_uid

    assert runner.processes_of(Path("/proc"), uid) == 0
    assert not (layout.world_writable[0] / "left-behind").exists()


def test_pdf_schema_and_runner_agree_on_the_page_cap() -> None:
    assert RENDER_MAX_PAGE_BYTES == 20_971_520
    assert pdf_schema.PAGE_CONTAINER_CAP == 100_000


def pinned_converter_installed() -> bool:
    if shutil.which("dpkg-query") is None:
        return False
    version = subprocess.run(
        ["dpkg-query", "-W", "-f=${Version}", "libreoffice-core-nogui"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return version == convert.LIBREOFFICE_PACKAGE_VERSION


@pytest.mark.skipif(
    not pinned_converter_installed(),
    reason=(
        "the converter runs under the convert child's filter in the pinned image "
        "(14-ingestion.md section 15.11 item 7.3; the converter isolation test, T-612); this "
        "host doesn't hold the pinned libreoffice-core-nogui"
    ),
)
async def test_t612_the_pinned_converter_converts_under_a_parse_user_and_the_filter(
    parse_layout: Layout, make_package: Callable[..., bytes]
) -> None:
    pool = real_pool(parse_layout)

    async with pool.slot(120) as slot:
        directory = slot.scratch()
        directory.write("source.bin", make_package("docx", text="CONVERTED UNDER THE FILTER"))
        outcome, data = await slot.convert(directory, DOCX, limit_seconds=300, max_pages=2000)

    assert outcome.ok
    assert data is not None
    assert data.startswith(b"%PDF-")
