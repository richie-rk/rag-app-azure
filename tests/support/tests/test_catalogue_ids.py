"""T-569, the catalogue check (`84-testing.md` section 5.1): code and test catalogue stay together.

It reads section 5 of `docs/spec/84-testing.md` and fails when two rows share an identifier or a
name, when a test name carries an identifier the catalogue lacks, and when a row of level
`unit`, `component`, `integration`, `contract`, `meta` or `browser` has no test whose name
carries it (section 5.1 item 3: `test_t<nnn>_` for Python, a title starting `T-<nnn>` for Vitest
and Playwright). A row's test arrives with its subject (`90-build-order.md` section 1.2 item 3),
so that last check covers the rows of the stages that `90-build-order.md` section 3 completes up
to the current one; the current stage is the one after the last stage that ADR 0141 records as
accepted.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

from docchat_testing.plugin import CatalogueRow, catalogue_identifier, read_catalogue

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[3]
BUILD_ORDER: Final = REPOSITORY_ROOT / "docs" / "spec" / "90-build-order.md"
ADR_DIRECTORY: Final = REPOSITORY_ROOT / "docs" / "adr"

GATED_LEVELS: Final = frozenset({"unit", "component", "integration", "contract", "meta", "browser"})
NOT_GATED: Final = frozenset({"integration (release commit)"})
"""A verdict of the load-test run, not a test of its own (`90-build-order.md` section 1.3.3)."""

SKIPPED_DIRECTORIES: Final = frozenset({".git", ".venv", "node_modules", "__pycache__"})
_STAGE_HEADING_RE: Final = re.compile(r"^### 3\.\d+ Stage (?P<stage>\d+) ")
_STAGE_TESTS_RE: Final = re.compile(
    r"^\*\*Tests (?:completed|withdrawn) at this stage\.\*\* (?P<ids>.*)$"
)
_ID_RANGE_RE: Final = re.compile(r"T-(?P<first>\d{3})(?: to T-(?P<last>\d{3}))?")
_OUTCOME_RE: Final = re.compile(
    r"Outcome \(\d{4}-\d{2}-\d{2}\): stage (?P<stage>\d+) accepted at commit"
)
_WEB_TITLE_RE: Final = re.compile(
    r"\b(?:test|it)(?:\.(?:only|skip|fails|concurrent|fixme|slow))*\(\s*[\"'`]T-(?P<number>\d{3})\b"
)


def is_gated(level: str) -> bool:
    """Whether a row of `level` runs on every pull request as a named test."""
    if level in NOT_GATED:
        return False
    return any(part.split(" (")[0] in GATED_LEVELS for part in level.split(" and "))


def stage_rows(text: str) -> dict[int, set[str]]:
    """The identifiers each stage of section 3 completes or withdraws."""
    stages: dict[int, set[str]] = {}
    current: int | None = None
    for line in text.splitlines():
        heading = _STAGE_HEADING_RE.match(line)
        if heading is not None:
            current = int(heading.group("stage"))
            continue
        listed = _STAGE_TESTS_RE.match(line)
        if listed is None or current is None:
            continue
        for match in _ID_RANGE_RE.finditer(listed.group("ids")):
            first = int(match.group("first"))
            last = int(match.group("last") or first)
            stages.setdefault(current, set()).update(f"T-{n:03d}" for n in range(first, last + 1))
    return stages


def current_stage(adr_text: str) -> int:
    """The stage after the last one ADR 0141's outcome lines accept; 0 before any."""
    accepted = [int(match.group("stage")) for match in _OUTCOME_RE.finditer(adr_text)]
    return max(accepted, default=-1) + 1


def _files(root: Path, suffixes: tuple[str, ...]) -> Iterator[Path]:
    # Pruned while walking: a hidden directory (the virtual environment, or a tool's checkouts
    # of the repository) holds no test of this repository and would only slow the walk.
    for directory, subdirectories, names in root.walk():
        subdirectories[:] = [
            name
            for name in subdirectories
            if name not in SKIPPED_DIRECTORIES and not name.startswith(".")
        ]
        yield from (directory / name for name in names if Path(name).suffix in suffixes)


def python_test_ids(source: str) -> list[str]:
    """The identifiers that test functions defined in `source` carry; test sources that a test
    embeds as strings (nested sessions) are text, not functions, and don't count."""
    names = (
        node.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    )
    return [identifier for name in names if (identifier := catalogue_identifier(name)) is not None]


def web_test_ids(source: str) -> list[str]:
    """The identifiers that Vitest and Playwright test titles in `source` start with."""
    return [f"T-{match.group('number')}" for match in _WEB_TITLE_RE.finditer(source)]


def repository_test_ids(root: Path = REPOSITORY_ROOT) -> dict[str, list[str]]:
    """Each identifier carried by a test name in the repository, with the files carrying it."""
    found: dict[str, list[str]] = {}
    for path in _files(root, (".py",)):
        if path.name.startswith("test_"):
            for identifier in python_test_ids(path.read_text(encoding="utf-8")):
                found.setdefault(identifier, []).append(str(path.relative_to(root)))
    for path in _files(root, (".ts", ".tsx")):
        if ".test." in path.name or ".spec." in path.name:
            for identifier in web_test_ids(path.read_text(encoding="utf-8")):
                found.setdefault(identifier, []).append(str(path.relative_to(root)))
    return found


@pytest.fixture(scope="module")
def catalogue() -> tuple[CatalogueRow, ...]:
    return read_catalogue(REPOSITORY_ROOT)


@pytest.fixture(scope="module")
def stages() -> dict[int, set[str]]:
    return stage_rows(BUILD_ORDER.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def stage() -> int:
    (adr,) = ADR_DIRECTORY.glob("0141-*.md")
    return current_stage(adr.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def test_ids() -> dict[str, list[str]]:
    return repository_test_ids()


def _missing(
    catalogue: tuple[CatalogueRow, ...], test_ids: dict[str, list[str]], rows: set[str]
) -> list[str]:
    return [
        f"{row.identifier} {row.name} ({row.level})"
        for row in catalogue
        if row.identifier in rows and is_gated(row.level) and row.identifier not in test_ids
    ]


# T-569


def test_t569_no_two_rows_share_an_identifier_or_a_name(
    catalogue: tuple[CatalogueRow, ...],
) -> None:
    identifiers = Counter(row.identifier for row in catalogue)
    names = Counter(row.name for row in catalogue)

    assert [identifier for identifier, count in identifiers.items() if count > 1] == []
    assert [name for name, count in names.items() if count > 1] == []


def test_t569_every_test_name_carries_a_catalogue_identifier(
    catalogue: tuple[CatalogueRow, ...], test_ids: dict[str, list[str]]
) -> None:
    known = {row.identifier for row in catalogue}

    unknown = {
        identifier: files for identifier, files in test_ids.items() if identifier not in known
    }

    assert unknown == {}


def test_t569_rows_of_accepted_stages_have_tests(
    catalogue: tuple[CatalogueRow, ...],
    stages: dict[int, set[str]],
    stage: int,
    test_ids: dict[str, list[str]],
) -> None:
    accepted = set().union(*(rows for number, rows in stages.items() if number < stage))

    assert _missing(catalogue, test_ids, accepted) == []


def test_t569_rows_of_the_current_stage_have_tests(
    catalogue: tuple[CatalogueRow, ...],
    stages: dict[int, set[str]],
    stage: int,
    test_ids: dict[str, list[str]],
) -> None:
    assert _missing(catalogue, test_ids, stages.get(stage, set())) == []


def test_every_catalogue_row_sits_in_exactly_one_stage(
    catalogue: tuple[CatalogueRow, ...], stages: dict[int, set[str]]
) -> None:
    placements = Counter(identifier for rows in stages.values() for identifier in rows)

    assert {row.identifier for row in catalogue} == set(placements)
    assert [identifier for identifier, count in placements.items() if count > 1] == []


# The readers


def test_python_test_names_are_read_from_function_definitions() -> None:
    source = (
        "def test_t001_matrix() -> None: ...\n"
        "async def test_t573_stream() -> None: ...\n"
        "def test_plain() -> None: ...\n"
        "EMBEDDED = 'def test_t999_inside_a_string() -> None: ...'\n"
        "class TestGroup:\n"
        "    def test_t002_method(self) -> None: ...\n"
    )

    assert python_test_ids(source) == ["T-001", "T-573", "T-002"]


def test_web_test_titles_are_read() -> None:
    source = (
        'test("T-349 tampered state lands on /chat", async () => {});\n'
        "it('T-350 renders', () => {});\n"
        "test.skip(`T-351 later`, () => {});\n"
        'describe("T-352 is a group, not a test", () => {});\n'
        'test("no identifier", () => {});\n'
    )

    assert web_test_ids(source) == ["T-349", "T-350", "T-351"]


@pytest.mark.parametrize(
    ("level", "gated"),
    [
        ("unit", True),
        ("meta", True),
        ("browser", True),
        ("component", True),
        ("contract", True),
        ("unit and integration", True),
        ("integration (ingestion image)", True),
        ("integration (release commit)", False),
        ("CI check", False),
        ("deployed (stage acceptance)", False),
        ("withdrawn", False),
        ("infrastructure", False),
    ],
)
def test_the_gated_levels(level: str, gated: bool) -> None:
    assert is_gated(level) is gated


def test_stage_lists_expand_ranges() -> None:
    text = (
        "### 3.1 Stage 0 — Scaffold\n"
        "**Tests completed at this stage.** T-029 to T-031, T-071.\n"
        "### 3.2 Stage 1 — Ingestion\n"
        "**Tests completed at this stage.** T-088.\n"
        "**Tests withdrawn at this stage.** T-208 (ADR 0152).\n"
    )

    assert stage_rows(text) == {0: {"T-029", "T-030", "T-031", "T-071"}, 1: {"T-088", "T-208"}}


def test_the_current_stage_follows_the_last_accepted_one() -> None:
    line = "Outcome (2026-11-30): stage {n} accepted at commit {sha}; record https://t/1\n"

    assert current_stage("no outcome yet") == 0
    assert current_stage(line.format(n=0, sha="a" * 40)) == 1
    assert current_stage(line.format(n=0, sha="a" * 40) + line.format(n=1, sha="b" * 40)) == 2
