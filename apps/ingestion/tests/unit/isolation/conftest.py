"""Fixtures shared by the isolation unit tests: small PDFs and Office packages built in the test
(the fixture documents of `84-testing.md` section 1.5 are another module's), and the layout of a
parse-user pool in a temporary directory for the tests that start real children.

A real child needs root (for `setpriv`), `setpriv`, `prlimit` and `libseccomp.so.2`; without them
those tests skip. Their directories live under `/tmp`, because a parse user must be able to reach
its scratch directory and the pytest base directory is private to root.
"""

from __future__ import annotations

import ctypes.util
import io
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import pikepdf
import pytest
from pikepdf import Dictionary, Name, Pdf, Stream

from docchat_ingestion.isolation.runner import Layout

type PdfFactory = Callable[..., bytes]
type PackageFactory = Callable[..., bytes]

DOCX_MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
PPTX_MAIN = "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
OFFICE_DOCUMENT = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
)
RELATIONSHIPS = "application/vnd.openxmlformats-package.relationships+xml"
TEST_UIDS = range(10002, 10018)


def text_page(pdf: Pdf, text: str, size: tuple[int, int] = (200, 200)) -> pikepdf.Page:
    """A page that draws `text` in Helvetica."""
    page = pdf.add_blank_page(page_size=size)
    font = Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica)
    page.obj.Resources = Dictionary(Font=Dictionary(F1=font))
    page.obj.Contents = Stream(pdf, f"BT /F1 12 Tf 10 100 Td ({text}) Tj ET".encode("latin-1"))
    return page


def save(pdf: Pdf, **options: object) -> bytes:
    buffer = io.BytesIO()
    pdf.save(buffer, **options)  # type: ignore[arg-type]
    return buffer.getvalue()


@pytest.fixture
def make_pdf() -> PdfFactory:
    """`make_pdf(pages=1, text="Page")`: a plain PDF whose page `n` draws `{text} {n}`."""

    def build(pages: int = 1, text: str = "Page") -> bytes:
        pdf = Pdf.new()
        for number in range(1, pages + 1):
            text_page(pdf, f"{text} {number}")
        return save(pdf)

    return build


def _content_types(
    main_part: str, main_type: str, defaults: Mapping[str, str], overrides: Mapping[str, str]
) -> str:
    entries = [
        f'<Default Extension="rels" ContentType="{RELATIONSHIPS}"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
        *(f'<Default Extension="{ext}" ContentType="{kind}"/>' for ext, kind in defaults.items()),
        f'<Override PartName="/{main_part}" ContentType="{main_type}"/>',
        *(
            f'<Override PartName="{part}" ContentType="{kind}"/>'
            for part, kind in overrides.items()
        ),
    ]
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        + "".join(entries)
        + "</Types>"
    )


def _relationships(target: str, relationship_type: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{relationship_type}" Target="{target}"/>'
        "</Relationships>"
    )


WORD_DOCUMENT = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    "<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
)


@pytest.fixture
def make_package() -> PackageFactory:
    """`make_package(kind="docx", ...)`: a minimal Office Open XML package written with
    `zipfile`, with switches for each rule of the `inspect` checks."""

    def build(
        kind: str = "docx",
        *,
        text: str = "Hello",
        main_type: str | None = None,
        relationship_type: str = OFFICE_DOCUMENT,
        defaults: Mapping[str, str] | None = None,
        overrides: Mapping[str, str] | None = None,
        extra: Mapping[str, bytes] | None = None,
        content_types: bytes | None = None,
        relationships: bytes | None = None,
        encrypt: str | None = None,
    ) -> bytes:
        main_part = "word/document.xml" if kind == "docx" else "ppt/presentation.xml"
        main = (DOCX_MAIN if kind == "docx" else PPTX_MAIN) if main_type is None else main_type
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "[Content_Types].xml",
                content_types
                or _content_types(main_part, main, defaults or {}, overrides or {}).encode(),
            )
            archive.writestr(
                "_rels/.rels",
                relationships or _relationships(main_part, relationship_type).encode(),
            )
            archive.writestr(main_part, WORD_DOCUMENT.format(text=text))
            for name, data in (extra or {}).items():
                archive.writestr(name, data)
        data = buffer.getvalue()
        if encrypt is not None:
            data = _set_encrypted_flag(data, encrypt)
        return data

    return build


def _set_encrypted_flag(data: bytes, name: str) -> bytes:
    """Set general-purpose bit 0 on one entry, in its local header and its central record."""
    raw = bytearray(data)
    encoded = name.encode()
    # (signature, flags offset, name-length offset, name offset) of the two record kinds.
    for signature, flags_at, length_at, name_at in (
        (b"PK\x03\x04", 6, 26, 30),
        (b"PK\x01\x02", 8, 28, 46),
    ):
        start = raw.find(signature)
        while start >= 0:
            length = int.from_bytes(raw[start + length_at : start + length_at + 2], "little")
            if raw[start + name_at : start + name_at + length] == encoded:
                raw[start + flags_at] |= 1
            start = raw.find(signature, start + 4)
    return bytes(raw)


def isolation_available() -> str | None:
    """Why real children can't run here, or `None`."""
    if os.geteuid() != 0:
        return "real parse children need root, as in the ingestion image"
    if shutil.which("setpriv") is None or shutil.which("prlimit") is None:
        return "setpriv and prlimit (util-linux) aren't installed"
    if ctypes.util.find_library("seccomp") is None:
        return "libseccomp.so.2 isn't installed"
    return None


def kill_test_uids() -> None:
    for uid in TEST_UIDS:
        subprocess.run(  # noqa: S603 - fixed arguments
            [  # noqa: S607 - setpriv from PATH, as the runner runs it
                "setpriv",
                f"--reuid={uid}",
                f"--regid={uid}",
                "--clear-groups",
                "--",
                "kill",
                "-KILL",
                "-1",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


@pytest.fixture
def parse_layout() -> Iterator[Layout]:
    """A parse-user pool in a temporary directory under `/tmp`: a scratch parent with mode
    0711, 16 root-owned lock files of mode 0600 and one world-writable directory, with this
    environment's Python standing in for `/opt/venv/bin/python`."""
    reason = isolation_available()
    if reason is not None:
        pytest.skip(reason)
    base = Path(tempfile.mkdtemp(prefix="docchat-isolation-test-"))
    base.chmod(0o755)
    scratch = base / "scratch"
    scratch.mkdir()
    scratch.chmod(0o711)
    locks = base / "locks"
    locks.mkdir(mode=0o700)
    for uid in TEST_UIDS:
        fd = os.open(locks / f"{uid}.lock", os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
    shared = base / "tmp"
    shared.mkdir()
    shared.chmod(0o1777)
    try:
        yield Layout(
            python=sys.executable,
            scratch_parent=scratch,
            lock_directory=locks,
            world_writable=(shared,),
        )
    finally:
        kill_test_uids()
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def stub_python(parse_layout: Layout) -> Callable[[str], Layout]:
    """`stub_python(script)`: the layout with a stand-in for the worker's Python that runs the
    shell `script` in the child's place, under the child's user and limits."""

    def build(script: str) -> Layout:
        path = parse_layout.scratch_parent.parent / f"stub-{abs(hash(script))}.sh"
        path.write_text("#!/bin/sh\n" + script + "\n")
        path.chmod(
            stat.S_IRUSR
            | stat.S_IWUSR
            | stat.S_IRGRP
            | stat.S_IXGRP
            | stat.S_IROTH
            | stat.S_IXOTH
            | stat.S_IXUSR
        )
        return Layout(
            python=str(path),
            scratch_parent=parse_layout.scratch_parent,
            lock_directory=parse_layout.lock_directory,
            world_writable=parse_layout.world_writable,
        )

    return build
