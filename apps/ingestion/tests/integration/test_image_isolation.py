"""Parsing isolation inside the built ingestion image: T-204, the sibling isolation test, and
T-611, the converter linked-content test (`14-ingestion.md` sections 15.6, 15.8 and 15.11 and
acceptance criteria 47 and 50; `82-security.md`; `84-testing.md` section 1.5).

Both run only inside the image, where the parse users `docchat-parse-0` to `docchat-parse-15`,
their lock files under `/run/docchat-parse`, the scratch parent `/run/docchat-scratch` and (for
T-611) the pinned LibreOffice exist and the test runs as root, as the worker does; anywhere else
they skip and say why. They use the production pool (`isolation.runner.Layout()`).

T-204's hostile child is a fixture child: the pool's worker interpreter is replaced by a script
that runs the test's own program under the child's user and limits, so one slot's child attacks
the other slot's child while both run.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.server
import json
import os
import pwd
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import AsyncExitStack
from dataclasses import replace
from pathlib import Path
from typing import Any, Final, override

import pikepdf
import pytest

from docchat_ingestion.isolation import convert, runner
from docchat_ingestion.isolation.results import DOCX, PDF, PPTX, InspectOk
from docchat_ingestion.isolation.runner import Layout, ParsePool

pytestmark = pytest.mark.integration

FIXTURES: Final = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "documents"
PRODUCTION: Final = Layout()
SIBLING_CANARY_DIRECTORY: Final = PRODUCTION.scratch_parent / "docchat-sibling"
SIBLING_CANARY: Final = SIBLING_CANARY_DIRECTORY / "canary.txt"
LOOPBACK_PORT: Final = 18080
MACRO_MARKER: Final = Path("/tmp/docchat-macro-marker.txt")  # noqa: S108 - the fixtures' path


def outside_the_image() -> str | None:
    """Why the image's isolation can't be exercised here, or `None` inside the image."""
    if os.geteuid() != 0:
        return "inside the ingestion image the worker runs as root; this process doesn't"
    try:
        for n in range(PRODUCTION.users):
            pwd.getpwnam(f"docchat-parse-{n}")
    except KeyError:
        return "the parse users docchat-parse-0 to docchat-parse-15 exist only in the image"
    if not all(PRODUCTION.lock_path(uid).is_file() for uid in PRODUCTION.uids):
        return "the lock files under /run/docchat-parse exist only in the image"
    parent = PRODUCTION.scratch_parent
    if not parent.is_dir() or stat.S_IMODE(parent.stat().st_mode) != 0o711:
        return "the scratch parent /run/docchat-scratch (mode 0711) exists only in the image"
    return None


def pinned_converter() -> str | None:
    if shutil.which("dpkg-query") is None:
        return "dpkg-query isn't installed: this isn't the Debian 12 image"
    version = subprocess.run(
        ["dpkg-query", "-W", "-f=${Version}", "libreoffice-core-nogui"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    if version != convert.LIBREOFFICE_PACKAGE_VERSION:
        return f"the pinned converter {convert.LIBREOFFICE_PACKAGE_VERSION} isn't installed"
    return None


# Decided at collection, so no fixture binds a port or writes under /run outside the image.
OUTSIDE: Final = outside_the_image()
NO_CONVERTER: Final = OUTSIDE or pinned_converter()
in_the_image = pytest.mark.skipif(OUTSIDE is not None, reason=str(OUTSIDE))
with_the_pinned_converter = pytest.mark.skipif(NO_CONVERTER is not None, reason=str(NO_CONVERTER))


def production_pool(layout: Layout = PRODUCTION) -> ParsePool:
    return ParsePool(
        slots_per_process=2, memory_limit_mb=2048, parse_timeout_seconds=600, layout=layout
    )


# --- T-204 ----------------------------------------------------------------------------------------

FIXTURE_CHILD: Final = r"""
import ctypes, errno, json, os, sys, time
from pathlib import Path

scratch = Path(sys.argv[1]).parent
role = json.loads((scratch / "role.json").read_text())
findings = {}

def attempt(name, action):
    try:
        action()
    except OSError as error:
        findings[name] = error.errno or errno.EPERM
    else:
        findings[name] = 0

if role["role"] == "hostile":
    uid = role["siblingUid"]
    pids = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                lines = (entry / "status").read_text().splitlines()
            except OSError:
                continue
            uids = [line.split()[1] for line in lines if line.startswith("Uid:")]
            if uids and int(uids[0]) == uid:
                pids.append(int(entry.name))
    findings["siblingFound"] = bool(pids)
    pid = pids[0] if pids else 1
    libc = ctypes.CDLL(None, use_errno=True)
    libc.ptrace.restype = ctypes.c_long
    libc.ptrace.argtypes = [ctypes.c_long, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]
    result = libc.ptrace(16, pid, None, None)
    findings["ptrace"] = ctypes.get_errno() if result == -1 else 0

    class Iovec(ctypes.Structure):
        _fields_ = [("base", ctypes.c_void_p), ("length", ctypes.c_size_t)]

    buffer = ctypes.create_string_buffer(64)
    local = Iovec(ctypes.cast(buffer, ctypes.c_void_p), 64)
    remote = Iovec(ctypes.c_void_p(0x400000), 64)
    libc.process_vm_readv.restype = ctypes.c_ssize_t
    result = libc.process_vm_readv(
        pid, ctypes.byref(local), 1, ctypes.byref(remote), 1, 0
    )
    findings["processVmReadv"] = ctypes.get_errno() if result == -1 else 0
    attempt("procMem", lambda: open(f"/proc/{pid}/mem", "rb").read(1))
    attempt("listScratchParent", lambda: os.listdir(role["scratchParent"]))
    attempt("listSiblingScratch", lambda: os.listdir(role["siblingScratch"]))
    attempt("readSiblingSource", lambda: open(role["siblingSource"], "rb").read(1))
    with open(role["tmpFile"], "w") as handle:
        handle.write("written by the hostile child")
    findings["tmpMode"] = os.stat(role["tmpFile"]).st_mode & 0o777
else:
    deadline = time.monotonic() + 20
    while not os.path.exists(role["hostileTmp"]) and time.monotonic() < deadline:
        time.sleep(0.05)
    attempt("readHostileTmp", lambda: open(role["hostileTmp"], "rb").read(1))
    time.sleep(0.2)

Path(role["findings"]).write_text(json.dumps(findings))
(scratch / "result.json").write_text(json.dumps({"ok": True, "pageCount": 7}))
"""


@pytest.fixture
def fixture_child_layout() -> Iterator[Layout]:
    """The production layout with the worker interpreter replaced by the fixture child."""
    directory = Path(tempfile.mkdtemp(prefix="docchat-t204-"))
    try:
        directory.chmod(0o755)
        program = directory / "fixture_child.py"
        program.write_text(FIXTURE_CHILD, encoding="utf-8")
        program.chmod(0o644)
        launcher = directory / "python"
        # The pool runs `<python> -I -m <worker> <request.json>`; the request path is the 4th.
        launcher.write_text(f'#!/bin/sh\nexec {sys.executable} -I {program} "$4"\n')
        launcher.chmod(0o755)
        yield replace(PRODUCTION, python=str(launcher))
    finally:
        shutil.rmtree(directory, ignore_errors=True)


async def _wait_for_processes(uid: int) -> None:
    for _ in range(200):
        if runner.processes_of(PRODUCTION.proc, uid) > 0:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the sibling child never started")


@in_the_image
async def test_t204_a_hostile_child_cant_reach_its_sibling(
    fixture_child_layout: Layout,
) -> None:
    token = secrets.token_hex(4)
    hostile_tmp = f"/tmp/t204-{token}"  # noqa: S108 - a world-writable directory under test
    findings_of = {
        role: Path(f"/var/tmp/t204-{role}-{token}.json")  # noqa: S108
        for role in ("hostile", "sibling")
    }
    pool = production_pool(fixture_child_layout)

    async with AsyncExitStack() as stack:
        sibling_slot = await stack.enter_async_context(pool.slot(120))
        hostile_slot = await stack.enter_async_context(pool.slot(120))
        sibling_dir = sibling_slot.scratch()
        sibling_dir.write("source.bin", b"%PDF-1.7 sibling bytes")
        sibling_dir.write(
            "role.json",
            json.dumps(
                {
                    "role": "sibling",
                    "hostileTmp": hostile_tmp,
                    "findings": str(findings_of["sibling"]),
                }
            ).encode(),
        )
        sibling = asyncio.create_task(sibling_slot.inspect(sibling_dir, PDF, max_pages=2000))
        await _wait_for_processes(sibling_slot.first_uid)
        hostile_dir = hostile_slot.scratch()
        hostile_dir.write("source.bin", b"%PDF-1.7 hostile bytes")
        hostile_dir.write(
            "role.json",
            json.dumps(
                {
                    "role": "hostile",
                    "siblingUid": sibling_slot.first_uid,
                    "scratchParent": str(PRODUCTION.scratch_parent),
                    "siblingScratch": str(sibling_dir.path),
                    "siblingSource": str(sibling_dir.path / "source.bin"),
                    "tmpFile": hostile_tmp,
                    "findings": str(findings_of["hostile"]),
                }
            ).encode(),
        )
        hostile_outcome = await hostile_slot.inspect(hostile_dir, PDF, max_pages=2000)
        sibling_outcome = await sibling
        hostile = json.loads(await asyncio.to_thread(findings_of["hostile"].read_text))
        sibling_findings = json.loads(await asyncio.to_thread(findings_of["sibling"].read_text))
        hostile_uid = hostile_slot.first_uid

    assert hostile_uid != sibling_slot.first_uid
    assert hostile["siblingFound"] is True
    for attack in (
        "ptrace",
        "processVmReadv",
        "procMem",
        "listScratchParent",
        "listSiblingScratch",
        "readSiblingSource",
    ):
        assert hostile[attack] != 0, attack
    assert hostile["tmpMode"] & 0o077 == 0
    assert sibling_findings["readHostileTmp"] != 0
    assert hostile_outcome.ok
    assert sibling_outcome.ok
    assert sibling_outcome.result == InspectOk(ok=True, pageCount=7)


# --- T-611 ----------------------------------------------------------------------------------------

LINKED_FIXTURES: Final = (
    ("include-fields.docx", DOCX),
    ("linked-section.docx", DOCX),
    ("linked-image-unc.docx", DOCX),
    ("linked-image-http.pptx", PPTX),
    ("ole-link.docx", DOCX),
    ("ole-link.pptx", PPTX),
    ("xxe.docx", DOCX),
    ("vba-autoopen.docx", DOCX),
    ("vba-autoopen.pptx", PPTX),
)


class _Recorder(http.server.BaseHTTPRequestHandler):
    requests: list[str] = []  # noqa: RUF012 - shared by the stand-in's handler instances

    def do_GET(self) -> None:
        _Recorder.requests.append(self.path)
        self.send_response(404)
        self.end_headers()

    @override
    def log_message(self, format: str, *args: Any) -> None:
        return None


@pytest.fixture
def loopback_stand_in() -> Iterator[list[str]]:
    """An HTTP server on 127.0.0.1:18080, where `linked-image-http.pptx` links its image; it
    records every request that reaches it."""
    _Recorder.requests = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", LOOPBACK_PORT), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _Recorder.requests
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def sibling_canary() -> Iterator[str]:
    """The canary the include fields name, in a scratch directory of another parse user."""
    value = f"cnry-filename-{secrets.token_hex(4)}"
    owner = PRODUCTION.uids[-1]
    SIBLING_CANARY_DIRECTORY.mkdir(mode=0o700, exist_ok=True)
    SIBLING_CANARY.write_text(value, encoding="utf-8")
    os.chown(SIBLING_CANARY, owner, owner)
    os.chown(SIBLING_CANARY_DIRECTORY, owner, owner)
    try:
        yield value
    finally:
        shutil.rmtree(SIBLING_CANARY_DIRECTORY, ignore_errors=True)


def _text_and_images(data: bytes, directory: Path) -> tuple[str, bytes]:
    path = directory / "converted.pdf"
    path.write_bytes(data)
    text = subprocess.run(  # noqa: S603 - fixed arguments on the test's own file
        ["pdftotext", str(path), "-"],  # noqa: S607 - poppler-utils of the image
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    images = b""
    with pikepdf.open(path) as pdf:
        for page in pdf.pages:
            for image in page.images.values():
                with contextlib.suppress(pikepdf.PdfError):
                    images += image.read_bytes()
    return text, images


def _markers() -> list[Path]:
    places = (Path("/tmp"), Path("/var/tmp"), Path("/dev/shm"), PRODUCTION.scratch_parent)  # noqa: S108
    found: list[Path] = []
    for place in places:
        with contextlib.suppress(OSError):
            found.extend(place.rglob(MACRO_MARKER.name))
    return found


@with_the_pinned_converter
@pytest.mark.parametrize(("name", "content_type"), LINKED_FIXTURES)
async def test_t611_the_converter_pulls_in_no_linked_content(
    name: str,
    content_type: str,
    loopback_stand_in: list[str],
    sibling_canary: str,
    tmp_path: Path,
) -> None:
    hostname = (await asyncio.to_thread(Path("/etc/hostname").read_text, "utf-8")).strip()
    await asyncio.to_thread(MACRO_MARKER.unlink, missing_ok=True)
    pool = production_pool()

    async with pool.slot(900) as slot:
        directory = slot.scratch()
        directory.write("source.bin", (FIXTURES / name).read_bytes())
        outcome, data = await slot.convert(
            directory, content_type, limit_seconds=900, max_pages=2000
        )

    assert outcome.ok, outcome.code
    assert data is not None
    text, images = await asyncio.to_thread(_text_and_images, data, tmp_path)
    assert hostname not in text
    assert hostname.encode() not in images
    assert sibling_canary not in text
    assert sibling_canary.encode() not in images
    assert loopback_stand_in == []
    assert await asyncio.to_thread(_markers) == []
