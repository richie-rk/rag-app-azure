"""The deployed suites' seed data and generated documents (`84-testing.md` sections 1.5, 6.5 to
6.7).

`ensure_seed_data` is the work of the deployed session fixture: through the admin API as `admin`
it creates, idempotently and by name, the non-default project `Automated tests` with `member` as
its member, and the index `automated-tests` (the page-wise method at its default parameters)
mapped as its default, holding the four fixture documents ingested and `completed`. A run finds
what earlier runs created and only adds what is missing, so the nightly suites reuse one project
and one index. The idempotency suite, the crash-resume test and the malware-scanning suites use
their own indexes in the same project (`ensure_mapped_index`).

Generated documents: `byte_unique` makes a copy of a document whose bytes no other upload has,
as `docchat-load build-corpus` does (a PDF comment line after `%%EOF`, or the ZIP archive
comment of a DOCX or PPTX), so the SHA-256 duplicate check sees a new document while parsing
stays the same; `generated_pdf` writes a PDF of numbered text pages, padded to an exact size with
an unreferenced stream of random bytes, which no reader renders and no parser reads.
"""

from __future__ import annotations

import hashlib
import os
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from docchat_testing.adminapi import AdminApi, UploadFile
from docchat_testing.tokens import REPOSITORY_ROOT

PROJECT_NAME: Final = "Automated tests"
PROJECT_DESCRIPTION: Final = "Created and used by the deployed test suites"
INDEX_NAME: Final = "automated-tests"
INDEX_METHOD: Final = "pagewise"
IDEMPOTENCY_INDEX_NAME: Final = "automated-tests-idempotency"
RESUME_INDEX_NAME: Final = "automated-tests-resume"
SCAN_INDEX_NAME: Final = "scan-tests"
FIXTURE_DOCUMENTS: Final = ("tables.pdf", "scanned.pdf", "headings.docx", "slides.pptx")
DOCUMENTS_DIR: Final = REPOSITORY_ROOT / "tests" / "fixtures" / "documents"

LARGE_UPLOAD_BYTES: Final = 52_428_800
"""`DOCCHAT_UPLOAD_MAX_BYTES`' default, the "50 MB" of `14-ingestion.md` section 2.2."""

_PDF_EOF: Final = b"%%EOF"
_ZIP_END_SIGNATURE: Final = b"PK\x05\x06"
_ZIP_END_SIZE: Final = 22
_ZIP_COMMENT_LENGTH_OFFSET: Final = 20
_ZIP_COMMENT_MAX: Final = 65_535
_LINES_PER_PAGE: Final = 24
_SIZE_ROUNDS: Final = 4


class SeedError(Exception):
    """The seed data can't be brought into the state the suites need; the message says what an
    operator has to look at."""


@dataclass(frozen=True, slots=True)
class MappedIndex:
    """An index of the suites and the project `Automated tests` it is mapped to."""

    project_id: str
    index_id: str


@dataclass(frozen=True, slots=True)
class SeedData:
    """What the deployed session fixture leaves in place: the project, its default index and
    the fixture documents by file name."""

    project_id: str
    index_id: str
    document_ids: Mapping[str, str]


# --- Generated documents ----------------------------------------------------------------------


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def upload_file(name: str, data: bytes) -> UploadFile:
    return UploadFile(name=name, data=data, sha256=sha256_hex(data))


def _zip_with_comment(data: bytes, comment: bytes) -> bytes:
    end = data.rfind(_ZIP_END_SIGNATURE)
    if end < 0 or len(data) - end < _ZIP_END_SIZE:
        raise ValueError("not a ZIP archive: no end-of-central-directory record")
    (length,) = struct.unpack_from("<H", data, end + _ZIP_COMMENT_LENGTH_OFFSET)
    if end + _ZIP_END_SIZE + length != len(data):
        raise ValueError("the ZIP archive has bytes after its end-of-central-directory record")
    if len(comment) > _ZIP_COMMENT_MAX:
        raise ValueError("a ZIP archive comment holds at most 65,535 bytes")
    record = bytearray(data[end : end + _ZIP_END_SIZE])
    struct.pack_into("<H", record, _ZIP_COMMENT_LENGTH_OFFSET, len(comment))
    return data[:end] + bytes(record) + comment


def byte_unique(data: bytes, name: str, label: str) -> bytes:
    """A copy of the document `name` whose bytes carry `label`: the PDF comment line
    `%<label>` after `%%EOF`, or `label` as the ZIP archive comment of a DOCX or PPTX."""
    if not label.isascii() or not label.isprintable():
        raise ValueError("the label must be printable ASCII")
    extension = Path(name).suffix.lower()
    if extension == ".pdf":
        if _PDF_EOF not in data[-1024:]:
            raise ValueError(f"{name} has no %%EOF marker near its end")
        separator = b"" if data.endswith(b"\n") else b"\n"
        return data + separator + b"%" + label.encode("ascii") + b"\n"
    if extension in {".docx", ".pptx"}:
        return _zip_with_comment(data, label.encode("ascii"))
    raise ValueError(f"{name}: only PDF, DOCX and PPTX documents are uploaded")


def _pdf_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _page_lines(label: str, page: int) -> list[str]:
    lines = [f"{label}: page {page}"]
    lines += [
        f"Section {page}.{line}: entry {page * 100 + line} of the {label} test document records "
        f"that item {line} on page {page} belongs to group {(page + line) % 7}."
        for line in range(1, _LINES_PER_PAGE)
    ]
    return lines


def _pdf(pages: int, label: str, padding: int) -> bytes:
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",  # the page tree, written once the page object numbers are known
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    kids: list[int] = []
    for page in range(1, pages + 1):
        text = " T* ".join(f"({_pdf_text(line)}) Tj" for line in _page_lines(label, page))
        stream = f"BT /F1 10 Tf 14 TL 56 780 Td {text} ET".encode("latin-1")
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        content = len(objects)
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % content
        )
        kids.append(len(objects))
    references = " ".join(f"{kid} 0 R" for kid in kids)
    objects[1] = f"<< /Type /Pages /Kids [{references}] /Count {pages} >>".encode("ascii")
    if padding:
        # Unreferenced from the page tree: it makes the file large without giving a reader or a
        # parser anything to do.
        objects.append(
            b"<< /Length %d >>\nstream\n" % padding + os.urandom(padding) + b"\nendstream"
        )
    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def generated_pdf(pages: int, *, label: str, size_bytes: int | None = None) -> bytes:
    """A PDF of `pages` numbered pages of text naming `label`, exactly `size_bytes` long when a
    size is given (padded with an unreferenced stream of random bytes)."""
    if pages < 1:
        raise ValueError("a PDF needs at least one page")
    data = _pdf(pages, label, 0)
    if size_bytes is None:
        return data
    padding = 0
    for _ in range(_SIZE_ROUNDS):
        missing = size_bytes - len(data)
        if missing == 0:
            return data
        padding += missing
        if padding <= 0:
            break
        data = _pdf(pages, label, padding)
    raise ValueError(f"can't write {pages} pages in exactly {size_bytes} bytes")


def fixture_bytes(name: str, directory: Path = DOCUMENTS_DIR) -> bytes:
    return (directory / name).read_bytes()


# --- The project and its indexes --------------------------------------------------------------


async def ensure_project_and_default_index(api: AdminApi) -> MappedIndex:
    """The project `Automated tests` and its default index `automated-tests`; the project's
    first mapping must be its default, so every other index of the suites is mapped after it."""
    project = await api.ensure_project(PROJECT_NAME, description=PROJECT_DESCRIPTION)
    index = await api.ensure_index(INDEX_NAME, method_key=INDEX_METHOD)
    project_id, index_id = str(project["projectId"]), str(index["indexId"])
    await api.ensure_mapping(project_id, index_id, is_default=True)
    return MappedIndex(project_id=project_id, index_id=index_id)


async def ensure_mapped_index(api: AdminApi, name: str) -> MappedIndex:
    """The index `name` (the catalogue's default method and settings when created) mapped,
    not as default, to `Automated tests`."""
    default = await ensure_project_and_default_index(api)
    index = await api.ensure_index(name)
    index_id = str(index["indexId"])
    await api.ensure_mapping(default.project_id, index_id, is_default=False)
    return MappedIndex(project_id=default.project_id, index_id=index_id)


# --- Documents --------------------------------------------------------------------------------


def _by_name(documents: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    return {str(document["name"]).casefold(): document for document in documents}


def _version_state(version: Mapping[str, Any] | None, sha256: str) -> str | None:
    if version is None or version.get("sha256") != sha256:
        return None
    return str(version["state"])


async def ensure_documents(
    api: AdminApi,
    index: MappedIndex,
    files: Sequence[UploadFile],
    *,
    within: float,
) -> dict[str, str]:
    """Make each file a live document of the index whose current version holds its bytes and
    is `ingested`; file name to document ID.

    Preflight decides per file: `new` and `update` are uploaded (an update confirmed); a file
    already `unchanged` gets any of its `pending` versions submitted again, which repairs a run
    that stopped between upload and submit (`already_queued` answers are harmless); a
    `duplicate` or a `rejected` file is a state an operator has to look at."""
    results = await api.preflight(index.index_id, files)
    pending: list[str] = []
    document_ids: dict[str, str] = {}
    for file, result in zip(files, results, strict=True):
        status = result["status"]
        if status in {"new", "update"}:
            reservation = await api.upload(
                index.index_id, file, index.project_id, confirm_update=status == "update"
            )
            document_ids[file.name] = reservation.document_id
            pending.append(reservation.version_id)
        elif status == "unchanged":
            document_id = str(result["documentId"])
            document_ids[file.name] = document_id
            detail = await api.get_document(index.index_id, document_id)
            versions = detail["versions"] if detail is not None else []
            pending += [str(v["versionId"]) for v in versions if v["state"] == "pending"]
        else:
            raise SeedError(
                f"{file.name}: preflight answered {status} ({result.get('reason')}); "
                f"another document of the index holds its bytes or its name is refused"
            )
    if pending:
        await api.submit(index.index_id, pending)
    await wait_until_ingested(api, index.index_id, files, within=within)
    return document_ids


async def wait_until_ingested(
    api: AdminApi, index_id: str, files: Sequence[UploadFile], *, within: float
) -> None:
    """Poll until each file's document serves those bytes from an `ingested` current version;
    a latest version of those bytes that `failed` ends the wait at once."""

    async def ingested() -> bool | None:
        documents = _by_name(await api.list_documents(index_id))
        waiting = False
        for file in files:
            document = documents.get(file.name.casefold())
            if document is None:
                raise SeedError(f"{file.name} isn't a live document of the index")
            if _version_state(document.get("currentVersion"), file.sha256) == "ingested":
                continue
            latest = document.get("latestVersion")
            if _version_state(latest, file.sha256) == "failed":
                code = latest.get("failureCode") if latest is not None else None
                raise SeedError(f"{file.name} failed to ingest ({code})")
            waiting = True
        return None if waiting else True

    await api.wait_until(ingested, within=within, what="the documents being ingested")


async def delete_documents(
    api: AdminApi, index_id: str, document_ids: Sequence[str], *, within: float
) -> None:
    """Delete the documents and wait until each delete run has ended."""
    runs = [
        run_id
        for document_id in document_ids
        if (run_id := await api.delete_document(index_id, document_id)) is not None
    ]
    for run_id in runs:
        await api.wait_for_run(run_id, within=within)


async def remove_all_documents(api: AdminApi, index_id: str, *, within: float) -> None:
    """Delete every live document of the index and wait until the delete runs have ended."""
    documents = await api.list_documents(index_id)
    await delete_documents(
        api, index_id, [str(document["documentId"]) for document in documents], within=within
    )


async def ensure_seed_data(
    api: AdminApi,
    *,
    member_id: str | None,
    within: float,
    directory: Path = DOCUMENTS_DIR,
) -> SeedData:
    """The deployed session fixture's seed data (`84-testing.md` section 6.6). `member_id` is
    the `member` alias's object ID; that user must have signed in once (any `/v1` call
    provisions it), because only signed-in people can be added to a project."""
    index = await ensure_project_and_default_index(api)
    if member_id is not None:
        await api.ensure_member(index.project_id, member_id)
    files = [upload_file(name, fixture_bytes(name, directory)) for name in FIXTURE_DOCUMENTS]
    document_ids = await ensure_documents(api, index, files, within=within)
    return SeedData(project_id=index.project_id, index_id=index.index_id, document_ids=document_ids)
