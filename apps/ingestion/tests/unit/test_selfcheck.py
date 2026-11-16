"""`docchat_ingestion.selfcheck`: the image's offline self-check and the isolation probe
(`14-ingestion.md` section 15.5; T-613's version check). The pool, the parsing children and the
packages are stand-ins here; the real probe runs in the runner's tests and in the image build."""

from __future__ import annotations

import grp
import os
import pwd
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from docchat_ingestion import selfcheck
from docchat_ingestion.isolation import convert
from docchat_ingestion.isolation.results import (
    DOCX,
    PDF,
    PPTX,
    Command,
    ExtractPartV1,
    InspectOk,
    PageRange,
)
from docchat_ingestion.isolation.runner import ChildOutcome, Layout

FIXTURES = Path(__file__).resolve().parents[2] / "image" / "selfcheck"


def completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def test_t613_the_installed_converter_must_be_the_pinned_one(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return completed(convert.LIBREOFFICE_PACKAGE_VERSION)

    assert selfcheck.check_converter(run, tmp_path) == []
    assert calls == [["dpkg-query", "-W", "-f=${Version}", "libreoffice-core-nogui"]]
    assert selfcheck.check_converter(lambda *a, **k: completed("4:7.4.7-1+deb12u15"), tmp_path) == [
        "libreoffice_version"
    ]
    assert selfcheck.check_converter(lambda *a, **k: completed("", 1), tmp_path) == [
        "libreoffice_version"
    ]
    (tmp_path / "libupdchklo.so").write_bytes(b"")
    assert selfcheck.check_converter(run, tmp_path) == ["libreoffice_update_check"]


def test_the_encodings_and_the_odbc_driver_are_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    loaded: list[str] = []
    monkeypatch.setitem(sys.modules, "tiktoken", SimpleNamespace(get_encoding=loaded.append))
    commands: list[list[str]] = []

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(argv)
        return completed("", 0 if len(commands) == 1 else 1)

    assert selfcheck.check_encodings()
    assert loaded == ["cl100k_base", "o200k_base"]
    assert selfcheck.check_odbc(run)
    assert not selfcheck.check_odbc(run)
    assert commands[0] == [
        sys.executable,
        "-I",
        "-c",
        "import docchat_core, pyodbc; pyodbc.drivers().index('ODBC Driver 18 for SQL Server')",
    ]


def test_the_pool_layout_is_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    locks = tmp_path / "locks"
    locks.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o711)
    scratch.chmod(0o711)
    for uid in range(10002, 10018):
        (locks / f"{uid}.lock").write_text("")
        (locks / f"{uid}.lock").chmod(0o600)
    users = {
        f"docchat-parse-{index}": SimpleNamespace(
            pw_uid=10002 + index, pw_gid=10002 + index, gr_gid=10002 + index
        )
        for index in range(16)
    }
    monkeypatch.setattr(pwd, "getpwnam", lambda name: users[name])
    monkeypatch.setattr(grp, "getgrnam", lambda name: users[name])
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    layout = Layout(lock_directory=locks, scratch_parent=scratch)
    owner_is_root = os.getuid() == 0

    failures = selfcheck.check_pool_layout(layout)

    if owner_is_root:
        assert failures == []
    (locks / "10007.lock").chmod(0o644)
    del users["docchat-parse-3"]
    users["docchat-parse-4"] = SimpleNamespace(pw_uid=1, pw_gid=1, gr_gid=1)
    scratch.chmod(0o755)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    failures = selfcheck.check_pool_layout(layout)
    assert {
        "uid_0",
        "parse_user:docchat-parse-3",
        "parse_user:docchat-parse-4",
        "parse_lock:10007",
        "scratch_parent",
    } <= set(failures)


def outcome(command: Command, result: object = None, code: str | None = None) -> ChildOutcome:
    return ChildOutcome(command, "ok" if code is None else "error", code, result, 1, 1)  # type: ignore[arg-type]


def part(text: str, table: bool = False) -> ExtractPartV1:
    elements = [{"kind": "text", "text": text, "tableHtml": None, "headingLevel": None}]
    if table:
        elements.append(
            {"kind": "table", "text": "", "tableHtml": "<table></table>", "headingLevel": None}
        )
    return ExtractPartV1.model_validate(
        {
            "formatVersion": 1,
            "method": "pagewise",
            "pageCount": 1,
            "firstPage": 1,
            "lastPage": 1,
            "pages": [{"pageNumber": 1, "elements": elements}],
        }
    )


@dataclass
class StandInSlot:
    """A slot whose children answer from tables; every call is recorded."""

    converted: dict[str, bytes | None] = field(default_factory=dict)
    parts: dict[bytes, ExtractPartV1 | None] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    _sources: dict[int, bytes] = field(default_factory=dict)

    def scratch(self) -> SimpleNamespace:
        directory = SimpleNamespace()

        def write(name: str, data: bytes) -> None:
            self._sources[id(directory)] = data

        directory.write = write
        return directory

    async def remove(self, directory: object) -> None:
        return None

    async def inspect(
        self, directory: object, content_type: str, *, max_pages: int
    ) -> ChildOutcome:
        self.calls.append(f"inspect {content_type}")
        source = self._sources[id(directory)]
        pages = None if content_type in (DOCX, PPTX) else 1
        if source.startswith(b"BROKEN"):
            return outcome(Command.INSPECT, code="corrupt_document")
        return outcome(Command.INSPECT, InspectOk(ok=True, pageCount=pages))

    async def convert(
        self, directory: object, content_type: str, **_kwargs: object
    ) -> tuple[ChildOutcome, bytes | None]:
        self.calls.append(f"convert {content_type}")
        data = self.converted.get(content_type)
        if data is None:
            return outcome(Command.CONVERT, code="convert_failed"), None
        return outcome(Command.CONVERT, SimpleNamespace(ok=True)), data

    async def partition(
        self, directory: object, *, pages: PageRange, page_count: int, **_kwargs: object
    ) -> tuple[ChildOutcome, bytes | None, ExtractPartV1 | None]:
        self.calls.append("partition")
        parsed = self.parts.get(self._sources[id(directory)])
        if parsed is None:
            return outcome(Command.PARTITION, code="parse_failed"), None, None
        return outcome(Command.PARTITION, SimpleNamespace(ok=True)), b"{}", parsed


class StandInPool:
    """A pool whose probes answer from `probe`, one value for both or one per filter; each probe
    records the filter it was started with."""

    def __init__(self, slot: StandInSlot, probe: bool | dict[str, bool] = True) -> None:
        self.layout = Layout()
        self._slot = slot
        self._probe = probe
        self.probed: list[str] = []

    @asynccontextmanager
    async def slot(self, first_timeout: float) -> AsyncIterator[StandInSlot]:
        yield self._slot

    async def probe(self, filter_name: str = "default") -> bool:
        self.probed.append(filter_name)
        return self._probe if isinstance(self._probe, bool) else self._probe[filter_name]


def fixtures(tmp_path: Path) -> Path:
    (tmp_path / "selfcheck.docx").write_bytes(b"DOCX")
    (tmp_path / "selfcheck.pptx").write_bytes(b"PPTX")
    (tmp_path / "selfcheck.pdf").write_bytes(b"PDF")
    return tmp_path


async def test_the_documents_convert_and_partition_through_the_runner(tmp_path: Path) -> None:
    slot = StandInSlot(
        converted={DOCX: b"C-DOCX", PPTX: b"C-PPTX"},
        parts={
            b"C-DOCX": part("DOCCHAT SELFCHECK DOCX MARKER"),
            b"C-PPTX": part("DOCCHAT SELFCHECK PPTX MARKER"),
            b"PDF": part("Quarterly revenue", table=True),
        },
    )

    failures = await selfcheck.run_documents(StandInPool(slot), fixtures(tmp_path))  # type: ignore[arg-type]

    assert failures == []
    assert slot.calls == [
        f"inspect {DOCX}",
        f"convert {DOCX}",
        f"inspect {PDF}",
        "partition",
        f"inspect {PPTX}",
        f"convert {PPTX}",
        f"inspect {PDF}",
        "partition",
        f"inspect {PDF}",
        "partition",
    ]


async def test_each_failing_document_check_is_named(tmp_path: Path) -> None:
    slot = StandInSlot(
        converted={DOCX: b"C-DOCX"},
        parts={b"C-DOCX": part("no marker here"), b"PDF": part("no table")},
    )

    failures = await selfcheck.run_documents(StandInPool(slot), fixtures(tmp_path))  # type: ignore[arg-type]

    assert failures == [
        "converted_marker:selfcheck.docx",
        "convert:selfcheck.pptx",
        "table_html:selfcheck.pdf",
    ]


async def test_an_inspect_or_partition_failure_is_named(tmp_path: Path) -> None:
    root = fixtures(tmp_path)
    (root / "selfcheck.docx").write_bytes(b"BROKEN")
    slot = StandInSlot(converted={PPTX: b"C-PPTX"}, parts={})

    failures = await selfcheck.run_documents(StandInPool(slot), root)  # type: ignore[arg-type]

    assert failures == [
        "inspect:selfcheck.docx",
        "partition:selfcheck.pptx.pdf",
        "partition:selfcheck.pdf",
    ]


async def test_the_self_check_gathers_every_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(selfcheck, "check_pool_layout", lambda layout: ["scratch_parent"])
    slot = StandInSlot(converted={}, parts={})

    def broken() -> bool:
        raise RuntimeError("unreachable")

    failures = await selfcheck.run_selfcheck(
        fixtures(tmp_path),
        StandInPool(slot, probe=False),  # type: ignore[arg-type]
        checks=(lambda: True, broken),
        converter=lambda: ["libreoffice_version"],
    )

    assert failures[:5] == [
        "scratch_parent",
        "libreoffice_version",
        "broken",
        "isolation_probe",
        "isolation_probe:convert",
    ]


async def test_run_isolation_probe_uses_the_pool() -> None:
    assert await selfcheck.run_isolation_probe(StandInPool(StandInSlot()))  # type: ignore[arg-type]
    assert not await selfcheck.run_isolation_probe(StandInPool(StandInSlot(), probe=False))  # type: ignore[arg-type]


async def test_the_isolation_probe_runs_the_default_and_the_convert_mode_probe() -> None:
    """Section 15.11 item 7.3: the isolation probe also asserts, in a `convert`-mode probe, that
    the `convert` child's filter still refuses every socket family but `AF_UNIX`."""
    pool = StandInPool(StandInSlot())

    assert await selfcheck.run_isolation_probe(pool)  # type: ignore[arg-type]
    assert pool.probed == ["default", "convert"]
    assert selfcheck.PROBE_FILTERS == ("default", "convert")
    for outcomes, failures in (
        ({"default": True, "convert": False}, ["isolation_probe:convert"]),
        ({"default": False, "convert": True}, ["isolation_probe"]),
    ):
        pool = StandInPool(StandInSlot(), probe=outcomes)
        assert not await selfcheck.run_isolation_probe(pool)  # type: ignore[arg-type]
        assert await selfcheck.failed_probes(pool) == failures  # type: ignore[arg-type]
        assert pool.probed == ["default", "convert"] * 2


def test_main_prints_the_failed_checks_and_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def run(fixtures: Path) -> list[str]:
        return ["isolation_probe"] if fixtures == Path("x") else []

    monkeypatch.setattr(selfcheck, "run_selfcheck", run)

    assert selfcheck.main(["x"]) == 1
    assert capsys.readouterr().err == "selfcheck failed: isolation_probe\n"
    assert selfcheck.main(["y"]) == 0


def test_the_committed_fixtures_exist() -> None:
    names = sorted(path.name for path in FIXTURES.iterdir())

    assert names == ["selfcheck.docx", "selfcheck.pdf", "selfcheck.pptx"]
    assert (FIXTURES / "selfcheck.pdf").read_bytes().startswith(b"%PDF-")
    assert (FIXTURES / "selfcheck.docx").read_bytes().startswith(b"PK\x03\x04")
