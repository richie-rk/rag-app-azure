"""The `inspect` checks (`14-ingestion.md` section 15.6 item 7), run in a parsing child.

For a DOCX or PPTX source they guard the converter: they refuse a file that isn't the type its
name says, that would exhaust the child before the conversion starts, that the converter would be
asked to decrypt, or whose content types declare macros, and leave every other construct to the
converter's sandbox and settings, because no reader ever receives the Office file. Every rule has
a stable label, which names its fixtures in the document check unit tests.

Part names, content types and relationship types compare case-insensitively (ASCII case folding),
because Office Open XML part names and media types are case-insensitive. The archive is read from
its central directory with `zipfile`, never extracted to disk, and every XML part is read as a
stream with entity expansion, network access and huge trees off. Nothing here raises for a
hostile file: what can't be read breaks `zip.unreadable` where the reader meets it.
"""

from __future__ import annotations

import posixpath
import zipfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Final
from urllib.parse import unquote

# lxml ships no type information.
import pikepdf
from lxml import etree

from docchat_ingestion.isolation.results import (
    DOCX,
    OFFICE_TYPES,
    PDF,
    PPTX,
    ZIP_MAX_BYTES,
    ZIP_MAX_ENTRIES,
    InspectCode,
    InspectFailed,
    InspectOk,
)

RULE_CODES: Final[dict[str, InspectCode]] = {
    "sig.mismatch": "file_signature_mismatch",
    "zip.entries": "zip_entry_limit_exceeded",
    "zip.size": "zip_size_limit_exceeded",
    "zip.ratio": "zip_size_limit_exceeded",
    "zip.encrypted": "encrypted_document",
    "zip.unreadable": "corrupt_document",
    "zip.duplicate": "corrupt_document",
    "macro.type": "macro_content_declared",
    "macro.vba": "macro_content_declared",
    "sig.type": "file_signature_mismatch",
    "pdf.password": "encrypted_document",
    "pdf.unreadable": "corrupt_document",
}
"""Each rule's label and the non-retryable item code it fails the item with."""

MAIN_CONTENT_TYPES: Final = {
    DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    PPTX: "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
}

PDF_SIGNATURE: Final = b"%PDF-"
PDF_SIGNATURE_WINDOW: Final = 1024
ZIP_SIGNATURE: Final = b"PK\x03\x04"
RATIO_ENTRY_MIN_BYTES: Final = 10 * 1024 * 1024
RATIO_MAX: Final = 200
VBA_PROJECT: Final = "application/vnd.ms-office.vbaproject"
CONTENT_TYPES_PART: Final = "[content_types].xml"
PACKAGE_RELATIONSHIPS_PART: Final = "_rels/.rels"
OFFICE_DOCUMENT_SUFFIX: Final = "/officedocument"
_ENCRYPTED_FLAG: Final = 0x1


class _UnreadableError(Exception):
    """An archive entry or XML part that can't be read or parsed."""


def signature_matches(head: bytes, content_type: str) -> bool:
    """Rule `sig.mismatch` (section 2.5 step 4): `%PDF-` within the first 1,024 bytes of a PDF,
    `50 4B 03 04` at offset 0 of a DOCX or PPTX."""
    if content_type == PDF:
        return PDF_SIGNATURE in head[:PDF_SIGNATURE_WINDOW]
    if content_type in OFFICE_TYPES:
        return head.startswith(ZIP_SIGNATURE)
    return False


def _fold(text: str) -> str:
    # ASCII case folding only: Office Open XML compares part names and media types so.
    return text.translate(_ASCII_LOWER)


_ASCII_LOWER: Final = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def _part_name(entry_name: str) -> str:
    return _fold("/" + entry_name.lstrip("/"))


def _iterparse(stream: IO[bytes]) -> Iterator[tuple[str, Any]]:
    """Read one XML part as a stream: no entity is expanded and nothing is fetched."""
    return iter(
        etree.iterparse(
            stream,
            events=("end",),
            resolve_entities=False,
            no_network=True,
            huge_tree=False,
            load_dtd=False,
        )
    )


def _local_name(element: object) -> str:
    tag = getattr(element, "tag", None)
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


@dataclass
class _Package:
    """What the parts of an Office package say about its types and its main part."""

    defaults: dict[str, str] = field(default_factory=dict)
    overrides: dict[str, str] = field(default_factory=dict)
    declared: list[str] = field(default_factory=list)
    main_targets: list[str] = field(default_factory=list)

    def content_type(self, part_name: str) -> str | None:
        """The `Override` for the part name, else the `Default` for its extension, else none."""
        override = self.overrides.get(part_name)
        if override is not None:
            return override
        extension = posixpath.splitext(part_name)[1].removeprefix(".")
        return self.defaults.get(extension) if extension else None


def _read_part(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, package: _Package, *, kind: str
) -> None:
    try:
        with archive.open(info) as stream:
            for _event, element in _iterparse(stream):
                name = _local_name(element)
                attributes = getattr(element, "attrib", {})
                if kind == "types" and name == "Default":
                    extension = _fold(str(attributes.get("Extension", "")))
                    content_type = _fold(str(attributes.get("ContentType", "")))
                    package.defaults[extension] = content_type
                    package.declared.append(content_type)
                elif kind == "types" and name == "Override":
                    part = _part_name(unquote(str(attributes.get("PartName", ""))))
                    content_type = _fold(str(attributes.get("ContentType", "")))
                    package.overrides[part] = content_type
                    package.declared.append(content_type)
                elif kind == "rels" and name == "Relationship":
                    relationship_type = _fold(str(attributes.get("Type", "")))
                    external = _fold(str(attributes.get("TargetMode", ""))) == "external"
                    if relationship_type.endswith(OFFICE_DOCUMENT_SUFFIX) and not external:
                        target = unquote(str(attributes.get("Target", "")))
                        package.main_targets.append(
                            _part_name(posixpath.normpath("/" + target.lstrip("/")))
                        )
                # Only what the rules read is kept, so a large part never grows the tree.
                clear = getattr(element, "clear", None)
                if clear is not None:
                    clear()
    except (
        etree.LxmlError,
        zipfile.BadZipFile,
        zlib.error,
        EOFError,
        NotImplementedError,
        RuntimeError,
        ValueError,
        OSError,
    ):
        raise _UnreadableError from None


def _is_xml_part(entry_name: str, package: _Package) -> bool:
    folded = _fold(entry_name)
    if folded.endswith((".xml", ".rels")):
        return True
    content_type = package.content_type(_part_name(entry_name))
    return content_type is not None and content_type.endswith("xml")


def _archive_rule(entries: list[zipfile.ZipInfo]) -> str | None:
    """Rules 2.1 to 2.4, from the central directory alone. Declared sizes can lie, so the
    child's memory limit is the backstop."""
    if len(entries) > ZIP_MAX_ENTRIES:
        return "zip.entries"
    if sum(entry.file_size for entry in entries) > ZIP_MAX_BYTES:
        return "zip.size"
    for entry in entries:
        if entry.file_size > RATIO_ENTRY_MIN_BYTES and (
            entry.compress_size == 0 or entry.file_size / entry.compress_size > RATIO_MAX
        ):
            return "zip.ratio"
    if any(entry.flag_bits & _ENCRYPTED_FLAG for entry in entries):
        return "zip.encrypted"
    return None


def _read_package(archive: zipfile.ZipFile, entries: list[zipfile.ZipInfo]) -> _Package:
    """Read every XML part; raises `_UnreadableError` where a part can't be read or parsed."""
    package = _Package()
    # The content types decide which other parts are XML, so they are read first.
    for entry in entries:
        if _fold(entry.filename) == CONTENT_TYPES_PART:
            _read_part(archive, entry, package, kind="types")
    for entry in entries:
        folded = _fold(entry.filename)
        if folded == CONTENT_TYPES_PART or not _is_xml_part(entry.filename, package):
            continue
        kind = "rels" if folded == PACKAGE_RELATIONSHIPS_PART else "other"
        _read_part(archive, entry, package, kind=kind)
    return package


def _package_rule(
    entries: list[zipfile.ZipInfo], package: _Package, content_type: str
) -> str | None:
    """Rules 2.6 to 2.8 and rule 3, from the names and what the XML parts declare."""
    folded_names = [_fold(entry.filename) for entry in entries]
    if len(set(folded_names)) != len(folded_names):
        return "zip.duplicate"
    if any("macroenabled" in declared or declared == VBA_PROJECT for declared in package.declared):
        return "macro.type"
    if any(name.endswith("vbaproject.bin") for name in folded_names):
        return "macro.vba"
    present = {_part_name(entry.filename) for entry in entries}
    main = next((target for target in package.main_targets if target in present), None)
    expected = MAIN_CONTENT_TYPES.get(content_type)
    if main is None or expected is None or package.content_type(main) != expected:
        return "sig.type"
    return None


def first_broken_rule(archive: zipfile.ZipFile, content_type: str) -> str | None:
    """The label of the first rule of section 15.6 item 7, rules 2 and 3, that the open archive
    breaks, in their order, or `None`. `content_type` is the version's media type, whose main
    content type rule `sig.type` checks."""
    entries = archive.infolist()
    rule = _archive_rule(entries)
    if rule is not None:
        return rule
    try:
        package = _read_package(archive, entries)
    except _UnreadableError:
        return "zip.unreadable"
    return _package_rule(entries, package, content_type)


def _office_rule(path: Path, content_type: str) -> str | None:
    try:
        with zipfile.ZipFile(path) as archive:
            return first_broken_rule(archive, content_type)
    except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, ValueError, OSError):
        return "zip.unreadable"


def pdf_rule(path: Path) -> tuple[str | None, int | None]:
    """Rules `pdf.password` and `pdf.unreadable`, opened with no password, and the page count.

    A PDF with only an owner password opens and is accepted, because both the layout service and
    the parser read it.
    """
    try:
        with pikepdf.open(path) as pdf:
            return None, len(pdf.pages)
    except pikepdf.PasswordError:
        return "pdf.password", None
    except Exception:  # noqa: BLE001 - any other open failure of a hostile file is the rule
        return "pdf.unreadable", None


def inspect_document(path: Path, content_type: str) -> InspectOk | InspectFailed:
    """The `inspect` command: the rules in order, then the page count of a PDF. An Office
    source's result has `pageCount` null; its converted PDF is counted by its own `inspect`."""
    with path.open("rb") as source:
        head = source.read(PDF_SIGNATURE_WINDOW)
    if not signature_matches(head, content_type):
        return InspectFailed(ok=False, code=RULE_CODES["sig.mismatch"])
    if content_type in OFFICE_TYPES:
        rule = _office_rule(path, content_type)
        if rule is not None:
            return InspectFailed(ok=False, code=RULE_CODES[rule])
        return InspectOk(ok=True, pageCount=None)
    rule, page_count = pdf_rule(path)
    if rule is not None:
        return InspectFailed(ok=False, code=RULE_CODES[rule])
    return InspectOk(ok=True, pageCount=page_count)
