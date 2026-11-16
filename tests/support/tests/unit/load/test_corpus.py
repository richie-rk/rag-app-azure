"""T-578: the load corpus (`84-testing.md` section 1.5)."""

from __future__ import annotations

import hashlib
import zipfile
from collections import Counter
from pathlib import Path

import pikepdf
import pytest

from docchat_testing.load.common import UsageError
from docchat_testing.load.corpus import build_corpus, corpus_files, plan
from docchat_testing.seed import DOCUMENTS_DIR


def test_t578_build_corpus_writes_200_byte_unique_files_in_the_stated_mix(tmp_path: Path) -> None:
    written = build_corpus("a", 200, tmp_path)

    assert len(written) == 200
    assert [path.name for path in written][:3] == [
        "load-a-001.pdf",
        "load-a-002.pdf",
        "load-a-003.pdf",
    ]
    assert len({hashlib.sha256(path.read_bytes()).hexdigest() for path in written}) == 200
    sources = Counter(item.source for item in plan("a", 200))
    assert sources == {"tables.pdf": 100, "scanned.pdf": 40, "headings.docx": 40, "slides.pptx": 20}
    extensions = Counter(path.suffix for path in written)
    assert extensions == {".pdf": 140, ".docx": 40, ".pptx": 20}
    # Each submitted half holds the same share of each document.
    assert Counter(item.source for item in plan("a", 200)[:100]) == {
        "tables.pdf": 50,
        "scanned.pdf": 20,
        "headings.docx": 20,
        "slides.pptx": 10,
    }


def test_t578_each_pdf_parses_and_each_copy_carries_its_label(tmp_path: Path) -> None:
    written = build_corpus("b", 20, tmp_path)
    for path, item in zip(written, plan("b", 20), strict=True):
        data = path.read_bytes()
        original = (DOCUMENTS_DIR / item.source).read_bytes()
        if path.suffix == ".pdf":
            with pikepdf.open(path) as pdf:
                assert len(pdf.pages) > 0
            assert data.startswith(original)
            assert data.endswith(f"%{item.label}\n".encode())
        else:
            with zipfile.ZipFile(path) as archive:
                assert archive.comment == item.label.encode()
                assert archive.testzip() is None
        assert item.label == f"docchat-load-b-{path.stem.removeprefix('load-b-')}"


def test_the_two_variants_never_share_a_file(tmp_path: Path) -> None:
    first = build_corpus("a", 10, tmp_path / "a")
    second = build_corpus("b", 10, tmp_path / "b")
    hashes_a = {hashlib.sha256(path.read_bytes()).hexdigest() for path in first}
    hashes_b = {hashlib.sha256(path.read_bytes()).hexdigest() for path in second}
    assert not hashes_a & hashes_b
    assert [path.name for path in corpus_files(tmp_path / "a", "a")] == [
        path.name for path in first
    ]


@pytest.mark.parametrize(("variant", "count"), [("c", 200), ("a", 0), ("a", 205), ("a", 1000)])
def test_an_unknown_variant_or_an_uneven_count_is_refused(variant: str, count: int) -> None:
    with pytest.raises(UsageError):
        plan(variant, count)


def test_a_directory_without_the_corpus_is_refused(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="load-a"):
        corpus_files(tmp_path, "a")
