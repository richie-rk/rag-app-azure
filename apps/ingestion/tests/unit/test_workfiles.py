"""Unit tests of the work-file formats and the rendition marker (`14-ingestion.md` sections 6.14
and 14.5)."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from docchat_core.ingestion.settings import TokenEncoding
from docchat_ingestion.isolation.results import ChildFileError
from docchat_ingestion.workfiles import (
    ChunkLine,
    ChunksHeader,
    EmbeddingBatch,
    ExtractDocument,
    ExtractPartV1,
    PageOffset,
    RenditionMarker,
    SummaryOutcome,
    TableSummaryLine,
    WorkFileError,
    decode_chunks,
    decode_embedding_batch,
    decode_extract_document,
    decode_marker,
    decode_table_summaries,
    encode_chunks,
    encode_table_summaries,
    json_size,
    parse_partition,
)

SHA = "a" * 64


def header(count: int) -> ChunksHeader:
    return ChunksHeader(
        chunk_set_id=uuid4(),
        document_id=uuid4(),
        document_version_id=uuid4(),
        chunk_count=count,
        encoding=TokenEncoding.CL100K_BASE,
        title="Rapport annuel",
    )


def line(ordinal: int) -> ChunkLine:
    return ChunkLine(
        ordinal=ordinal,
        chunk_text=f"Texte {ordinal} — été",
        heading_path=["Résumé"],
        page_number=1,
        token_count=4,
    )


def test_the_chunks_file_round_trips_with_its_header_first() -> None:
    first = header(2)
    data = encode_chunks(first, [line(0), line(1)])
    lines = data.decode("utf-8").splitlines()
    assert json.loads(lines[0]) == {
        "formatVersion": 1,
        "chunkSetId": str(first.chunk_set_id),
        "documentId": str(first.document_id),
        "documentVersionId": str(first.document_version_id),
        "chunkCount": 2,
        "encoding": "cl100k_base",
        "title": "Rapport annuel",
    }
    assert json.loads(lines[1]) == {
        "ordinal": 0,
        "chunkText": "Texte 0 — été",
        "headingPath": ["Résumé"],
        "pageNumber": 1,
        "tokenCount": 4,
    }
    assert "été" in data.decode("utf-8")
    decoded = decode_chunks(data)
    assert decoded.header == first
    assert [chunk.ordinal for chunk in decoded.chunks] == [0, 1]


@pytest.mark.parametrize(
    "lines",
    [
        [line(0)],
        [line(1), line(0)],
    ],
)
def test_a_chunks_file_must_count_and_order_its_chunks(lines: list[ChunkLine]) -> None:
    with pytest.raises(ValueError, match=r"counts|ordinals"):
        decode_chunks(encode_chunks(header(2), lines))


def test_a_chunks_file_starts_with_its_header() -> None:
    with pytest.raises(ValueError, match="header"):
        decode_chunks(b"\n")


def test_headings_are_checked() -> None:
    with pytest.raises(ValidationError):
        ChunkLine(ordinal=0, chunk_text="x", heading_path=[""], page_number=1, token_count=1)
    with pytest.raises(ValidationError):
        ChunkLine(ordinal=0, chunk_text="x", heading_path=["h"] * 9, page_number=1, token_count=1)


def test_table_summaries_round_trip() -> None:
    lines = [
        TableSummaryLine(
            table_sha256=SHA,
            page_number=2,
            outcome=SummaryOutcome.SUMMARISED,
            summary="Totals.",
            input_tokens=9,
            output_tokens=3,
        ),
        TableSummaryLine(
            table_sha256="b" * 64,
            page_number=3,
            outcome=SummaryOutcome.SKIPPED,
            summary=None,
            input_tokens=0,
            output_tokens=0,
        ),
    ]
    data = encode_table_summaries(lines)
    assert json.loads(data.splitlines()[0])["tableSha256"] == SHA
    assert decode_table_summaries(data) == lines
    assert decode_table_summaries(b"") == []
    assert decode_table_summaries(None) == []
    with pytest.raises(WorkFileError):
        decode_table_summaries(data.replace(b"tableSha256", b"table_sha256"))
    with pytest.raises(WorkFileError):
        decode_table_summaries(b"\xff\n")
    with pytest.raises(ValidationError):
        TableSummaryLine(
            table_sha256=SHA,
            page_number=1,
            outcome=SummaryOutcome.FILTERED,
            summary="x",
            input_tokens=0,
            output_tokens=0,
        )


def test_an_embedding_batch_matches_its_batch() -> None:
    batch = EmbeddingBatch(first_ordinal=64, vectors=[[0.1, 0.2], [0.3, 0.4]], prompt_tokens=12)
    assert json.loads(batch.encode()) == {
        "formatVersion": 1,
        "firstOrdinal": 64,
        "vectors": [[0.1, 0.2], [0.3, 0.4]],
        "promptTokens": 12,
    }
    assert batch.matches(64, 2, 2)
    assert not batch.matches(0, 2, 2)
    assert not batch.matches(64, 3, 2)
    assert not batch.matches(64, 2, 3)


def test_a_file_is_read_by_its_camel_case_keys_only() -> None:
    data = encode_chunks(header(1), [line(0)])
    with pytest.raises(ValidationError):
        decode_chunks(data.replace(b"chunkText", b"chunk_text"))
    batch = EmbeddingBatch(first_ordinal=0, vectors=[[0.5]], prompt_tokens=1).encode()
    assert decode_embedding_batch(batch) is not None
    assert decode_embedding_batch(batch.replace(b"firstOrdinal", b"first_ordinal")) is None
    assert decode_embedding_batch(b"not json") is None


def test_a_work_file_error_carries_no_detail() -> None:
    assert str(WorkFileError()) == "work file missing or invalid"


def test_the_extract_document_format() -> None:
    document = ExtractDocument(
        page_count=2,
        markdown="# Title\n\ntext",
        page_offsets=[PageOffset(page_number=1, offset=0, length=7)],
        doc_intel_pages=2,
    )
    assert json.loads(document.encode())["pageOffsets"] == [
        {"pageNumber": 1, "offset": 0, "length": 7}
    ]
    assert json.loads(document.encode())["method"] == "recursive"
    assert decode_extract_document(document.encode(), 2) == document
    with pytest.raises(WorkFileError):
        decode_extract_document(document.encode(), 3)
    with pytest.raises(WorkFileError):
        decode_extract_document(document.encode().replace(b"pageCount", b"page_count"), 2)


def test_the_rendition_marker() -> None:
    marker = RenditionMarker(
        page_count=3, pdf_sha256=SHA, rendition_rules_version=1, document_pdf="built"
    )
    assert json.loads(marker.encode()) == {
        "pageCount": 3,
        "pdfSha256": SHA,
        "renditionRulesVersion": 1,
        "documentPdf": "built",
    }
    assert marker.current(SHA, 1)
    assert not marker.current(SHA, 2)
    assert not marker.current("b" * 64, 1)
    assert decode_marker(marker.encode()) == marker
    assert (
        decode_marker(
            b'{"pageCount": 3, "pdfSha256": "' + SHA.encode() + b'", "documentPdf": "built"}'
        )
        is None
    )
    assert decode_marker(b"not json") is None


def test_json_size_counts_utf8_bytes() -> None:
    assert json_size({"a": "é"}) == len('{"a":"é"}'.encode())


# --- T-098: each work file of `80-data-model.md` section 4.4 against its reference file ----------

_CHUNK_SET = "6f1c2b9e-4d3a-4c5b-8e7f-0a1b2c3d4e5f"
_DOCUMENT = "0f8fad5b-d9cb-469f-a165-70867728950e"
_VERSION = "7c9e6679-7425-40de-944b-e07fc1f90ae7"

REFERENCE_EXTRACT_PART: dict[str, object] = {
    "formatVersion": 1,
    "method": "pagewise",
    "pageCount": 12,
    "firstPage": 1,
    "lastPage": 2,
    "pages": [
        {
            "pageNumber": 1,
            "elements": [
                {"kind": "title", "text": "Rapport", "tableHtml": None, "headingLevel": 1},
                {
                    "kind": "table",
                    "text": "Item EUR",
                    "tableHtml": "<table><tr><td>Item</td></tr></table>",
                    "headingLevel": None,
                },
            ],
        },
        {"pageNumber": 2, "elements": []},
    ],
}
REFERENCE_EXTRACT_DOCUMENT: dict[str, object] = {
    "formatVersion": 1,
    "method": "recursive",
    "pageCount": 2,
    "markdown": "# Rapport\n\nété",
    "pageOffsets": [
        {"pageNumber": 1, "offset": 0, "length": 9},
        {"pageNumber": 2, "offset": 11, "length": 3},
    ],
    "docIntelPages": 2,
}
REFERENCE_TABLE_SUMMARIES: list[dict[str, object]] = [
    {
        "tableSha256": "a" * 64,
        "pageNumber": 1,
        "outcome": "summarised",
        "summary": "Totals by item.",
        "inputTokens": 120,
        "outputTokens": 30,
    },
    {
        "tableSha256": "b" * 64,
        "pageNumber": 2,
        "outcome": "filtered",
        "summary": None,
        "inputTokens": 80,
        "outputTokens": 0,
    },
]
REFERENCE_CHUNKS: list[dict[str, object]] = [
    {
        "formatVersion": 1,
        "chunkSetId": _CHUNK_SET,
        "documentId": _DOCUMENT,
        "documentVersionId": _VERSION,
        "chunkCount": 2,
        "encoding": "cl100k_base",
        "title": "Rapport annuel",
    },
    {
        "ordinal": 0,
        "chunkText": "Rapport",
        "headingPath": ["Rapport"],
        "pageNumber": 1,
        "tokenCount": 2,
    },
    {"ordinal": 1, "chunkText": "été", "headingPath": [], "pageNumber": 2, "tokenCount": 1},
]
REFERENCE_EMBEDDINGS: dict[str, object] = {
    "formatVersion": 1,
    "firstOrdinal": 64,
    "vectors": [[0.25, -0.5], [0.125, 1.0]],
    "promptTokens": 12,
}


def _json_lines(values: list[dict[str, object]]) -> bytes:
    return b"".join(json.dumps(value, ensure_ascii=False).encode() + b"\n" for value in values)


def _with_version(value: dict[str, object], version: object) -> dict[str, object]:
    return {**value, "formatVersion": version}


def _without_version(value: dict[str, object]) -> dict[str, object]:
    return {key: item for key, item in value.items() if key != "formatVersion"}


def _part(raw: bytes) -> ExtractPartV1:
    return parse_partition(raw, page_count=12, first=1, last=2)


def test_t098_the_extract_part_round_trips_its_reference_file() -> None:
    part = _part(json.dumps(REFERENCE_EXTRACT_PART).encode())

    encoded = part.model_dump_json(by_alias=True).encode()
    assert json.loads(encoded) == REFERENCE_EXTRACT_PART
    assert _part(encoded) == part


def test_t098_the_extract_document_round_trips_its_reference_file() -> None:
    document = decode_extract_document(
        json.dumps(REFERENCE_EXTRACT_DOCUMENT, ensure_ascii=False).encode(), 2
    )

    assert json.loads(document.encode()) == REFERENCE_EXTRACT_DOCUMENT
    assert decode_extract_document(document.encode(), 2) == document


def test_t098_the_table_summaries_round_trip_their_reference_file() -> None:
    lines = decode_table_summaries(_json_lines(REFERENCE_TABLE_SUMMARIES))

    encoded = encode_table_summaries(lines)
    assert [json.loads(line) for line in encoded.splitlines()] == REFERENCE_TABLE_SUMMARIES
    assert decode_table_summaries(encoded) == lines


def test_t098_the_chunks_file_round_trips_its_reference_file() -> None:
    decoded = decode_chunks(_json_lines(REFERENCE_CHUNKS))

    encoded = encode_chunks(decoded.header, decoded.chunks)
    assert [json.loads(line) for line in encoded.splitlines()] == REFERENCE_CHUNKS
    assert decode_chunks(encoded) == decoded


def test_t098_the_embedding_batch_round_trips_its_reference_file() -> None:
    batch = decode_embedding_batch(json.dumps(REFERENCE_EMBEDDINGS).encode())

    assert batch is not None
    assert json.loads(batch.encode()) == REFERENCE_EMBEDDINGS
    assert decode_embedding_batch(batch.encode()) == batch


@pytest.mark.parametrize("version", [0, 2, "1", 1.5, None, "missing"])
def test_t098_every_other_format_version_is_refused(version: object) -> None:
    def changed(value: dict[str, object]) -> dict[str, object]:
        return _without_version(value) if version == "missing" else _with_version(value, version)

    with pytest.raises(ChildFileError):
        _part(json.dumps(changed(REFERENCE_EXTRACT_PART)).encode())
    with pytest.raises(WorkFileError):
        decode_extract_document(json.dumps(changed(REFERENCE_EXTRACT_DOCUMENT)).encode(), 2)
    with pytest.raises(ValidationError):
        decode_chunks(_json_lines([changed(REFERENCE_CHUNKS[0]), *REFERENCE_CHUNKS[1:]]))
    assert decode_embedding_batch(json.dumps(changed(REFERENCE_EMBEDDINGS)).encode()) is None


def test_t098_a_table_summary_line_carries_no_format_version() -> None:
    # Section 4.4 gives the table-summary line no `formatVersion`; a line with one is refused
    # like any other unknown key.
    with pytest.raises(WorkFileError):
        decode_table_summaries(_json_lines([_with_version(REFERENCE_TABLE_SUMMARIES[0], 1)]))
