"""The fixture documents of `84-testing.md` section 1.5 and their generator.

`tests/fixtures/documents/generate.py` wrote every committed document it names; they change only
through a reviewed regeneration. This test regenerates them in memory and fails on any byte that
differs, then checks the properties section 1.5 states (page, table, slide and note counts, the
image-only scanned pages at 200 dpi, sizes under 1 MB) and the one construct each archive,
converter and rendition schema fixture carries (`14-ingestion.md` sections 14, 15.6 and 15.11).
"""

from __future__ import annotations

import importlib.util
import io
import re
import sys
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import docx
import pikepdf
import pptx
import pytest
from lxml import etree

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from types import ModuleType

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[3]
DOCUMENTS: Final = REPOSITORY_ROOT / "tests" / "fixtures" / "documents"
ONE_MB: Final = 1_000_000
LARGE_FILE_LIMIT: Final = 1024 * 1024  # pre-commit's check-added-large-files --maxkb=1024
SHARED: Final = ("tables.pdf", "scanned.pdf", "headings.docx", "slides.pptx")
# Files of the directory that the generator doesn't write (section 1.5): the six controls saved
# by Office and the indirect-attack document of the deployed screening test.
NOT_GENERATED: Final = frozenset(
    {
        "office-base.docx",
        "office-base.pptx",
        "office-base-chart.pptx",
        "office-base-strict.docx",
        "office-base-strict.pptx",
        "office-base-chart-strict.pptx",
        "indirect-attack.pdf",
    }
)


def _load_generator() -> ModuleType:
    name = "docchat_fixture_documents_generate"
    spec = importlib.util.spec_from_file_location(name, DOCUMENTS / "generate.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


generate = _load_generator()


@pytest.fixture(scope="module")
def built() -> dict[str, bytes]:
    result: dict[str, bytes] = generate.build()
    return result


def _pdf(data: bytes, password: str = "") -> pikepdf.Pdf:
    return pikepdf.open(io.BytesIO(data), password=password, attempt_recovery=False)


def _archive(data: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(data))


def _content(page: pikepdf.Page) -> bytes:
    contents = page.obj.Contents
    if isinstance(contents, pikepdf.Array):
        return b"".join(stream.read_bytes() for stream in contents)
    return bytes(contents.read_bytes())


# --- Regeneration -----------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(generate.FIXTURES))
def test_regenerating_writes_the_committed_bytes(built: dict[str, bytes], name: str) -> None:
    committed = DOCUMENTS / name
    assert committed.is_file(), f"{name} is missing; run tests/fixtures/documents/generate.py"
    assert committed.read_bytes() == built[name], (
        f"{name} differs from what generate.py writes; regenerate it in a reviewed change"
    )


def test_every_document_beside_the_generator_is_one_it_writes_or_a_named_exception() -> None:
    documents = {
        path.name for path in DOCUMENTS.iterdir() if path.suffix in {".pdf", ".docx", ".pptx"}
    }

    assert documents - set(generate.FIXTURES) - NOT_GENERATED == set()


def test_two_builds_give_the_same_bytes(built: dict[str, bytes]) -> None:
    generate._DEFLATE_CACHE.clear()
    names = ("headings.docx", "slides.pptx", "base-chart.pptx", "scanned.pdf", "owner-only.pdf")

    assert generate.build(names) == {name: built[name] for name in names}


def test_every_file_fits_the_repository_size_limit(built: dict[str, bytes]) -> None:
    oversized = {name: len(data) for name, data in built.items() if len(data) > LARGE_FILE_LIMIT}

    assert oversized == {}


# --- The four shared documents ----------------------------------------------------------------


@pytest.mark.parametrize("name", SHARED)
def test_each_shared_document_is_under_1_mb(built: dict[str, bytes], name: str) -> None:
    assert len(built[name]) < ONE_MB


def test_tables_pdf_has_12_pages_and_4_tables_one_spanning_two_pages_one_with_merged_headers(
    built: dict[str, bytes],
) -> None:
    with _pdf(built["tables.pdf"]) as pdf:
        pages = [_content(page) for page in pdf.pages]
        fonts = [set(page.Resources.Font.keys()) for page in pdf.pages]

    captions = {
        number: [index + 1 for index, data in enumerate(pages) if b"(Table %d." % number in data]
        for number in range(1, 5)
    }
    continued = [index + 1 for index, data in enumerate(pages) if b"\\(continued\\)" in data]
    assert len(pages) == 12
    assert captions == {1: [2], 2: [4], 3: [6], 4: [10]}
    assert continued == [7]
    assert b"(Deliveries by quarter \\(ML\\))" in pages[3]
    assert all(f"({quarter})".encode() in pages[3] for quarter in ("Q1", "Q2", "Q3", "Q4"))
    assert all(page_fonts == {"/F1", "/F2"} for page_fonts in fonts)


def test_scanned_pdf_has_6_image_only_pages_at_200_dpi_and_no_text_layer(
    built: dict[str, bytes],
) -> None:
    with _pdf(built["scanned.pdf"]) as pdf:
        assert len(pdf.pages) == 6
        for page in pdf.pages:
            resources = page.Resources
            assert "/Font" not in resources
            (image,) = resources.XObject.values()
            width_points = float(page.MediaBox[2]) - float(page.MediaBox[0])
            height_points = float(page.MediaBox[3]) - float(page.MediaBox[1])
            assert image.Subtype == "/Image"
            assert int(image.BitsPerComponent) == 1
            assert int(image.Width) * 72 / width_points == 200
            assert int(image.Height) * 72 / height_points == 200
            operators = {str(item.operator) for item in pikepdf.parse_content_stream(page)}
            assert operators == {"q", "cm", "Do", "Q"}
            assert _content(page).strip() == b"q 612 0 0 792 0 0 cm /Im1 Do Q"


def test_scanned_pages_carry_typed_text_for_ocr(built: dict[str, bytes]) -> None:
    with _pdf(built["scanned.pdf"]) as pdf:
        inked = []
        for page in pdf.pages:
            (image,) = page.Resources.XObject.values()
            raw = image.read_bytes()
            inked.append(sum(bin(255 - byte).count("1") for byte in raw) / (len(raw) * 8))

    assert all(0.005 < share < 0.2 for share in inked), inked


def test_headings_docx_has_three_heading_levels_18_sections_and_2_tables(
    built: dict[str, bytes],
) -> None:
    document = docx.Document(io.BytesIO(built["headings.docx"]))
    levels = [
        int(paragraph.style.name.removeprefix("Heading "))
        for paragraph in document.paragraphs
        if paragraph.style is not None
        and paragraph.style.name is not None
        and paragraph.style.name.startswith("Heading ")
    ]

    assert {level: levels.count(level) for level in (1, 2, 3)} == {1: 3, 2: 6, 3: 9}
    assert len(levels) == 18
    assert len(document.tables) == 2


def test_slides_pptx_has_10_slides_2_with_tables_and_notes_on_3(built: dict[str, bytes]) -> None:
    deck = pptx.Presentation(io.BytesIO(built["slides.pptx"]))
    slides = list(deck.slides)
    with_tables = [
        number
        for number, slide in enumerate(slides, start=1)
        if any(shape.has_table for shape in slide.shapes)
    ]
    with_notes = [
        number
        for number, slide in enumerate(slides, start=1)
        if slide.has_notes_slide
        and slide.notes_slide.notes_text_frame is not None
        and slide.notes_slide.notes_text_frame.text.strip()
    ]

    assert len(slides) == 10
    assert with_tables == [4, 7]
    assert with_notes == [2, 4, 9]


# --- The passing base files -------------------------------------------------------------------


def test_base_docx_has_one_heading_one_paragraph_one_https_link_a_header_and_an_xlsx_object(
    built: dict[str, bytes],
) -> None:
    document = docx.Document(io.BytesIO(built["base.docx"]))
    body = [paragraph for paragraph in document.paragraphs if paragraph.text.strip()]
    rels = document.part.rels.values()
    with _archive(built["base.docx"]) as archive:
        names = archive.namelist()
        main = archive.read("word/document.xml").decode()

    assert [paragraph.style.name for paragraph in body if paragraph.style] == [
        "Heading 1",
        "Normal",
    ]
    assert [rel.target_ref for rel in rels if rel.is_external] == [generate.BASE_LINK]
    assert any(name.startswith("word/header") for name in names)
    assert "word/embeddings/Microsoft_Excel_Worksheet.xlsx" in names
    assert 'ProgID="Excel.Sheet.12"' in main
    assert 'Type="Embed"' in main


def test_base_pptx_has_two_slides_one_with_notes(built: dict[str, bytes]) -> None:
    slides = list(pptx.Presentation(io.BytesIO(built["base.pptx"])).slides)

    assert len(slides) == 2
    assert [slide.has_notes_slide for slide in slides] == [False, True]


def test_base_chart_pptx_has_one_slide_with_a_chart_whose_data_is_an_embedded_xlsx(
    built: dict[str, bytes],
) -> None:
    (slide,) = pptx.Presentation(io.BytesIO(built["base-chart.pptx"])).slides
    (chart,) = [shape for shape in slide.shapes if shape.has_chart]
    with _archive(built["base-chart.pptx"]) as archive:
        workbooks = [name for name in archive.namelist() if name.endswith(".xlsx")]

    assert chart.chart.plots[0].series[0].values == (1250, 1480, 1710, 1390)
    assert len(workbooks) == 1


def test_base_pdf_is_one_page_of_text_and_owner_only_pdf_opens_without_a_password(
    built: dict[str, bytes],
) -> None:
    with _pdf(built["base.pdf"]) as base, _pdf(built["owner-only.pdf"]) as owner_only:
        assert len(base.pages) == 1
        assert b"Tj" in _content(base.pages[0])
        assert owner_only.is_encrypted
        assert _content(owner_only.pages[0]) == _content(base.pages[0])
    with pytest.raises(pikepdf.PasswordError):
        _pdf(built["encrypted.pdf"])
    with _pdf(built["encrypted.pdf"], password=generate.PASSWORDS["encrypted.pdf"]) as pdf:
        assert len(pdf.pages) == 1


# --- The archive and document check fixtures (T-189) and the type check fixtures (T-610) ------


def _content_types(archive: zipfile.ZipFile) -> str:
    return archive.read("[Content_Types].xml").decode()


def test_bomb_docx_is_a_single_entry_that_declares_more_than_512_mib(
    built: dict[str, bytes],
) -> None:
    with _archive(built["bomb.docx"]) as archive:
        (bomb,) = [info for info in archive.infolist() if info.file_size > 10 * 1024 * 1024]
        total = sum(info.file_size for info in archive.infolist())
        with archive.open(bomb) as stream:
            head = stream.read(1 << 20)

    assert total > 536_870_912
    assert bomb.file_size / bomb.compress_size > 1000
    assert head == bytes(1 << 20)


def test_ratio_docx_declares_a_ratio_above_200_on_one_entry_above_10_mib(
    built: dict[str, bytes],
) -> None:
    with _archive(built["ratio.docx"]) as archive:
        big = [info for info in archive.infolist() if info.file_size > 10 * 1024 * 1024]
        total = sum(info.file_size for info in archive.infolist())
        assert archive.read(big[0]) == bytes(big[0].file_size)

    assert len(big) == 1
    assert big[0].file_size / big[0].compress_size > 200
    assert total <= 536_870_912


def test_many_entries_pptx_holds_more_than_10000_entries(built: dict[str, bytes]) -> None:
    with _archive(built["many-entries.pptx"]) as archive:
        assert len(archive.infolist()) > 10_000


def test_encrypted_entry_docx_flags_its_main_part_as_encrypted(built: dict[str, bytes]) -> None:
    with _archive(built["encrypted-entry.docx"]) as archive:
        flagged = [info.filename for info in archive.infolist() if info.flag_bits & 1]
        main = archive.read("word/document.xml", pwd=b"docchat")

    assert flagged == ["word/document.xml"]
    assert main.startswith(b"<?xml")


def test_unreadable_archive_docx_cant_be_read_by_zipfile(built: dict[str, bytes]) -> None:
    assert built["unreadable-archive.docx"].startswith(b"PK\x03\x04")
    with pytest.raises(zipfile.BadZipFile):
        _archive(built["unreadable-archive.docx"])


def test_duplicate_names_docx_holds_two_names_equal_case_insensitively(
    built: dict[str, bytes],
) -> None:
    with _archive(built["duplicate-names.docx"]) as archive:
        names = [name.lower() for name in archive.namelist()]

    assert [name for name in set(names) if names.count(name) > 1] == ["word/document.xml"]


@pytest.mark.parametrize(
    ("name", "declared"),
    [
        ("macro.docm-as.docx", "application/vnd.ms-word.document.macroEnabled.main+xml"),
        ("macro.pptm-as.pptx", "application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml"),
        ("vba-override.docx", "application/vnd.ms-office.vbaProject"),
        ("xlsm-default.pptx", "application/vnd.ms-excel.sheet.macroEnabled.12"),
    ],
)
def test_each_macro_type_fixture_declares_a_macro_content_type(
    built: dict[str, bytes], name: str, declared: str
) -> None:
    with _archive(built[name]) as archive:
        assert f'ContentType="{declared}"' in _content_types(archive)


def test_vbaproject_entry_docx_holds_an_upper_case_vba_project_with_no_content_type(
    built: dict[str, bytes],
) -> None:
    with _archive(built["vbaproject-entry.docx"]) as archive:
        assert "word/VBAPROJECT.BIN" in archive.namelist()
        assert "vbaproject" not in _content_types(archive).lower()


@pytest.mark.parametrize(
    ("name", "main_type"),
    [
        ("template.dotx-as.docx", "wordprocessingml.template.main+xml"),
        ("template.potx-as.pptx", "presentationml.template.main+xml"),
        ("show.ppsx-as.pptx", "presentationml.slideshow.main+xml"),
        ("deck.pptx-as.docx", "presentationml.presentation.main+xml"),
    ],
)
def test_each_signature_fixture_has_another_main_content_type(
    built: dict[str, bytes], name: str, main_type: str
) -> None:
    with _archive(built[name]) as archive:
        assert f"application/vnd.openxmlformats-officedocument.{main_type}" in (
            _content_types(archive)
        )


def test_no_office_document_docx_names_no_office_document_relationship(
    built: dict[str, bytes],
) -> None:
    with _archive(built["no-office-document.docx"]) as archive:
        assert not re.search(r'Type="[^"]*/officeDocument"', archive.read("_rels/.rels").decode())


@pytest.mark.parametrize(
    ("name", "rels", "construct"),
    [
        ("external-template.docx", "word/_rels/settings.xml.rels", "/attachedTemplate"),
        ("remote-image.pptx", "ppt/slides/_rels/slide1.xml.rels", "/image"),
    ],
)
def test_each_external_fixture_names_an_external_target(
    built: dict[str, bytes], name: str, rels: str, construct: str
) -> None:
    with _archive(built[name]) as archive:
        text = archive.read(rels).decode()

    assert re.search(rf'Type="[^"]*{construct}" Target="https://[^"]+" TargetMode="External"', text)


def test_dde_field_and_ole_package_fixtures_carry_their_constructs(
    built: dict[str, bytes],
) -> None:
    with _archive(built["dde-field.docx"]) as archive:
        assert "DDEAUTO Excel" in archive.read("word/document.xml").decode()
    with _archive(built["ole-package.docx"]) as archive:
        package = archive.read("word/embeddings/oleObject1.bin")
        main = archive.read("word/document.xml").decode()

    assert package.startswith(bytes.fromhex("D0CF11E0A1B11AE1"))
    assert "\x01Ole10Native".encode("utf-16-le") in package
    assert 'ProgID="Package"' in main


def test_blank_scan_pdf_is_one_white_image_only_page(built: dict[str, bytes]) -> None:
    with _pdf(built["blank-scan.pdf"]) as pdf:
        (page,) = pdf.pages
        (image,) = page.Resources.XObject.values()
        assert "/Font" not in page.Resources
        assert set(image.read_bytes()) == {0xFF}


# --- The converter fixtures (14-ingestion.md section 15.11 item 6) ----------------------------


def test_include_fields_docx_names_the_host_name_and_the_canary_in_both_fields(
    built: dict[str, bytes],
) -> None:
    with _archive(built["include-fields.docx"]) as archive:
        main = archive.read("word/document.xml").decode()
    instructions = re.findall(r"<w:instrText[^>]*>([^<]*)</w:instrText>", main)

    assert sorted(" ".join(item.split()) for item in instructions) == sorted(
        f'{field} "{target}"'
        for field in ("INCLUDETEXT", "INCLUDEPICTURE")
        for target in (generate.HOSTNAME_URL, generate.CANARY_URL)
    )


@pytest.mark.parametrize(
    ("name", "rels", "kind", "targets"),
    [
        (
            "linked-section.docx",
            "word/_rels/document.xml.rels",
            "subDocument",
            ["file:///etc/hostname", "file:///run/docchat-scratch/docchat-sibling/canary.txt"],
        ),
        (
            "linked-image-unc.docx",
            "word/_rels/document.xml.rels",
            "image",
            ["file:///\\\\host\\share\\a.png"],
        ),
        (
            "linked-image-http.pptx",
            "ppt/slides/_rels/slide1.xml.rels",
            "image",
            ["http://127.0.0.1:18080/linked-image.png"],
        ),
        (
            "ole-link.docx",
            "word/_rels/document.xml.rels",
            "oleObject",
            ["file:///etc/hostname", "file:///run/docchat-scratch/docchat-sibling/canary.txt"],
        ),
        (
            "ole-link.pptx",
            "ppt/slides/_rels/slide1.xml.rels",
            "oleObject",
            ["file:///etc/hostname"],
        ),
    ],
)
def test_each_linked_content_fixture_links_its_local_or_remote_targets(
    built: dict[str, bytes], name: str, rels: str, kind: str, targets: list[str]
) -> None:
    with _archive(built[name]) as archive:
        text = archive.read(rels).decode()
    external = re.findall(rf'Type="[^"]*/{kind}" Target="([^"]+)" TargetMode="External"', text)

    assert external == targets


def test_xxe_docx_declares_an_external_entity_that_the_inspect_parser_never_expands(
    built: dict[str, bytes],
) -> None:
    declaration = b'<!ENTITY host SYSTEM "file:///etc/hostname">'
    titles = []
    with _archive(built["xxe.docx"]) as archive:
        declaring = [name for name in archive.namelist() if declaration in archive.read(name)]
        for name in archive.namelist():
            if name.endswith((".xml", ".rels")):
                with archive.open(name) as stream:
                    titles += [
                        element
                        for _, element in etree.iterparse(
                            stream, resolve_entities=False, no_network=True, huge_tree=False
                        )
                        if element.tag == "{http://purl.org/dc/elements/1.1/}title"
                    ]

    assert sorted(declaring) == ["docProps/core.xml", "word/document.xml"]
    (title,) = titles
    assert title.text is None
    assert [(child.tag, child.name) for child in title] == [(etree.Entity, "host")]


@pytest.mark.parametrize(
    ("name", "part", "rels", "macro"),
    [
        ("vba-autoopen.docx", "word/vbaProject.bin", "word/_rels/document.xml.rels", "AutoOpen"),
        ("vba-autoopen.pptx", "ppt/vbaProject.bin", "ppt/_rels/presentation.xml.rels", "Auto_Open"),
    ],
)
def test_each_vba_fixture_is_a_document_type_package_carrying_an_auto_macro(
    built: dict[str, bytes], name: str, part: str, rels: str, macro: str
) -> None:
    with _archive(built[name]) as archive:
        project = archive.read(part)
        types = _content_types(archive)
        relationships = archive.read(rels).decode()

    assert project.startswith(bytes.fromhex("D0CF11E0A1B11AE1"))
    assert "macroEnabled" not in types
    assert "vbaProject" not in types
    assert 'Type="http://schemas.microsoft.com/office/2006/relationships/vbaProject"' in (
        relationships
    )
    assert macro in _vba_sources(project)


def _vba_sources(project: bytes) -> str:
    """The decompressed module sources of a VBA project this generator wrote, whose streams
    hold literal-only containers ([MS-OVBA] 2.4.1)."""
    text = []
    for start in (
        match.start() for match in re.finditer(rb"\x01..\x00Attribut", project, re.DOTALL)
    ):
        size = (int.from_bytes(project[start + 1 : start + 3], "little") & 0x0FFF) + 1
        body = project[start + 3 : start + 3 + size]
        text.append(b"".join(body[index + 1 : index + 9] for index in range(0, len(body), 9)))
    return b"".join(text).decode("cp1252")


# --- The rendition schema, limit, inline dictionary and whole-document fixtures ---------------


def _single_page(built: dict[str, bytes], name: str) -> Iterator[pikepdf.Page]:
    with _pdf(built[name]) as pdf:
        assert len(pdf.pages) == 1
        yield pdf.pages[0]


def _presteps(page: pikepdf.Page) -> bool:
    action = page.obj.PresSteps.NA
    return bool(action.S == "/GoToR" and "/Type" not in action)


def _gotoe(page: pikepdf.Page) -> bool:
    return bool(page.obj.X1.S == "/GoToE")


def _page_af(page: pikepdf.Page) -> bool:
    (spec,) = page.obj.AF
    return "/EF" in spec and "/Type" not in spec


def _alternates(page: pikepdf.Page) -> bool:
    return "/Alternates" in page.Resources.XObject.Im1


def _form_ref(page: pikepdf.Page) -> bool:
    form = page.Resources.XObject.Fm1
    return bool(form.Subtype == "/Form" and str(form.Ref.F) == "other.pdf" and form.Ref.Page == 0)


def _stream_f(page: pikepdf.Page) -> bool:
    image = page.Resources.XObject.Im1
    return str(image.F) == "x.bin" and len(image.read_raw_bytes()) > 0


def _font_unlisted(page: pikepdf.Page) -> bool:
    return bool(page.Resources.Font.F1.X1.S == "/URI")


def _annotation_ap(page: pikepdf.Page) -> bool:
    subtypes = [str(annotation.Subtype) for annotation in page.obj.Annots]
    appearances = [annotation for annotation in page.obj.Annots if "/AP" in annotation]
    return subtypes == ["/Square", "/Widget", "/Link"] and len(appearances) == 2


def _resources_unknown(page: pikepdf.Page) -> bool:
    unknown = page.Resources.X1
    return bool(unknown.S == "/Launch" and str(unknown.F) == "calc.exe")


@pytest.mark.parametrize(
    ("name", "construct"),
    [
        ("schema-presteps-gotor.pdf", _presteps),
        ("schema-gotoe-unlisted.pdf", _gotoe),
        ("schema-page-af.pdf", _page_af),
        ("schema-image-alternates.pdf", _alternates),
        ("schema-form-ref.pdf", _form_ref),
        ("schema-stream-f.pdf", _stream_f),
        ("schema-font-unlisted.pdf", _font_unlisted),
        ("schema-annotation-ap.pdf", _annotation_ap),
        ("schema-resources-unknown.pdf", _resources_unknown),
    ],
)
def test_each_rendition_schema_fixture_is_one_page_of_text_with_its_construct(
    built: dict[str, bytes], name: str, construct: Callable[[pikepdf.Page], bool]
) -> None:
    for page in _single_page(built, name):
        assert b"Tj" in _content(page)
        assert construct(page)


def test_active_content_pdf_carries_each_action_and_annotation_type(
    built: dict[str, bytes],
) -> None:
    with _pdf(built["active-content.pdf"]) as pdf:
        catalogue = pdf.Root
        annotations = [annotation for page in pdf.pages for annotation in page.obj.Annots]
        subtypes = {str(annotation.Subtype) for annotation in annotations}
        actions = {str(item.S) for item in _dictionaries(pdf) if "/S" in item}
        beads = [page.obj.B for page in pdf.pages]

    assert {"/Names", "/PageMode", "/URI", "/OpenAction", "/Outlines", "/AcroForm"} <= set(
        catalogue.keys()
    )
    assert {"/JavaScript", "/EmbeddedFiles"} <= set(catalogue.Names.keys())
    assert subtypes == {
        "/Link",
        "/Widget",
        "/FileAttachment",
        "/Screen",
        "/Movie",
        "/Sound",
        "/3D",
        "/RichMedia",
    }
    assert {
        "/JavaScript",
        "/SubmitForm",
        "/ImportData",
        "/ResetForm",
        "/Launch",
        "/URI",
        "/GoToR",
        "/GoToE",
        "/Rendition",
        "/Sound",
        "/Movie",
        "/Hide",
        "/Named",
        "/RichMediaExecute",
        "/GoTo",
        "/Thread",
    } <= actions
    assert all(len(page_beads) == 1 for page_beads in beads)
    assert "/Next" in catalogue.OpenAction
    assert any("/PA" in annotation and "/Dest" in annotation for annotation in annotations)


def _dictionaries(pdf: pikepdf.Pdf) -> Iterator[pikepdf.Dictionary]:
    seen: set[tuple[int, int]] = set()
    pending: list[Any] = [pdf.Root, *pdf.objects]
    while pending:
        item = pending.pop()
        if not isinstance(item, pikepdf.Object):
            continue
        if item.is_indirect:
            if item.objgen in seen:
                continue
            seen.add(item.objgen)
        if isinstance(item, pikepdf.Dictionary | pikepdf.Stream):
            if isinstance(item, pikepdf.Dictionary):
                yield item
            pending += [value for _, value in item.items()]
        elif isinstance(item, pikepdf.Array):
            pending += list(item)


def _containers(page: pikepdf.Page) -> tuple[int, int]:
    """The distinct dictionaries, arrays and streams reached from a page's dictionary (its
    /Parent excluded), and the depth of the deepest below the page."""
    seen: set[tuple[int, int]] = set()
    count = deepest = 0
    pending: list[tuple[Any, int]] = [(page.obj, 0)]
    while pending:
        item, depth = pending.pop()
        if not isinstance(item, pikepdf.Dictionary | pikepdf.Array | pikepdf.Stream):
            continue
        if item.is_indirect:
            if item.objgen in seen:
                continue
            seen.add(item.objgen)
        count += 1
        deepest = max(deepest, depth)
        if isinstance(item, pikepdf.Array):
            pending += [(value, depth + 1) for value in item]
        else:
            pending += [
                (value, depth + 1)
                for key, value in item.items()
                if not (depth == 0 and key == "/Parent")
            ]
    return count, deepest


def test_schema_depth_65_runs_one_path_65_containers_below_the_page(
    built: dict[str, bytes],
) -> None:
    for page in _single_page(built, "schema-depth-65.pdf"):
        assert _containers(page)[1] == 65


def test_schema_containers_100001_is_one_page_of_100001_containers(
    built: dict[str, bytes],
) -> None:
    for page in _single_page(built, "schema-containers-100001.pdf"):
        assert _containers(page)[0] == 100_001


@pytest.mark.timeout(240)
def test_the_schema_limits_document_has_2000_pages_whose_containers_total_1000001(
    built: dict[str, bytes],
) -> None:
    with _pdf(built["schema-document-1000001.pdf"]) as pdf:
        counts = [_containers(page)[0] for page in pdf.pages]

    assert len(counts) == 2000
    assert sum(counts) == 1_000_001
    assert max(counts) <= 100_000


def test_the_inline_dictionary_fixtures_write_their_dictionaries_in_the_content_stream(
    built: dict[str, bytes],
) -> None:
    for page in _single_page(built, "schema-inline-af.pdf"):
        assert b"/Span << /AF [ << /Type /Filespec /F (a.pdf) >> ] >> BDC" in _content(page)
    for page in _single_page(built, "schema-inline-image-url.pdf"):
        (inline,) = pikepdf.parse_content_stream(page, "BI ID EI")
        assert isinstance(inline, pikepdf.ContentStreamInlineImage)
        assert str(inline.iimage.obj["/URL"]) == "https://example.net/x"
    for page in _single_page(built, "schema-inline-tagged.pdf"):
        content = _content(page)
        for marked in (b"/MCID 0", b"/ActualText", b"/Alt", b"/Lang", b"/E ", b"/OC /oc1 BDC"):
            assert marked in content
        assert "/oc1" in page.Resources.Properties


def test_schema_memo_two_pages_draws_each_page_with_its_own_font_and_image(
    built: dict[str, bytes],
) -> None:
    with _pdf(built["schema-memo-two-pages.pdf"]) as pdf:
        assert len(pdf.pages) == 2
        fonts = [page.Resources.Font.F1 for page in pdf.pages]
        images = [page.Resources.XObject.Im1 for page in pdf.pages]
        font_files = [font.FontDescriptor.FontFile2 for font in fonts]
        shapes = [sorted(font.keys()) for font in fonts] + [sorted(i.keys()) for i in images]
        assert fonts[0].objgen != fonts[1].objgen
        assert font_files[0].objgen != font_files[1].objgen
        assert images[0].read_bytes() != images[1].read_bytes()
        assert list(fonts[0].Encoding.Differences) != list(fonts[1].Encoding.Differences)
        assert shapes[0] == shapes[1]
        assert shapes[2] == shapes[3]
