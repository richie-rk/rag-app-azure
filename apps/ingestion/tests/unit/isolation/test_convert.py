"""`docchat_ingestion.isolation.convert`: the Office conversion's profile, converter version and
command line (`14-ingestion.md` section 15.11; T-612, T-613).

The pinned LibreOffice 7.4.7 runs only in the ingestion image; here the installed one, when there
is one, converts a package built in the test without the filter, which proves the command line
and the rename. Whether LibreOffice converts under the system-call filter is the converter-sandbox
spike's to decide (section 15.11 item 7).
"""

from __future__ import annotations

import hashlib
import importlib
import io
from collections.abc import Callable
from pathlib import Path

import pikepdf
import pytest

from docchat_ingestion.isolation import convert
from docchat_ingestion.isolation.results import DOCX, PDF, PPTX, ConvertFailed, Ok

type PackageFactory = Callable[..., bytes]

SPEC = Path(__file__).resolve().parents[5] / "docs" / "spec" / "14-ingestion.md"


def profile_in_the_spec() -> bytes:
    """The `registrymodifications.xcu` block of section 15.11 item 3, byte for byte."""
    lines = SPEC.read_text(encoding="utf-8").splitlines()
    start = lines.index("   ```xml") + 1
    end = lines.index("   ```", start)
    return ("\n".join(line.removeprefix("   ") for line in lines[start:end]) + "\n").encode()


def test_t612_the_profile_equals_the_spec_byte_for_byte() -> None:
    assert profile_in_the_spec() == convert.PROFILE


def test_t613_the_converter_version_names_the_package_and_the_profile() -> None:
    digest = hashlib.sha256(convert.PROFILE).hexdigest()[:12]

    assert f"lo-4:7.4.7-1+deb12u14/{digest}" == convert.CONVERTER_VERSION
    assert len(convert.CONVERTER_VERSION) <= 34
    assert convert.LIBREOFFICE_PACKAGE_VERSION == "4:7.4.7-1+deb12u14"


def test_t613_the_converter_version_is_the_same_across_imports() -> None:
    first = convert.CONVERTER_VERSION

    assert first == importlib.reload(convert).CONVERTER_VERSION


def test_t613_one_profile_byte_changes_the_converter_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = list(convert._PROFILE_ITEMS)
    path, name, _value = items[-1]
    items[-1] = (path, name, "true")
    monkeypatch.setattr(convert, "_PROFILE_ITEMS", tuple(items))
    changed = convert._profile()

    assert changed != convert.PROFILE
    assert hashlib.sha256(changed).hexdigest()[:12] != convert.CONVERTER_VERSION[-12:]


@pytest.mark.parametrize(
    ("content_type", "import_filter", "export_filter"),
    [
        (DOCX, "MS Word 2007 XML", "pdf:writer_pdf_Export"),
        (PPTX, "Impress MS PowerPoint 2007 XML", "pdf:impress_pdf_Export"),
    ],
)
def test_t613_the_command_names_both_filters_and_passes_no_filter_options(
    content_type: str, import_filter: str, export_filter: str
) -> None:
    scratch = Path("/run/docchat-scratch/docchat-x")

    assert convert.soffice_command(scratch, content_type) == [
        "/usr/lib/libreoffice/program/soffice",
        "--headless",
        "--norestore",
        "--nologo",
        "--nodefault",
        "--nolockcheck",
        "-env:UserInstallation=file:///run/docchat-scratch/docchat-x/lo-profile",
        f"--infilter={import_filter}",
        "--convert-to",
        export_filter,
        "--outdir",
        "/run/docchat-scratch/docchat-x/out",
        "source.bin",
    ]


def stub(tmp_path: Path, body: str) -> str:
    path = tmp_path / "soffice-stub"
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)
    return str(path)


def test_the_child_renames_the_output_to_converted_pdf(tmp_path: Path) -> None:
    soffice = stub(tmp_path, "mkdir -p out && printf '%%PDF-1.7' > out/source.pdf")

    assert convert.convert_in_child(tmp_path, DOCX, soffice) == Ok(ok=True)
    assert (tmp_path / "converted.pdf").read_bytes() == b"%PDF-1.7"
    assert not (tmp_path / "out" / "source.pdf").exists()


@pytest.mark.parametrize(
    "body",
    [
        "exit 1",
        "mkdir -p out && ln -s /etc/hostname out/source.pdf",
        "mkdir -p out/source.pdf",
    ],
)
def test_t612_no_output_a_link_or_a_directory_is_convert_failed(tmp_path: Path, body: str) -> None:
    soffice = stub(tmp_path, body)

    assert convert.convert_in_child(tmp_path, DOCX, soffice) == ConvertFailed(
        ok=False, code="convert_failed"
    )


def test_a_type_without_filters_is_convert_failed(tmp_path: Path) -> None:
    assert convert.convert_in_child(tmp_path, PDF) == ConvertFailed(ok=False, code="convert_failed")


def test_t613_the_update_check_library_is_found(tmp_path: Path) -> None:
    (tmp_path / "program").mkdir()

    assert not convert.update_check_present(tmp_path)
    (tmp_path / "program" / "libupdchklo.so").write_bytes(b"")
    assert convert.update_check_present(tmp_path)


@pytest.mark.skipif(
    not Path(convert.SOFFICE).exists(), reason="LibreOffice isn't installed at /usr/lib/libreoffice"
)
def test_t612_the_installed_converter_converts_with_the_profile(
    tmp_path: Path, make_package: PackageFactory
) -> None:
    (tmp_path / "source.bin").write_bytes(make_package("docx", text="CONVERTED MARKER TEXT"))
    (tmp_path / convert.PROFILE_USER_DIRECTORY).mkdir(parents=True)
    (tmp_path / convert.OUTPUT_DIRECTORY).mkdir()
    (tmp_path / convert.PROFILE_FILE).write_bytes(convert.PROFILE)

    assert convert.convert_in_child(tmp_path, DOCX) == Ok(ok=True)
    with pikepdf.open(io.BytesIO((tmp_path / "converted.pdf").read_bytes())) as pdf:
        assert len(pdf.pages) == 1
        keys = set(pdf.Root.keys())
        assert "/AcroForm" not in keys
        assert "/Outlines" not in keys
