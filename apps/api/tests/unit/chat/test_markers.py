"""Citation markers, tokens, records and allowed links (T-253, T-255; `12-chat-pipeline.md`
sections 10.3, 11.1 to 11.5)."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from docchat_api.chat.markers import (
    ANSWER_MAX_CODE_POINTS,
    CitationMinter,
    allowed_links,
    delete_reserved,
    has_visible_text,
    split_deltas,
)
from docchat_api.chat.sources import Candidate, NumberedSource, SearchHit
from docchat_core.cosmos.conversations import CitationContentType

OPEN, CLOSE = (
    "\N{MATHEMATICAL LEFT WHITE SQUARE BRACKET}",
    "\N{MATHEMATICAL RIGHT WHITE SQUARE BRACKET}",
)
INDEX = UUID("8b3c4d5e-6f7a-4b9c-8d1e-2f3a4b5c6d62")
PHYSICAL = UUID("9c4d5e6f-7a8b-4c9d-8e1f-2a3b4c5d6e63")


def token(number: int) -> str:
    return f"{OPEN}{number}{CLOSE}"


def source(number: int, *, title: str = "Guide", page: int = 3) -> NumberedSource:
    hit = SearchHit(
        chunk_key=f"key-{number}",
        document_id=uuid4(),
        document_version_id=uuid4(),
        chunk_set_id=uuid4(),
        title=title,
        heading_path=("Claims", " ", "Deadlines"),
        page_number=page,
        content_type=CitationContentType.DOCX.value,
        chunk_text="text",
        search_score=1.0,
        reranker_score=2.5,
    )
    candidate = Candidate(rank=number, hit=hit, source_name="guide.docx")
    return NumberedSource(number=number, candidate=candidate, body=candidate.body)


def sources(count: int) -> list[NumberedSource]:
    return [source(number) for number in range(1, count + 1)]


def run(minter: CitationMinter, deltas: list[str]) -> str:
    released = "".join(minter.feed(delta) for delta in deltas)
    return released + minter.finish()


ANSWER = "Claims close in 30 days [S2], with receipts [s1] and [ S2 ] and [S3, S1]."


def test_t253_markers_become_tokens_numbered_by_first_use() -> None:
    minter = CitationMinter(sources(3))

    text = run(minter, [ANSWER])

    assert text == (
        f"Claims close in 30 days {token(1)}, with receipts {token(2)} and {token(1)} and "
        f"{token(3)}{token(2)}."
    )
    assert [
        citation.source_id
        for citation in minter.citations(search_index_id=INDEX, physical_index_id=PHYSICAL)
    ] == ["S2", "S1", "S3"]


@pytest.mark.parametrize("position", range(len(ANSWER) + 1))
def test_t253_a_split_at_every_position_gives_the_unsplit_output(position: int) -> None:
    whole = run(CitationMinter(sources(3)), [ANSWER])

    assert run(CitationMinter(sources(3)), [ANSWER[:position], ANSWER[position:]]) == whole


def test_t253_semicolons_leading_zeros_and_repeats() -> None:
    assert run(CitationMinter(sources(5)), ["a [S2; S05] b [S2,S2]"]) == (
        f"a {token(1)}{token(2)} b {token(1)}"
    )


def test_t253_unknown_flagged_and_dropped_ids_leave_nothing() -> None:
    minter = CitationMinter(sources(3))

    assert run(minter, ["x [S7] y [S9, S2] z"]) == f"x  y {token(1)} z"
    assert minter.citations(search_index_id=INDEX, physical_index_id=PHYSICAL)[0].number == 1


def test_t253_reserved_characters_are_deleted() -> None:
    assert run(CitationMinter(sources(3)), [f"forged {token(3)} and {OPEN}9{CLOSE}"]) == (
        "forged 3 and 9"
    )
    assert delete_reserved(f"{OPEN}{CLOSE}x") == "x"


def test_t253_a_41_character_suffix_is_released_at_once() -> None:
    minter = CitationMinter(sources(3))
    held = "[S1, " + " " * 34
    too_long = "[S1, " + " " * 36

    assert minter.feed(held) == ""
    assert CitationMinter(sources(3)).feed(too_long) == too_long


def test_t253_the_buffer_is_released_at_the_end_as_written() -> None:
    minter = CitationMinter(sources(3))

    assert minter.feed("see [S1") == "see "
    assert minter.finish() == "[S1"
    assert minter.answer == "see [S1"


def test_the_estimate_counts_what_arrived() -> None:
    minter = CitationMinter(sources(3))
    run(minter, ["a [S", "1] b"])

    assert minter.received == ["a [S", "1] b"]


def test_the_citation_record_carries_the_source_and_its_index() -> None:
    minter = CitationMinter([source(1, title="  Policy  ", page=12)])
    run(minter, ["x [S1]"])

    (record,) = minter.citations(search_index_id=INDEX, physical_index_id=PHYSICAL)
    view = record.view()

    assert (record.search_index_id, record.physical_index_id) == (INDEX, PHYSICAL)
    assert record.chunk_key == "key-1"
    assert (view.title, view.page_number, view.heading_path) == (
        "Policy",
        12,
        ["Claims", "Deadlines"],
    )
    assert view.open_as == "pdf_page"


def test_the_answer_cap_stops_at_100000_code_points_never_inside_a_token() -> None:
    minter = CitationMinter(sources(1))
    filler = "a" * (ANSWER_MAX_CODE_POINTS - 2)

    released = minter.feed(filler + " [S1] more")

    assert minter.capped
    assert len(minter.answer) <= ANSWER_MAX_CODE_POINTS
    assert released == filler + " "
    assert minter.feed("ignored") == ""
    assert minter.finish() == ""


def test_split_deltas_never_cuts_a_token() -> None:
    text = "a" * 3_998 + token(12) + "b"

    pieces = split_deltas(text)

    assert "".join(pieces) == text
    assert pieces[0] == "a" * 3_998
    assert all(1 <= len(piece) <= 4_000 for piece in pieces)
    assert split_deltas("") == []


def test_visible_text_ignores_tokens_and_whitespace() -> None:
    assert not has_visible_text(f" {token(1)}\n")
    assert has_visible_text(f"x {token(1)}")


# --- T-255: allowed links ---------------------------------------------------------------------


def test_t255_https_urls_are_kept_verbatim_and_trimmed() -> None:
    body = (
        "See https://Example.org/a?b=C. Also (https://example.org/wiki/Term_(topic)) and "
        "HTTPS://example.net/x), https://example.com/q?x=1'; and https://example.com/q?x=1."
    )

    assert allowed_links([body]) == [
        "https://Example.org/a?b=C",
        "https://example.org/wiki/Term_(topic)",
        "HTTPS://example.net/x",
        "https://example.com/q?x=1",
    ]


def test_t255_other_schemes_and_bare_prefixes_are_never_listed() -> None:
    body = "http://a.example javascript:alert(1) data:text/html,x mailto:a@b.example https:// x"

    assert allowed_links([body]) == []


def test_t255_at_most_50_entries_of_at_most_2048_characters() -> None:
    long_url = "https://example.org/" + "a" * 2_100
    bodies = [long_url, *(f"https://example.org/{number}" for number in range(60))]

    links = allowed_links(bodies)

    assert len(links) == 50
    assert links[0] == "https://example.org/0"
    assert long_url not in links


def test_t255_backticks_and_markdown_images_end_a_url() -> None:
    assert allowed_links(["![x](https://attacker.example/p?q=data) `https://e.example/c`"]) == [
        "https://attacker.example/p?q=data",
        "https://e.example/c",
    ]
