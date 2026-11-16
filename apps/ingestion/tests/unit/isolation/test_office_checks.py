"""`docchat_ingestion.isolation.office_checks`: the `inspect` checks of `14-ingestion.md` section
15.6 item 7, each rule on a package built here with one change (T-189, T-610)."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pikepdf
import pytest

from docchat_ingestion.isolation import office_checks
from docchat_ingestion.isolation.office_checks import first_broken_rule, inspect_document
from docchat_ingestion.isolation.results import DOCX, PDF, PPTX, InspectFailed, InspectOk

type PackageFactory = Callable[..., bytes]
type PdfFactory = Callable[..., bytes]

VBA = "application/vnd.ms-office.vbaProject"
MACRO_DOCX = "application/vnd.ms-word.document.macroEnabled.main+xml"
MACRO_PPTX = "application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml"
TEMPLATE_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml"
TEMPLATE_PPTX = "application/vnd.openxmlformats-officedocument.presentationml.template.main+xml"
SLIDESHOW = "application/vnd.openxmlformats-officedocument.presentationml.slideshow.main+xml"
STRICT_OFFICE_DOCUMENT = "http://purl.oclc.org/ooxml/officeDocument/relationships/officeDocument"


def rule(data: bytes, content_type: str = DOCX) -> str | None:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return first_broken_rule(archive, content_type)


def inspected(tmp_path: Path, data: bytes, content_type: str) -> InspectOk | InspectFailed:
    path = tmp_path / "source.bin"
    path.write_bytes(data)
    return inspect_document(path, content_type)


def test_t610_base_packages_pass(make_package: PackageFactory) -> None:
    assert rule(make_package("docx")) is None
    assert rule(make_package("pptx"), PPTX) is None


def test_t610_the_strict_relationship_namespace_passes(make_package: PackageFactory) -> None:
    assert rule(make_package(relationship_type=STRICT_OFFICE_DOCUMENT)) is None


@pytest.mark.parametrize(
    ("kind", "overrides", "defaults"),
    [
        ("docx", {"/word/vbaData.xml": VBA}, {}),
        ("pptx", {}, {"xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12"}),
    ],
)
def test_t610_a_declared_macro_type_is_refused(
    make_package: PackageFactory, kind: str, overrides: dict[str, str], defaults: dict[str, str]
) -> None:
    data = make_package(kind, overrides=overrides, defaults=defaults)

    assert rule(data, DOCX if kind == "docx" else PPTX) == "macro.type"


@pytest.mark.parametrize(("kind", "main"), [("docx", MACRO_DOCX), ("pptx", MACRO_PPTX)])
def test_t610_a_macro_enabled_package_renamed_is_refused(
    tmp_path: Path, make_package: PackageFactory, kind: str, main: str
) -> None:
    content_type = DOCX if kind == "docx" else PPTX
    data = make_package(kind, main_type=main)

    assert rule(data, content_type) == "macro.type"
    assert inspected(tmp_path, data, content_type) == InspectFailed(
        ok=False, code="macro_content_declared"
    )


def test_t610_a_vba_project_entry_without_a_declaration_is_refused(
    make_package: PackageFactory,
) -> None:
    assert rule(make_package(extra={"word/VBAPROJECT.BIN": b"\x00"})) == "macro.vba"


@pytest.mark.parametrize(
    ("kind", "content_type", "main"),
    [
        ("docx", DOCX, TEMPLATE_DOCX),
        ("pptx", PPTX, TEMPLATE_PPTX),
        ("pptx", PPTX, SLIDESHOW),
        ("pptx", DOCX, None),
    ],
)
def test_t610_another_type_renamed_fails_the_type_rule(
    tmp_path: Path, make_package: PackageFactory, kind: str, content_type: str, main: str | None
) -> None:
    data = make_package(kind, main_type=main)

    assert rule(data, content_type) == "sig.type"
    assert inspected(tmp_path, data, content_type) == InspectFailed(
        ok=False, code="file_signature_mismatch"
    )


def test_t610_a_package_without_an_office_document_relationship_fails(
    make_package: PackageFactory,
) -> None:
    data = make_package(relationship_type="http://example.invalid/other")

    assert rule(data) == "sig.type"


def test_an_external_main_part_or_a_missing_target_fails_the_type_rule(
    make_package: PackageFactory,
) -> None:
    external = (
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="r" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        b'relationships/officeDocument" Target="word/document.xml" TargetMode="External"/>'
        b"</Relationships>"
    )
    missing = external.replace(b' TargetMode="External"', b"").replace(b"word/document", b"x/y")

    assert rule(make_package(relationships=external)) == "sig.type"
    assert rule(make_package(relationships=missing)) == "sig.type"


def test_t610_the_xml_parse_expands_no_entity_and_opens_no_file(
    tmp_path: Path, make_package: PackageFactory
) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    xxe = (
        f'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file://{secret}">]><d>&x;</d>'
    ).encode()
    data = make_package(extra={"word/custom.xml": xxe})

    # The part parses with the entity left unexpanded; the package passes as any other.
    assert rule(data) is None
    events = list(office_checks._iterparse(io.BytesIO(xxe)))
    assert all("secret" not in (element.text or "") for _event, element in events)


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"extra": {"word/broken.xml": b"<a><b></a>"}}, "zip.unreadable"),
        ({"extra": {"word/_rels/document.xml.rels": b"not xml"}}, "zip.unreadable"),
        ({"content_types": b"<Types"}, "zip.unreadable"),
        ({"encrypt": "word/document.xml"}, "zip.encrypted"),
    ],
)
def test_t189_archive_rules_fail_with_their_labels(
    make_package: PackageFactory, change: dict[str, object], expected: str
) -> None:
    assert rule(make_package(**change)) == expected


def test_t189_a_part_whose_content_type_ends_in_xml_is_parsed(
    make_package: PackageFactory,
) -> None:
    data = make_package(
        overrides={"/word/data.bin": "application/vnd.example+xml"},
        extra={"word/data.bin": b"<unclosed>"},
    )

    assert rule(data) == "zip.unreadable"


def test_t189_two_names_equal_case_insensitively_are_refused(
    make_package: PackageFactory,
) -> None:
    buffer = io.BytesIO(make_package())
    with zipfile.ZipFile(buffer, "a") as archive:
        archive.writestr("WORD/document.xml", b"<w/>")

    assert rule(buffer.getvalue()) == "zip.duplicate"


def test_t189_entry_count_size_and_ratio_caps() -> None:
    class CentralDirectory:
        """The rules of 2.1 to 2.4 read only the central directory's records."""

        def __init__(self, entries: list[zipfile.ZipInfo]) -> None:
            self.entries = entries

        def infolist(self) -> list[zipfile.ZipInfo]:
            return self.entries

    def archive_with(entries: list[zipfile.ZipInfo]) -> zipfile.ZipFile:
        return cast(zipfile.ZipFile, CentralDirectory(entries))

    many = [zipfile.ZipInfo(f"slide{index}.xml") for index in range(10_001)]
    big = zipfile.ZipInfo("big.xml")
    big.file_size = 536_870_913
    big.compress_size = 536_870_913
    bomb = zipfile.ZipInfo("bomb.xml")
    bomb.file_size = 10 * 1024 * 1024 + 1
    bomb.compress_size = bomb.file_size // 201
    empty = zipfile.ZipInfo("empty.xml")
    empty.file_size = 10 * 1024 * 1024 + 1
    empty.compress_size = 0
    fair = zipfile.ZipInfo("fair.xml")
    fair.file_size = 10 * 1024 * 1024 + 1
    fair.compress_size = fair.file_size // 100

    assert first_broken_rule(archive_with(many), DOCX) == "zip.entries"
    assert first_broken_rule(archive_with([big]), DOCX) == "zip.size"
    assert first_broken_rule(archive_with([bomb]), DOCX) == "zip.ratio"
    assert first_broken_rule(archive_with([empty]), DOCX) == "zip.ratio"
    assert office_checks._archive_rule([fair]) is None


def test_t189_a_real_single_entry_bomb_is_refused(
    tmp_path: Path, make_package: PackageFactory
) -> None:
    data = make_package(extra={"word/bomb.xml": b"<a>" + b" " * (11 * 1024 * 1024) + b"</a>"})

    assert inspected(tmp_path, data, DOCX) == InspectFailed(
        ok=False, code="zip_size_limit_exceeded"
    )


def test_t189_an_unreadable_archive_is_corrupt(tmp_path: Path) -> None:
    assert inspected(tmp_path, b"PK\x03\x04 this is no archive", DOCX) == InspectFailed(
        ok=False, code="corrupt_document"
    )


@pytest.mark.parametrize(
    ("data", "content_type"),
    [
        (b"PK\x03\x04", PDF),
        (b"no signature at all", PDF),
        (b"%PDF-1.7", DOCX),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", PPTX),
        (b"%PDF-1.7", "text/plain"),
    ],
)
def test_t189_a_signature_that_doesnt_match_the_type_fails_first(
    tmp_path: Path, data: bytes, content_type: str
) -> None:
    assert inspected(tmp_path, data, content_type) == InspectFailed(
        ok=False, code="file_signature_mismatch"
    )


def test_the_pdf_signature_may_sit_within_the_first_kilobyte() -> None:
    assert office_checks.signature_matches(b" " * 1000 + b"%PDF-1.4", PDF)
    assert not office_checks.signature_matches(b" " * 1020 + b"%PDF-1.4", PDF)


def test_t189_an_office_source_passes_with_a_null_page_count(
    tmp_path: Path, make_package: PackageFactory
) -> None:
    assert inspected(tmp_path, make_package(), DOCX) == InspectOk(ok=True, pageCount=None)


def test_t189_a_pdf_passes_with_its_page_count(tmp_path: Path, make_pdf: PdfFactory) -> None:
    assert inspected(tmp_path, make_pdf(pages=3), PDF) == InspectOk(ok=True, pageCount=3)


def test_t189_a_pdf_that_needs_a_password_is_encrypted_and_owner_only_passes(
    tmp_path: Path,
) -> None:
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page()
    user, owner_only = io.BytesIO(), io.BytesIO()
    pdf.save(user, encryption=pikepdf.Encryption(owner="owner", user="user"))
    pdf.save(owner_only, encryption=pikepdf.Encryption(owner="owner", user=""))

    assert inspected(tmp_path, user.getvalue(), PDF) == InspectFailed(
        ok=False, code="encrypted_document"
    )
    assert inspected(tmp_path, owner_only.getvalue(), PDF) == InspectOk(ok=True, pageCount=1)


def test_t189_an_unreadable_pdf_is_corrupt(tmp_path: Path) -> None:
    assert inspected(tmp_path, b"%PDF-1.7\nnothing else", PDF) == InspectFailed(
        ok=False, code="corrupt_document"
    )


def test_every_rule_maps_to_an_inspect_code() -> None:
    assert set(office_checks.RULE_CODES.values()) <= {
        "file_signature_mismatch",
        "zip_entry_limit_exceeded",
        "zip_size_limit_exceeded",
        "encrypted_document",
        "corrupt_document",
        "macro_content_declared",
    }
