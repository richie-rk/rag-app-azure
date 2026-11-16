"""`docchat-load build-corpus` (`84-testing.md` section 1.5; T-578).

`docchat-load build-corpus --variant <a|b> --count 200 --out <dir>` writes 100 copies of
`tables.pdf`, 40 of `scanned.pdf`, 40 of `headings.docx` and 20 of `slides.pptx`, each made
byte-unique by appending the PDF comment line `%docchat-load-<variant>-<nnn>` after `%%EOF`
(readers ignore it) or by setting the ZIP archive comment to the same text (DOCX, PPTX), named
`load-<variant>-<nnn>.<ext>`. The SHA-256 duplicate check therefore sees new documents while
parse cost stays representative and repeatable.

The mix is that ratio, 5 : 2 : 2 : 1, laid out in blocks of ten so that each submitted half of the
corpus holds the same share of each document.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from docchat_testing.load.common import UsageError
from docchat_testing.seed import DOCUMENTS_DIR, byte_unique

VARIANTS: Final = ("a", "b")
BLOCK: Final = (
    "tables.pdf",
    "tables.pdf",
    "tables.pdf",
    "tables.pdf",
    "tables.pdf",
    "scanned.pdf",
    "scanned.pdf",
    "headings.docx",
    "headings.docx",
    "slides.pptx",
)
"""One block of ten files: half tables, a fifth scanned, a fifth headings, a tenth slides."""
MAX_COUNT: Final = 990


@dataclass(frozen=True, slots=True)
class CorpusFile:
    name: str
    source: str
    label: str


def plan(variant: str, count: int) -> list[CorpusFile]:
    """The corpus's files in submission order."""
    if variant not in VARIANTS:
        raise UsageError("variant_invalid", "the variant is a or b")
    if count < len(BLOCK) or count > MAX_COUNT or count % len(BLOCK):
        raise UsageError(
            "count_invalid", f"the count is a multiple of 10 from 10 to {MAX_COUNT}, not {count}"
        )
    files: list[CorpusFile] = []
    for index in range(count):
        source = BLOCK[index % len(BLOCK)]
        number = f"{index + 1:03d}"
        extension = Path(source).suffix
        files.append(
            CorpusFile(
                name=f"load-{variant}-{number}{extension}",
                source=source,
                label=f"docchat-load-{variant}-{number}",
            )
        )
    return files


def build_corpus(
    variant: str, count: int, out: Path, *, documents: Path = DOCUMENTS_DIR
) -> list[Path]:
    """Write the corpus into `out` (created when absent); the written paths, in order."""
    files = plan(variant, count)
    out.mkdir(parents=True, exist_ok=True)
    sources = {name: (documents / name).read_bytes() for name in sorted(set(BLOCK))}
    written: list[Path] = []
    for file in files:
        path = out / file.name
        path.write_bytes(byte_unique(sources[file.source], file.source, file.label))
        written.append(path)
    return written


def corpus_files(directory: Path, variant: str) -> Sequence[Path]:
    """The corpus files of a variant in a directory, in submission order."""
    found = sorted(directory.glob(f"load-{variant}-*.*"))
    if not found:
        raise UsageError("corpus_missing", f"{directory} holds no load-{variant}-* files")
    return found
