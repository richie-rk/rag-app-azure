"""The verdict queries are section 7.6's text (`84-testing.md`), with the window's bounds bound."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from docchat_testing.load import queries

REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
SPEC = (REPOSITORY_ROOT / "docs" / "spec" / "84-testing.md").read_text(encoding="utf-8")
START = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)
END = datetime(2026, 11, 3, 10, 10, tzinfo=UTC)
WINDOW = re.compile(r"^let s = datetime\([^)]*\); let e = datetime\([^)]*\);\s*")


def spec_queries() -> list[str]:
    section = SPEC.split("### 7.6 ")[1].split("### 7.7 ")[0]
    return re.findall(r"```kusto\n(.*?)```", section, flags=re.DOTALL)


def normalised(text: str) -> str:
    return " ".join(WINDOW.sub("", text.strip()).split())


@pytest.mark.parametrize(
    ("index", "build"),
    [
        (0, queries.c_db),
        (1, queries.s1_concurrency),
        (2, queries.request_units),
        (3, queries.s1_turn_charges),
        (4, queries.s1_missing_extractions),
    ],
    ids=["C-db", "S1-e", "S1-h", "S1-i", "S1-k"],
)
def test_the_query_is_the_text_of_section_7_6(index: int, build: object) -> None:
    assert callable(build)
    text = build(START, END)
    assert text.startswith(
        "let s = datetime(2026-11-03T10:00:00.000000Z); "
        "let e = datetime(2026-11-03T10:10:00.000000Z);"
    )
    assert normalised(text) == normalised(spec_queries()[index])


def test_the_one_line_criteria_follow_section_7_6() -> None:
    section = SPEC.split("### 7.6 ")[1]
    assert "toint(ResultCode) >= 500 | summarize n = sum(ItemCount)" in queries.s1_server_errors(
        START, END
    )
    assert "summarize instances = dcount(AppRoleInstance)" in queries.s1_instances(START, END)
    throttled = (
        "CDBDataPlaneRequests | where TimeGenerated between (s .. e) and DatabaseName == "
        '"docchat" | summarize throttled = countif(StatusCode == 429) by CollectionName'
    )
    assert throttled in section
    assert throttled in normalised(queries.c_db_throttled(START, END))
    cap = queries.c_cap(START)
    assert (
        "_LogOperation\n| where TimeGenerated between "
        "(datetime(2026-11-03T10:00:00.000000Z) .. now())" in cap
    )
    assert 'where Category =~ "Ingestion"' in cap
    assert 'where Detail contains "OverQuota"' in cap


def test_the_s2_queries_name_the_removed_instance_and_its_window() -> None:
    removed_at = datetime(2026, 11, 3, 10, 4, 30, tzinfo=UTC)
    candidates = queries.s2_removed_instance(START, END, removed_at)
    assert "let tr = datetime(2026-11-03T10:04:30.000000Z);" in candidates
    assert "countif(TimeGenerated between ((tr - 120s) .. tr))" in candidates
    assert "countif(TimeGenerated between ((tr + 60s) .. e))" in candidates
    assert "where before > 0 and after == 0" in candidates
    surviving = queries.s2_surviving_failures(START, END, "inst-2")
    assert 'AppRoleInstance != "inst-2"' in surviving
    assert "(Success == false or toint(ResultCode) >= 500) | count" in surviving
    assert "project OperationId, AppRoleInstance" in queries.s2_requests(START, END)


def test_the_calibration_and_ingestion_queries() -> None:
    screen = queries.screen_calls(START, END)
    assert 'Name == "docchat.chat.screen"' in screen
    assert 'summarize s = avg(todouble(Properties["docchat.calls"]))' in screen
    throttled = queries.s4_throttled(START, END)
    assert 'Name == "docchat.ingestion.throttled" | summarize n = sum(Sum)' in throttled
    arrived = queries.telemetry_arrived(END)
    assert "between ((e - 1m) .. e)" in arrived


def test_the_alert_queries_are_a41_and_a42_with_their_thresholds() -> None:
    observability = (REPOSITORY_ROOT / "docs" / "spec" / "85-observability.md").read_text("utf-8")
    for name, text in (("A41", queries.a41(10000)), ("A42", queries.a42(8000))):
        found = re.search(rf"- \*\*{name}\*\* `([^`]*)`", observability)
        assert found is not None
        specified = re.sub(r"<alerts\.\w+>", "{threshold}", found.group(1))
        threshold = "10000" if name == "A41" else "8000"
        assert " ".join(text.split()) == " ".join(specified.format(threshold=threshold).split())
