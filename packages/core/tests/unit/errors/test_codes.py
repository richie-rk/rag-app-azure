from __future__ import annotations

import re
from collections.abc import Callable

import pytest

from docchat_core.errors.codes import (
    CODES,
    RETRY_AFTER_MAX,
    STREAM_ONLY_CODES,
    TITLE_MAX_LENGTH,
    CodeArea,
    CodeEntry,
    _catalogue,
)

type SpecTable = Callable[[str, str], list[list[str]]]

_API = "11-api.md"
_BATCH_NOTE = re.compile(r" \(`\{maxFilesPerBatch\}` is .*\)$")
_MEMBERS = re.compile(r"retryAfterSeconds|errors|sessionId|turnId")
_FIXED_OR_RANGE = re.compile(r"`retryAfterSeconds` ([\d,]+)(?: to ([\d,]+))?$")


def _number(text: str) -> int:
    return int(text.replace(",", ""))


def _retryable(text: str) -> bool:
    # "no" is the only refusal; "yes, …" and "after a refetch" are retryable after a wait or a
    # change the cell names.
    return not text.startswith("no")


def _problem_retry_after(cell: str) -> tuple[int, int] | None:
    if "retryAfterSeconds" not in cell:
        return None
    match = _FIXED_OR_RANGE.search(cell)
    if match is None:
        # Computed waits (to a deadline or a quota reset) take the problem's whole range.
        return (1, RETRY_AFTER_MAX)
    low = _number(match.group(1))
    return (low, _number(match.group(2)) if match.group(2) else low)


def _stream_retry_after(cell: str) -> tuple[int, int] | None:
    if cell == "null":
        return None
    bounds = re.search(r"at least (\d+) and at most (\d+)", cell)
    if bounds is not None:
        return (int(bounds.group(1)), int(bounds.group(2)))
    return (int(cell), int(cell))


def test_problem_codes_match_section_2_2(spec_table: SpecTable) -> None:
    rows = spec_table(_API, "### 2.2 Problem codes")

    assert len(rows) == 75
    for code, status, title, detail, retryable, extensions, _raised_by in rows:
        entry = CODES[code]
        assert entry.status == int(status), code
        assert entry.title == title, code
        assert entry.detail == _BATCH_NOTE.sub("", detail), code
        assert entry.retryable is _retryable(retryable), code
        assert entry.extensions == frozenset(_MEMBERS.findall(extensions)), code
        assert entry.retry_after == _problem_retry_after(extensions), code


def test_stream_only_codes_match_section_2_3(spec_table: SpecTable) -> None:
    rows = spec_table(_API, "### 2.3 Stream-only codes")

    stream_codes = {row[0] for row in rows} - {"internal_error"}
    assert stream_codes == STREAM_ONLY_CODES
    assert len(STREAM_ONLY_CODES) == 13
    for code, title, message, retryable, retry_after in rows:
        entry = CODES[code]
        assert entry.title == title, code
        assert entry.detail == message, code
        assert entry.retryable is (retryable == "true"), code
        if code != "internal_error":
            assert entry.status is None, code
            assert entry.retry_after == _stream_retry_after(retry_after), code


def test_stored_only_code_matches_section_2_4(spec_table: SpecTable) -> None:
    [(code, title, text, retryable, _where)] = spec_table(_API, "### 2.4 Stored-only turn code")

    entry = CODES[code]
    assert code == "turn_abandoned"
    assert (entry.status, entry.title, entry.detail) == (None, title, text)
    assert entry.retryable is _retryable(retryable)


def test_catalogue_holds_exactly_the_three_sections() -> None:
    assert len(CODES) == 75 + 13 + 1


def test_status_is_none_for_exactly_the_stream_only_codes_and_turn_abandoned() -> None:
    without_status = {code for code, entry in CODES.items() if entry.status is None}

    assert without_status == STREAM_ONLY_CODES | {"turn_abandoned"}


def test_titles_are_sentence_case_and_short() -> None:
    for entry in CODES.values():
        first, *rest = entry.title.split(" ")
        assert len(entry.title) <= TITLE_MAX_LENGTH, entry.code
        assert first[0].isupper(), entry.code
        # Later words are lower case unless they are an acronym such as PDF.
        assert all(word == word.lower() or word.isupper() for word in rest), entry.code


def test_ingestion_codes_are_the_codes_of_14_ingestion_section_17_1(spec_table: SpecTable) -> None:
    rows = spec_table("14-ingestion.md", "### 17.1 API problem codes")

    ingestion = {code for code, entry in CODES.items() if entry.area is CodeArea.INGESTION}
    assert ingestion == {row[0] for row in rows}
    for code, status, title, _raised_by, detail, _retryable in rows:
        entry = CODES[code]
        assert (entry.status, entry.title) == (int(status), title), code
        assert entry.detail == re.sub(r" \(`\{maxFilesPerBatch\}` is .*\)$", "", detail), code


def test_areas_of_chat_and_conversation_codes() -> None:
    by_area = {
        area: {code for code, entry in CODES.items() if entry.area is area} for area in CodeArea
    }

    assert by_area[CodeArea.CONVERSATIONS] == {
        "session_not_found",
        "turn_not_found",
        "memory_fact_not_found",
        "turn_in_progress",
        "turn_already_ended",
        "client_turn_id_conflict",
        "session_turn_limit",
        "feedback_not_allowed",
        "turn_abandoned",
    }
    assert by_area[CodeArea.CHAT] == STREAM_ONLY_CODES | {
        "citation_not_found",
        "document_pdf_unavailable",
        "document_changed",
        "rate_limited",
        "too_many_streams",
        "too_many_downloads",
        "token_quota_exceeded",
        "citation_content_missing",
        "document_pdf_missing",
    }
    assert {"authentication_required", "internal_error", "validation_failed"} <= by_area[
        CodeArea.IDENTITY_ACCESS
    ]


def test_session_and_turn_members_only_on_the_turn_creation_conflicts() -> None:
    with_session = {code for code, entry in CODES.items() if "sessionId" in entry.extensions}
    with_turn = {code for code, entry in CODES.items() if "turnId" in entry.extensions}
    conflicts_with_wait = {
        code
        for code, entry in CODES.items()
        if entry.status == 409 and "retryAfterSeconds" in entry.extensions
    }

    assert with_session == {
        "turn_in_progress",
        "turn_already_ended",
        "client_turn_id_conflict",
        "session_turn_limit",
    }
    assert with_turn == with_session - {"session_turn_limit"}
    assert conflicts_with_wait == {"turn_in_progress"}


def test_every_429_and_every_422_carries_its_member() -> None:
    for entry in CODES.values():
        if entry.status == 429:
            assert "retryAfterSeconds" in entry.extensions, entry.code
        if entry.status == 422:
            assert "errors" in entry.extensions, entry.code


def test_problem_type() -> None:
    assert CODES["invalid_token"].problem_type == "urn:docchat:problem:invalid_token"


def test_fixed_retry_after() -> None:
    assert CODES["dependency_unavailable"].fixed_retry_after == 5
    assert CODES["rate_limited"].fixed_retry_after is None
    assert CODES["index_not_found"].fixed_retry_after is None


def test_render_detail_fills_only_batch_too_large() -> None:
    batch = CODES["batch_too_large"]

    assert batch.detail_fields == frozenset({"maxFilesPerBatch"})
    assert batch.render_detail({"maxFilesPerBatch": 100}) == "Send at most 100 files at a time."
    assert CODES["index_busy"].render_detail() == CODES["index_busy"].detail
    with pytest.raises(ValueError, match="placeholders"):
        batch.render_detail()
    with pytest.raises(ValueError, match="placeholders"):
        CODES["index_busy"].render_detail({"maxFilesPerBatch": 1})


def test_codes_is_read_only() -> None:
    with pytest.raises(TypeError):
        CODES["new_code"] = CODES["index_busy"]  # type: ignore[index]


def _entry(**changes: object) -> CodeEntry:
    fields: dict[str, object] = {
        "code": "some_code",
        "status": 409,
        "title": "Some title",
        "detail": "Some text.",
        "retryable": False,
        "area": CodeArea.CHAT,
    }
    fields.update(changes)
    return CodeEntry(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes",
    [
        {"code": "Bad-Code"},
        {"code": "x" * 65},
        {"status": 200},
        {"status": 600},
        {"title": "x" * 41},
        {"title": ""},
        {"detail": "x" * 301},
        {"detail": ""},
        {"status": None, "extensions": frozenset({"errors"})},
        {"extensions": frozenset({"retryAfterSeconds"}), "retry_after": (0, 5)},
        {"extensions": frozenset({"retryAfterSeconds"}), "retry_after": (5, 1)},
        {"extensions": frozenset({"retryAfterSeconds"}), "retry_after": (1, RETRY_AFTER_MAX + 1)},
        {"extensions": frozenset({"retryAfterSeconds"})},
        {"retry_after": (5, 5)},
    ],
)
def test_code_entry_refuses_an_invalid_entry(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"some_code|invalid code"):
        _entry(**changes)


def test_a_stream_code_may_state_a_wait_without_the_problem_member() -> None:
    entry = _entry(status=None, retry_after=(1, 60))

    assert entry.retry_after == (1, 60)


def test_catalogue_refuses_a_code_registered_twice() -> None:
    with pytest.raises(ValueError, match="registered twice"):
        _catalogue((_entry(), _entry()))
