"""Unit tests of document names, displayed text, blob paths and physical index names (T-096,
T-106, T-131 and the `display_text` half of T-209; `14-ingestion.md` sections 2.2, 6.14, 8.2 and
9.1 item 5; `80-data-model.md` sections 4.3 and 6.2)."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

import pytest

from docchat_core.ingestion.models import ContentType
from docchat_core.ingestion.names import (
    INGESTION_QUEUE,
    POISON_QUEUE,
    BlobContainer,
    DocumentName,
    InvalidDocumentNameError,
    chunks_path,
    content_type_of,
    converted_path,
    display_text,
    document_name,
    document_title,
    embedding_batch_path,
    extract_document_path,
    extract_part_path,
    index_prefix,
    normalize_document_name,
    physical_index_name,
    rendition_document_path,
    rendition_marker_path,
    rendition_page_path,
    scanner_canary_path,
    source_path,
    table_summaries_path,
    validate_blob_path,
    validate_blob_prefix,
    validate_physical_index_name,
    work_converted_path,
    work_prefix,
)

INDEX = UUID("3f2c0d9e-8b7a-41c6-a5d4-e3f2c1b0a998")
DOCUMENT = UUID("9b2e4c1a-7d3f-4e8b-a6c5-0f1e2d3c4b5a")
RUN = UUID("6c1f3e2a-9b4d-4f0e-8a71-2d5c9e0b7f13")
CHUNK_SET = UUID("0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d")
PREFIX = f"{RUN}/{DOCUMENT}/12/"


# --- Name rules (T-131) ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "normalized"),
    [
        ("Report.pdf", "report.pdf"),
        ("\t Annual   Report 2026.PDF \n", "annual report 2026.pdf"),
        ("Stra\u00dfe.docx", "strasse.docx"),
        ("Cafe\u0301.pptx", "caf\u00e9.pptx"),
        ("a\u00a0b\u2028c.pdf", "a b c.pdf"),
        ("x" * 251 + ".pdf", "x" * 251 + ".pdf"),
    ],
)
def test_t131_normalisation_applies_nfc_trimming_folding_and_casefold(
    name: str, normalized: str
) -> None:
    assert normalize_document_name(name) == normalized


@pytest.mark.parametrize(
    "character",
    [
        "\u0000",
        "\u001f",
        "\u007f",
        "\u0085",
        "\u009f",
        "\u200b",
        "\u200e",
        "\u200f",
        "\ufeff",
        "\u202e",
        "\u2066",
        "\u2069",
        "\ue000",
        "\ud800",
        "\u0378",
        "/",
        "\\",
    ],
)
def test_t131_refused_code_points_are_invalid_characters(character: str) -> None:
    with pytest.raises(InvalidDocumentNameError) as raised:
        normalize_document_name(f"a{character}b.pdf")
    assert (raised.value.code, raised.value.reason) == ("invalid_file_name", "invalid_characters")
    assert character not in str(raised.value)


def test_t131_a_tab_inside_a_name_is_a_control_character() -> None:
    with pytest.raises(InvalidDocumentNameError) as raised:
        normalize_document_name("Annual\tReport.pdf")
    assert raised.value.reason == "invalid_characters"


def test_t131_a_fake_double_extension_is_refused() -> None:
    with pytest.raises(InvalidDocumentNameError) as raised:
        document_name("invoice\u202excod.pdf")
    assert raised.value.reason == "invalid_characters"


@pytest.mark.parametrize(("name", "reason"), [("", "too_short"), ("   ", "too_short")])
def test_t131_an_empty_name_is_too_short(name: str, reason: str) -> None:
    with pytest.raises(InvalidDocumentNameError) as raised:
        normalize_document_name(name)
    assert raised.value.reason == reason


def test_t131_a_256_character_name_is_too_long() -> None:
    normalize_document_name("x" * 251 + ".pdf")
    with pytest.raises(InvalidDocumentNameError) as raised:
        normalize_document_name("x" * 252 + ".pdf")
    assert (raised.value.code, raised.value.reason) == ("invalid_file_name", "too_long")


def test_length_counts_after_trimming_and_nfc() -> None:
    assert normalize_document_name("  " + "e\u0301" * 251 + ".pdf  ") == "\u00e9" * 251 + ".pdf"


@pytest.mark.parametrize(
    ("name", "content_type"),
    [
        ("a.pdf", ContentType.PDF),
        ("A.PDF", ContentType.PDF),
        ("deck.PpTx", ContentType.PPTX),
        ("memo.docx", ContentType.DOCX),
    ],
)
def test_document_name_takes_the_media_type_from_the_extension(
    name: str, content_type: ContentType
) -> None:
    parsed = document_name(f"  {name}  ")
    assert parsed == DocumentName(name, name.casefold(), content_type)


@pytest.mark.parametrize("name", ["report", "report.doc", "report.pdf.exe", "slides.ppt", "x.txt"])
def test_another_extension_is_an_unsupported_type(name: str) -> None:
    with pytest.raises(InvalidDocumentNameError) as raised:
        document_name(name)
    assert (raised.value.code, raised.value.reason) == ("unsupported_type", "invalid_format")


def test_the_character_rules_come_before_the_extension() -> None:
    with pytest.raises(InvalidDocumentNameError) as raised:
        document_name("a/b.txt")
    assert raised.value.code == "invalid_file_name"


def test_content_type_and_title_of_a_name() -> None:
    assert content_type_of("x.Docx") is ContentType.DOCX
    assert content_type_of("x.zip") is None
    assert document_title("Annual report 2026.PDF") == "Annual report 2026"
    assert document_title("deck.v2.pptx") == "deck.v2"
    assert document_title("no extension") == "no extension"
    assert document_title("t" * 400 + ".pdf") == "t" * 300


# --- Displayed text (T-209, display_text) ---------------------------------------------------------


@pytest.mark.parametrize(
    "character",
    [
        "\u0000",
        "\u001b",
        "\u007f",
        "\u200b",
        "\u200e",
        "\u202e",
        "\u2066",
        "\u2069",
        "\ufeff",
        "\ue000",
        "\u0378",
        "\ud800",
    ],
)
def test_t209_display_text_removes_refused_code_points(character: str) -> None:
    assert display_text(f"Intro{character}duction") == "Introduction"


def test_t209_display_text_normalises_collapses_trims_and_cuts() -> None:
    assert display_text("  Cafe\u0301   menu \u00a0 ") == "Caf\u00e9 menu"
    assert display_text("Chapter 1\nIntroduction\u0085Part\tA") == "Chapter 1 Introduction Part A"
    assert display_text("h" * 250) == "h" * 200
    assert display_text("abc", 2) == "ab"
    assert display_text("\u200b\u2066\u202e") == ""
    assert display_text("") == ""


def test_display_text_needs_a_positive_limit() -> None:
    with pytest.raises(ValueError, match="limit"):
        display_text("x", 0)


# --- Blob paths (T-096) ---------------------------------------------------------------------------


def test_t096_document_and_rendition_paths() -> None:
    assert source_path(INDEX, DOCUMENT, 12) == f"{INDEX}/{DOCUMENT}/12/source"
    assert converted_path(INDEX, DOCUMENT, 12) == f"{INDEX}/{DOCUMENT}/12/converted.pdf"
    assert rendition_page_path(INDEX, DOCUMENT, 12, 7) == f"{INDEX}/{DOCUMENT}/12/pages/7.pdf"
    assert rendition_document_path(INDEX, DOCUMENT, 1) == f"{INDEX}/{DOCUMENT}/1/document.pdf"
    assert rendition_marker_path(INDEX, DOCUMENT, 1) == f"{INDEX}/{DOCUMENT}/1/complete.json"
    assert scanner_canary_path(DOCUMENT) == f"_scanner-canary/{DOCUMENT}.pdf"
    assert index_prefix(INDEX) == f"{INDEX}/"


def test_t096_work_paths() -> None:
    assert work_prefix(RUN, DOCUMENT, 12) == PREFIX
    assert work_converted_path(PREFIX) == f"{PREFIX}convert/converted.pdf"
    assert extract_part_path(PREFIX, 1, 40) == f"{PREFIX}extract/part-00001-00040.json"
    assert extract_part_path(PREFIX, 41, 41) == f"{PREFIX}extract/part-00041-00041.json"
    assert extract_document_path(PREFIX) == f"{PREFIX}extract/document.json"
    assert table_summaries_path(PREFIX) == f"{PREFIX}enrich/table-summaries.jsonl"
    assert chunks_path(PREFIX, CHUNK_SET) == f"{PREFIX}chunks/{CHUNK_SET}.jsonl"
    assert embedding_batch_path(PREFIX, CHUNK_SET, 0) == (
        f"{PREFIX}embeddings/{CHUNK_SET}/batch-00000.json"
    )
    assert embedding_batch_path(PREFIX, CHUNK_SET, 99_999).endswith("batch-99999.json")


@pytest.mark.parametrize(
    ("builder", "arguments"),
    [
        (source_path, (str(INDEX), DOCUMENT, 1)),
        (source_path, (INDEX, DOCUMENT.hex, 1)),
        (converted_path, (INDEX, 7, 1)),
        (rendition_page_path, (INDEX, DOCUMENT, 1, "3")),
        (work_prefix, (None, DOCUMENT, 1)),
        (chunks_path, (PREFIX, str(CHUNK_SET))),
        (scanner_canary_path, ("canary",)),
        (index_prefix, (str(INDEX),)),
    ],
)
def test_t096_a_segment_that_isnt_a_uuid_is_refused(
    builder: Any, arguments: tuple[Any, ...]
) -> None:
    with pytest.raises((TypeError, ValueError)):
        builder(*arguments)


@pytest.mark.parametrize("number", [0, -1, True, 1.0, "1"])
def test_t096_a_version_or_page_that_isnt_a_positive_integer_is_refused(number: Any) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        source_path(INDEX, DOCUMENT, number)
    with pytest.raises(ValueError, match="positive integer"):
        rendition_page_path(INDEX, DOCUMENT, 1, number)


@pytest.mark.parametrize(
    ("first", "last"), [(0, 1), (2, 1), (1, 100_000), (True, 2), (1, 2.0), (-1, 3)]
)
def test_t096_part_numbers_are_bounded(first: Any, last: Any) -> None:
    with pytest.raises(ValueError, match="part"):
        extract_part_path(PREFIX, first, last)


@pytest.mark.parametrize("batch", [-1, 100_000, True, 1.0])
def test_t096_batch_numbers_are_bounded(batch: Any) -> None:
    with pytest.raises(ValueError, match="batch"):
        embedding_batch_path(PREFIX, CHUNK_SET, batch)


@pytest.mark.parametrize(
    "prefix",
    [
        f"{RUN}/{DOCUMENT}/12",
        f"{RUN}/{DOCUMENT}/0/",
        f"{RUN}/{DOCUMENT}/012/",
        f"../{DOCUMENT}/1/",
        f"{str(RUN).upper()}/{DOCUMENT}/1/",
        f"{RUN}/{DOCUMENT}/1/extract/",
    ],
)
def test_a_work_prefix_from_data_is_revalidated(prefix: str) -> None:
    with pytest.raises(ValueError, match="work prefix"):
        table_summaries_path(prefix)


def test_every_built_path_has_its_containers_shape() -> None:
    documents = BlobContainer.DOCUMENTS
    assert validate_blob_path(documents, source_path(INDEX, DOCUMENT, 3))
    assert validate_blob_path("renditions", rendition_page_path(INDEX, DOCUMENT, 3, 2))  # type: ignore[arg-type]
    for path in (
        f"{INDEX}/{DOCUMENT}/3/source/../x",
        f"{INDEX}/{DOCUMENT}/03/source",
        f"{INDEX}/{DOCUMENT}/3/Source",
        f"{INDEX}/{DOCUMENT}/3/pages/1.pdf",
        "_scanner-canary/x.pdf",
        "",
    ):
        with pytest.raises(ValueError, match="blob path"):
            validate_blob_path(documents, path)
    with pytest.raises(ValueError, match="blob path"):
        validate_blob_path(BlobContainer.RENDITIONS, f"{INDEX}/{DOCUMENT}/3/pages/0.pdf")
    with pytest.raises(ValueError, match="blob path"):
        validate_blob_path(BlobContainer.INGESTION_WORK, f"{PREFIX}extract/part-1-2.json")


def test_blob_prefixes() -> None:
    assert validate_blob_prefix(BlobContainer.RENDITIONS, f"{INDEX}/{DOCUMENT}/")
    assert validate_blob_prefix(BlobContainer.DOCUMENTS, f"{INDEX}/{DOCUMENT}/4/")
    assert validate_blob_prefix(BlobContainer.INGESTION_WORK, PREFIX)
    with pytest.raises(ValueError, match="prefix"):
        validate_blob_prefix(BlobContainer.INGESTION_WORK, f"{RUN}/")
    with pytest.raises(ValueError, match="prefix"):
        validate_blob_prefix(BlobContainer.DOCUMENTS, "")


def test_containers_and_queues() -> None:
    assert [container.value for container in BlobContainer] == [
        "documents",
        "renditions",
        "ingestion-work",
    ]
    assert INGESTION_QUEUE == "ingestion-requests"
    assert POISON_QUEUE == "ingestion-requests-poison"


# --- Physical index names (T-106) -----------------------------------------------------------------


def test_t106_the_example_name() -> None:
    assert physical_index_name(INDEX, 2) == "docchat-3f2c0d9e8b7a41c6a5d4e3f2c1b0a998-g2"


@pytest.mark.parametrize("generation", [1, 9, 10, 4321, 99_999])
def test_t106_every_name_matches_the_pattern(generation: int) -> None:
    name = physical_index_name(INDEX, generation)
    assert re.fullmatch(r"^docchat-[0-9a-f]{32}-g[1-9][0-9]{0,4}$", name)
    assert len(name) <= 47


@pytest.mark.parametrize("generation", [0, 100_000, -1, True, 2.0])
def test_t106_generation_0_or_100000_is_refused(generation: Any) -> None:
    with pytest.raises(ValueError, match="generation"):
        physical_index_name(INDEX, generation)


def test_a_name_is_built_from_a_uuid() -> None:
    with pytest.raises(TypeError):
        physical_index_name(str(INDEX), 1)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "name",
    [
        "docchat-3f2c0d9e8b7a41c6a5d4e3f2c1b0a998-g0",
        "docchat-3F2C0D9E8B7A41C6A5D4E3F2C1B0A998-g1",
        "docchat--3f2c0d9e8b7a41c6a5d4e3f2c1b0a99-g1",
        "docchat-3f2c0d9e8b7a41c6a5d4e3f2c1b0a998-g123456",
        "other-index",
    ],
)
def test_a_name_from_data_is_revalidated(name: str) -> None:
    with pytest.raises(ValueError, match="physical index name"):
        validate_physical_index_name(name)
