from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from docchat_testing.load.common import (
    LOAD_USERS,
    QUESTIONS_FILE,
    LoadError,
    UsageError,
    append_summary,
    iso,
    load_questions,
    parse_aliases,
    parse_dotenv,
    parse_time,
    read_azd_environment,
    read_tokens,
    setting,
    token_of,
    write_json,
    write_private,
)


def test_the_load_users_are_load_01_to_load_20() -> None:
    assert LOAD_USERS[0] == "load-01"
    assert LOAD_USERS[-1] == "load-20"
    assert len(set(LOAD_USERS)) == 20


def test_an_alias_range_expands_with_its_digits() -> None:
    assert parse_aliases("load-01..load-20,load-admin") == [*LOAD_USERS, "load-admin"]
    assert parse_aliases("load-03..load-04, load-03") == ["load-03", "load-04"]
    assert parse_aliases("admin") == ["admin"]


@pytest.mark.parametrize("text", ["", " , ", "load-05..load-02"])
def test_an_empty_or_backwards_alias_list_is_refused(text: str) -> None:
    with pytest.raises(UsageError):
        parse_aliases(text)


def test_the_azd_file_is_read_with_its_quoting() -> None:
    text = '# comment\nAPI_CLIENT_ID="abc"\nexport NAME=plain\nQUOTED="a \\"b\\""\nSINGLE=\'x\'\n\n'
    assert parse_dotenv(text) == {
        "API_CLIENT_ID": "abc",
        "NAME": "plain",
        "QUOTED": 'a "b"',
        "SINGLE": "x",
    }


def test_a_missing_azd_file_is_a_usage_error(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="azd env refresh"):
        read_azd_environment("load", tmp_path)
    (tmp_path / ".azure" / "load").mkdir(parents=True)
    (tmp_path / ".azure" / "load" / ".env").write_text('X="1"\n', encoding="utf-8")
    assert read_azd_environment("load", tmp_path) == {"X": "1"}


def test_a_setting_comes_from_the_job_then_the_azd_file() -> None:
    assert setting("A", environ={"A": "job"}, azd={"A": "azd"}) == "job"
    assert setting("A", environ={"A": ""}, azd={"A": "azd"}) == "azd"
    with pytest.raises(UsageError, match="A is set neither"):
        setting("A", environ={}, azd={})


def test_a_private_file_has_mode_0600_whatever_the_umask(tmp_path: Path) -> None:
    path = tmp_path / "out" / "tokens.json"
    path.parent.mkdir()
    path.write_text("old and longer", encoding="utf-8")
    path.chmod(0o644)
    previous = os.umask(0)
    try:
        write_private(path, "new")
    finally:
        os.umask(previous)
    assert path.read_text(encoding="utf-8") == "new"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_json_is_written_sorted_and_privately_on_request(tmp_path: Path) -> None:
    write_json(tmp_path / "a.json", {"b": 1, "a": 2})
    write_json(tmp_path / "p.json", {"x": 1}, private=True)
    assert list(json.loads((tmp_path / "a.json").read_text(encoding="utf-8"))) == ["a", "b"]
    assert stat.S_IMODE((tmp_path / "p.json").stat().st_mode) == 0o600


def test_the_token_file_holds_one_token_per_alias(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"tokens": {"load-01": "t1"}}), encoding="utf-8")
    assert read_tokens(path) == {"load-01": "t1"}
    assert token_of({"load-01": "t1"}, "load-01") == "t1"
    with pytest.raises(UsageError, match="load-02"):
        token_of({"load-01": "t1"}, "load-02")
    contents: list[object] = [{"tokens": {"load-01": ""}}, {"passwords": {}}, ["x"]]
    for content in contents:
        path.write_text(json.dumps(content), encoding="utf-8")
        with pytest.raises(UsageError):
            read_tokens(path)
    with pytest.raises(UsageError):
        read_tokens(tmp_path / "absent.json")


def test_the_load_questions_are_read_from_the_fixture(tmp_path: Path) -> None:
    questions = load_questions()
    assert questions == json.loads(QUESTIONS_FILE.read_text(encoding="utf-8"))
    bad = tmp_path / "q.json"
    bad.write_text(json.dumps(["ok", ""]), encoding="utf-8")
    with pytest.raises(UsageError):
        load_questions(bad)


def test_times_are_utc_with_milliseconds() -> None:
    moment = parse_time("2026-11-03T10:00:05Z")
    assert moment == datetime(2026, 11, 3, 10, 0, 5, tzinfo=UTC)
    assert parse_time("2026-11-03T10:00:05") == moment
    assert iso(moment) == "2026-11-03T10:00:05.000Z"
    with pytest.raises(UsageError):
        parse_time("yesterday")


def test_the_summary_is_appended_only_inside_a_job(tmp_path: Path) -> None:
    summary = tmp_path / "summary.md"
    append_summary("first", {"GITHUB_STEP_SUMMARY": str(summary)})
    append_summary("second\n", {"GITHUB_STEP_SUMMARY": str(summary)})
    append_summary("nowhere", {})
    assert summary.read_text(encoding="utf-8") == "first\nsecond\n"


def test_a_usage_error_exits_2_and_other_load_errors_1() -> None:
    assert UsageError("x", "m").exit_code == 2
    assert LoadError("x", "m").exit_code == 1
    assert LoadError("code", "message").code == "code"
