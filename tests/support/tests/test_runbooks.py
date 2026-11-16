"""The runbook files against the runbook index (`86-operations.md` sections 3.1, 3.2 and 4.1).

Every row of section 3.2 has one file `RNN-<slug>.md` in `docs/runbooks/`, its slug the title in
lower case with every run of characters other than letters and digits replaced by one `-`, its
first line `# RNN — <title>` exactly; no other `R*.md` file exists; each states its performer,
trigger and prerequisites, its verification, undo and audit record; and `rehearsals.md` exists.
The index holds 37 rows (`86-operations.md` acceptance criterion 15), so this also proves the
count that the stage-acceptance file check reads (`90-build-order.md` A6-2).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[3]
RUNBOOKS: Final = REPOSITORY_ROOT / "docs" / "runbooks"
OPERATIONS: Final = REPOSITORY_ROOT / "docs" / "spec" / "86-operations.md"
ROW: Final = re.compile(r"^\| (R\d{2}) \| ([^|]+?) \|")
FORM: Final = (
    "**Performer:**",
    "**Trigger:**",
    "**Prerequisites:**",
    "## Verification",
    "## Undo",
    "## Audit record",
)


def index() -> dict[str, str]:
    """Section 3.2's rows: runbook ID to title."""
    text = OPERATIONS.read_text(encoding="utf-8")
    section = text.split("### 3.2 Runbook index", 1)[1].split("\n### ", 1)[0]
    return {
        match.group(1): match.group(2).strip()
        for line in section.splitlines()
        if (match := ROW.match(line))
    }


def slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def test_the_index_holds_37_runbooks() -> None:
    assert list(index()) == [f"R{number:02d}" for number in range(1, 38)]


def test_every_runbook_of_the_index_has_its_file_and_no_other_file_exists() -> None:
    expected = {f"{identifier}-{slug(title)}.md" for identifier, title in index().items()}

    assert {path.name for path in RUNBOOKS.glob("R*.md")} == expected
    assert (RUNBOOKS / "rehearsals.md").is_file()


@pytest.mark.parametrize(("identifier", "title"), sorted(index().items()))
def test_each_runbook_opens_with_its_heading_and_states_its_form(
    identifier: str, title: str
) -> None:
    path = RUNBOOKS / f"{identifier}-{slug(title)}.md"
    text = path.read_text(encoding="utf-8")

    assert text.splitlines()[0] == f"# {identifier} — {title}"
    for part in FORM:
        assert part in text, f"{path.name} lacks {part}"


# --- The rehearsal log (`86-operations.md` section 4.3) ------------------------------------------

REHEARSALS: Final = RUNBOOKS / "rehearsals.md"
LOG_HEADER: Final = (
    "| Date (UTC) | Runbook | Operator | Environment | Result | Correlation or run IDs |"
)
LOG_ROW: Final = re.compile(
    r"^\| (?P<date>\d{4}-\d{2}-\d{2}) \| (?P<runbook>R\d{2}) \| (?P<operator>@?[A-Za-z0-9-]+) "
    r"\| (?P<environment>dev|prod|load) \| (?P<result>passed|failed at step \d+) "
    r"\| (?P<ids>[^|]*\S[^|]*) \|$"
)
CATALOGUE_ROW: Final = re.compile(r"^\| (T-\d{3}) [^|]+ \| ([^|]+) \|")


def log_rows() -> list[str]:
    """The log table's rows: every line after its header and separator."""
    lines = REHEARSALS.read_text(encoding="utf-8").splitlines()
    start = lines.index(LOG_HEADER) + 2
    return [line for line in lines[start:] if line.strip()]


def test_t549_the_log_ends_with_its_table_and_every_row_has_the_records_form() -> None:
    lines = REHEARSALS.read_text(encoding="utf-8").splitlines()
    assert LOG_HEADER in lines
    assert lines[lines.index(LOG_HEADER) + 1] == "|---|---|---|---|---|---|"
    runbooks = set(index())
    for row in log_rows():
        match = LOG_ROW.match(row)
        assert match is not None, f"not a row of section 4.3's form: {row}"
        assert match.group("runbook") in runbooks, row
        # Rows hold IDs only, never a password or a token.
        assert "eyJ" not in match.group("ids"), row


def test_the_catalogues_rehearsal_rows_name_runbooks_of_the_index() -> None:
    lines = REHEARSALS.read_text(encoding="utf-8").splitlines()
    named = {
        match.group(1): set(re.findall(r"\bR\d{2}\b", match.group(2)))
        for line in lines[: lines.index(LOG_HEADER)]
        if (match := CATALOGUE_ROW.match(line))
    }
    rehearsal_rows = {
        "T-455",
        "T-464",
        "T-465",
        "T-466",
        "T-473",
        *(f"T-{n}" for n in range(550, 555)),
    }

    assert rehearsal_rows <= set(named)
    for identifier in rehearsal_rows:
        assert named[identifier], f"{identifier} names no runbook"
        assert named[identifier] <= set(index()), identifier
