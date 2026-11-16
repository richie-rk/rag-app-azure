"""Unit tests of the table-summary call of the page-wise Enrich step (T-185, T-186;
`14-ingestion.md` sections 6.4 and 12; ADR 0051).

Model calls are tested against the model stub at the integration level; here the summariser takes
an in-memory fake of its `SummaryModel` port, and the `AsyncOpenAI` adapter takes a fake client
whose `chat.completions.create` records its arguments (`01-repository.md` section 6.3).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast, override

import openai
import pytest

from docchat_core.ingestion.methods import MethodFailedError
from docchat_core.ingestion.models import ItemSettings, ItemWarning
from docchat_ingestion.isolation.results import ExtractElement, ExtractPage, ExtractPartV1
from docchat_ingestion.methods.pagewise.prompts import SYSTEM_MESSAGE
from docchat_ingestion.methods.pagewise.summaries import (
    OpenAISummaryModel,
    SummaryReply,
    SummaryUnavailableError,
    TableJob,
    TableSummariser,
    check_output,
    summary_texts,
    table_jobs,
)
from docchat_ingestion.methods.tables import table_sha256
from docchat_ingestion.methods.text import Tokenizer
from docchat_ingestion.workfiles import (
    SummaryOutcome,
    TableSummaryLine,
    decode_table_summaries,
    encode_table_summaries,
)
from docchat_testing.clock import FakeClock

type MetricPoints = Callable[[str], list[Any]]


def settings(**overrides: Any) -> ItemSettings:
    values: dict[str, Any] = {
        "activity_time_budget_seconds": 1200,
        "max_pages_per_document": 2000,
        "pagewise_pages_per_extract": 40,
        "max_parse_seconds_per_document": 7200,
        "embedding_batch_max_inputs": 64,
        "embedding_batch_max_tokens": 60000,
        "embedding_concurrency": 2,
        "index_batch_max_documents": 100,
        "index_batch_max_bytes": 8_000_000,
        "table_summary_concurrency": 4,
        "table_summary_max_input_tokens": 6000,
        "table_summary_max_output_tokens": 300,
    }
    values.update(overrides)
    return ItemSettings(**values)


def table(label: str) -> str:
    return f"<table><tbody><tr><td>{label} value</td></tr></tbody></table>"


def job(label: str, page: int = 1, title: str | None = None) -> TableJob:
    html = table(label)
    return TableJob(table_sha256(html), html, page, title)


def reply(text: str | None = "A table of values.", **fields: Any) -> SummaryReply:
    values: dict[str, Any] = {"finish_reason": "stop", "prompt_tokens": 50, "completion_tokens": 9}
    values.update(fields)
    return SummaryReply(content=text, **values)


# --- The output check (T-186) ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "outcome"),
    [
        (reply("Quarterly revenue by region in EUR, with a total row."), "summarised"),
        (reply("  Padded summary.  "), "summarised"),
        (reply("NO_SUMMARY"), "discarded"),
        (reply(""), "discarded"),
        (reply(None), "discarded"),
        (reply("See https://example.net for more."), "discarded"),
        (reply("See HTTP://EXAMPLE.NET."), "discarded"),
        (reply("Visit www.example.net."), "discarded"),
        (reply("A <b>bold</b> claim."), "discarded"),
        (reply("Closing </table> tag."), "discarded"),
        (reply("A [link](somewhere)."), "discarded"),
        (reply("Cut off", finish_reason="length"), "discarded"),
        (reply("Filtered", finish_reason="content_filter"), "filtered"),
        (reply("I can't help", refusal="refused"), "filtered"),
        (SummaryReply(None, None, content_filtered=True), "filtered"),
        (reply("Values 3 < 5 and 7 > 2."), "summarised"),
    ],
)
def test_t186_the_output_check(answer: SummaryReply, outcome: str, tokenizer: Tokenizer) -> None:
    result, summary = check_output(answer, 300, tokenizer)

    assert result == outcome
    assert (summary is not None) == (outcome == "summarised")
    if summary is not None:
        assert summary == summary.strip()


def test_t186_an_output_above_the_token_cap_is_discarded(tokenizer: Tokenizer) -> None:
    words = " ".join(["word"] * 151)

    assert check_output(reply(words), 300, tokenizer)[0] == "discarded"
    assert check_output(reply(" ".join(["word"] * 150)), 300, tokenizer)[0] == "summarised"


# --- The request (T-185) --------------------------------------------------------------------------


@dataclass
class _Completions:
    answer: Any = None
    failure: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def create(self, **arguments: Any) -> Any:
        self.calls.append(arguments)
        if self.failure is not None:
            raise self.failure
        return self.answer


def _client(completions: _Completions) -> openai.AsyncOpenAI:
    return cast(
        "openai.AsyncOpenAI", SimpleNamespace(chat=SimpleNamespace(completions=completions))
    )


def _completion(
    content: str | None, finish_reason: str = "stop", refusal: str | None = None
) -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, refusal=refusal),
                finish_reason=finish_reason,
            )
        ],
        usage=SimpleNamespace(prompt_tokens=120, completion_tokens=40),
    )


async def test_t185_the_request_sends_low_reasoning_and_no_sampling() -> None:
    completions = _Completions(answer=_completion("A summary."))
    model = OpenAISummaryModel(_client(completions), "ingest-summary")

    answer = await model.complete(SYSTEM_MESSAGE, "the user message")

    assert answer == SummaryReply("A summary.", "stop", None, 120, 40)
    assert completions.calls == [
        {
            "model": "ingest-summary",
            "messages": [
                {"role": "system", "content": SYSTEM_MESSAGE},
                {"role": "user", "content": "the user message"},
            ],
            "reasoning_effort": "low",
            "max_completion_tokens": 2000,
            "stream": False,
        }
    ]
    for never in ("temperature", "top_p", "tools", "response_format"):
        assert never not in completions.calls[0]
    assert model.deployment == "ingest-summary"


async def test_a_reply_without_choices_or_usage() -> None:
    completions = _Completions(answer=SimpleNamespace(choices=[], usage=None))

    answer = await OpenAISummaryModel(_client(completions), "d").complete("s", "u")

    assert answer == SummaryReply(None, None, None, 0, 0)


def _response(status: int, headers: dict[str, str] | None = None) -> Any:
    return SimpleNamespace(request=None, status_code=status, headers=headers or {})


async def test_a_content_filter_400_is_a_filtered_reply() -> None:
    failure = openai.BadRequestError(
        "filtered", response=_response(400), body={"code": "content_filter"}
    )
    completions = _Completions(failure=failure)

    answer = await OpenAISummaryModel(_client(completions), "d").complete("s", "u")

    assert answer.content_filtered


async def test_other_client_errors_reach_the_boundary() -> None:
    failure = openai.BadRequestError("bad", response=_response(400), body={"code": "other"})
    with pytest.raises(openai.BadRequestError):
        await OpenAISummaryModel(_client(_Completions(failure=failure)), "d").complete("s", "u")
    denied = openai.PermissionDeniedError("no", response=_response(403), body=None)
    with pytest.raises(openai.PermissionDeniedError):
        await OpenAISummaryModel(_client(_Completions(failure=denied)), "d").complete("s", "u")


@pytest.mark.parametrize(
    ("failure", "retry_after"),
    [
        (
            openai.RateLimitError("slow", response=_response(429, {"retry-after": "7"}), body=None),
            7,
        ),
        (
            openai.InternalServerError(
                "x", response=_response(503, {"retry-after-ms": "1500"}), body=None
            ),
            2,
        ),
        (openai.APITimeoutError(request=cast("Any", None)), None),
        (openai.APIConnectionError(request=cast("Any", None)), None),
    ],
)
async def test_unavailability_carries_the_wait(
    failure: Exception, retry_after: int | None, metric_points: MetricPoints
) -> None:
    model = OpenAISummaryModel(_client(_Completions(failure=failure)), "ingest-summary")

    with pytest.raises(SummaryUnavailableError) as unavailable:
        await model.complete("s", "u")

    assert unavailable.value.retry_after_seconds == retry_after
    throttled = [
        point
        for point in metric_points("docchat.ingestion.throttled")
        if point.attributes.get("service") == "openai"
    ]
    assert len(throttled) == (1 if isinstance(failure, openai.RateLimitError) else 0)


# --- Tables and the summaries file ----------------------------------------------------------------


def _element(kind: str, text: str = "", html: str | None = None) -> ExtractElement:
    return ExtractElement(kind=kind, text=text, table_html=html, heading_level=None)  # type: ignore[arg-type]


def _part(pages: dict[int, list[ExtractElement]], first: int, last: int) -> ExtractPartV1:
    return ExtractPartV1(
        format_version=1,
        method="pagewise",
        page_count=10,
        first_page=first,
        last_page=last,
        pages=[ExtractPage(page_number=page, elements=items) for page, items in pages.items()],
    )


def test_table_jobs_list_distinct_tables_with_the_nearest_title() -> None:
    parts = [
        _part(
            {
                1: [_element("title", "Sales"), _element("table", "t", table("a"))],
                2: [_element("table", "t", table("b")), _element("title", "Later")],
            },
            1,
            2,
        ),
        _part({3: [_element("title", "Again"), _element("table", "t", table("a"))]}, 3, 4),
    ]

    assert table_jobs(parts) == [job("a", 1, "Sales"), job("b", 2, None)]


def test_the_summaries_file_round_trips() -> None:
    lines = [
        TableSummaryLine(
            table_sha256="a" * 64,
            page_number=2,
            outcome=SummaryOutcome.SUMMARISED,
            summary="A summary.",
            input_tokens=10,
            output_tokens=3,
        ),
        TableSummaryLine(
            table_sha256="b" * 64,
            page_number=3,
            outcome=SummaryOutcome.FILTERED,
            summary=None,
            input_tokens=10,
            output_tokens=0,
        ),
    ]

    raw = encode_table_summaries(lines)

    assert raw.decode("utf-8").splitlines()[0] == (
        '{"tableSha256":"' + "a" * 64 + '","pageNumber":2,"outcome":"summarised",'
        '"summary":"A summary.","inputTokens":10,"outputTokens":3}'
    )
    assert decode_table_summaries(raw + b"\n") == lines
    assert decode_table_summaries(None) == []
    assert summary_texts(lines) == {"a" * 64: "A summary."}


# --- The summariser -------------------------------------------------------------------------------


class _Model:
    """A `SummaryModel` that answers from a table of replies by table label."""

    deployment = "ingest-summary"

    def __init__(
        self,
        replies: dict[str, SummaryReply | Exception] | None = None,
        *,
        clock: FakeClock | None = None,
        seconds: float = 0,
    ) -> None:
        self.replies = replies or {}
        self.messages: list[str] = []
        self.in_flight = 0
        self.most_in_flight = 0
        self.clock = clock
        self.seconds = seconds

    async def complete(self, system: str, user: str) -> SummaryReply:
        assert system == SYSTEM_MESSAGE
        self.messages.append(user)
        self.in_flight += 1
        self.most_in_flight = max(self.most_in_flight, self.in_flight)
        await asyncio.sleep(0)
        if self.clock is not None:
            self.clock.advance(self.seconds)
        self.in_flight -= 1
        label = next((key for key in self.replies if f"{key} value" in user), None)
        answer = self.replies.get(label, reply()) if label else reply()
        if isinstance(answer, Exception):
            raise answer
        return answer


class _Saves:
    def __init__(self) -> None:
        self.saved: list[list[TableSummaryLine]] = []

    async def __call__(self, lines: Sequence[TableSummaryLine]) -> None:
        self.saved.append(list(lines))


def summariser(
    model: _Model,
    tokenizer: Tokenizer,
    saves: _Saves,
    clock: FakeClock | None = None,
    **overrides: Any,
) -> TableSummariser:
    return TableSummariser(model, tokenizer, settings(**overrides), clock or FakeClock(), saves)


async def test_every_table_gets_one_line_and_the_counts(
    tokenizer: Tokenizer, metric_points: MetricPoints
) -> None:
    model = _Model({"b": reply("NO_SUMMARY"), "c": reply(None, refusal="no")})
    saves = _Saves()
    jobs = [job("a", 1, "Revenue"), job("b", 2), job("c", 3)]

    result = await summariser(model, tokenizer, saves).run(jobs, [])

    assert result.done
    assert (result.tables, result.summarised, result.discarded, result.filtered) == (3, 1, 1, 1)
    assert (result.input_tokens, result.output_tokens) == (150, 27)
    assert set(result.warnings) == {
        ItemWarning(code="table_summary_discarded", page=2),
        ItemWarning(code="table_summary_filtered", page=3),
    }
    assert len(saves.saved) == 1
    assert {line.table_sha256: line.outcome for line in saves.saved[0]} == {
        jobs[0].sha256: "summarised",
        jobs[1].sha256: "discarded",
        jobs[2].sha256: "filtered",
    }
    assert any("nearby title: Revenue." in message for message in model.messages)
    tokens = metric_points("docchat.ingestion.tokens")
    assert {point.attributes["kind"] for point in tokens} == {"summary_input", "summary_output"}
    assert (
        sum(point.value for point in tokens if point.attributes["kind"] == "summary_input") == 150
    )


async def test_tables_already_in_the_file_are_skipped(tokenizer: Tokenizer) -> None:
    model = _Model()
    saves = _Saves()
    done = TableSummaryLine(
        table_sha256=job("a").sha256,
        page_number=1,
        outcome=SummaryOutcome.SUMMARISED,
        summary="Earlier.",
        input_tokens=1,
        output_tokens=1,
    )

    result = await summariser(model, tokenizer, saves).run([job("a"), job("b")], [done])

    assert result.done
    assert result.tables == 1
    assert len(model.messages) == 1
    assert saves.saved[-1][0] == done
    nothing = await summariser(model, tokenizer, _Saves()).run([job("a")], [done])
    assert nothing.done
    assert nothing.tables == 0


async def test_a_table_without_cell_text_is_skipped_without_a_call(tokenizer: Tokenizer) -> None:
    empty = "<table><tbody><tr><td> </td></tr></tbody></table>"
    model = _Model()
    saves = _Saves()

    result = await summariser(model, tokenizer, saves).run(
        [TableJob(table_sha256(empty), empty, 4, None)], []
    )

    assert model.messages == []
    assert saves.saved[0][0].outcome == "skipped"
    assert (result.tables, result.summarised, result.warnings) == (1, 0, ())


async def test_a_long_table_is_cut_with_a_warning(tokenizer: Tokenizer) -> None:
    rows = "".join(f"<tr><td>row {index} value</td></tr>" for index in range(200))
    html = f"<table><tbody>{rows}</tbody></table>"
    model = _Model()

    result = await summariser(model, tokenizer, _Saves(), table_summary_max_input_tokens=500).run(
        [TableJob(table_sha256(html), html, 7, None)], []
    )

    assert ItemWarning(code="table_summary_input_truncated", page=7) in result.warnings
    assert "[rows omitted]" in model.messages[0]
    assert "row 199 value" not in model.messages[0]


async def test_concurrency_and_saving_every_16_tables(tokenizer: Tokenizer) -> None:
    model = _Model()
    saves = _Saves()
    jobs = [job(f"t{index}") for index in range(35)]

    result = await summariser(model, tokenizer, saves, table_summary_concurrency=3).run(jobs, [])

    assert result.done
    assert model.most_in_flight == 3
    assert [len(lines) for lines in saves.saved] == [16, 32, 35]


async def test_the_time_budget_ends_the_call_with_done_false(tokenizer: Tokenizer) -> None:
    clock = FakeClock()
    model = _Model(clock=clock, seconds=700)
    saves = _Saves()
    jobs = [job(f"t{index}") for index in range(5)]

    result = await summariser(
        model,
        tokenizer,
        saves,
        clock,
        table_summary_concurrency=1,
        activity_time_budget_seconds=1200,
    ).run(jobs, [])

    assert not result.done
    assert result.tables == 2
    assert len(saves.saved[-1]) == 2


async def test_unavailability_saves_progress_and_fails_retryably(tokenizer: Tokenizer) -> None:
    model = _Model(
        {
            "t1": SummaryUnavailableError(30),
            "t2": SummaryUnavailableError(None),
            "t3": SummaryUnavailableError(90),
        }
    )
    saves = _Saves()
    jobs = [job(f"t{index}") for index in range(6)]

    with pytest.raises(MethodFailedError) as failed:
        await summariser(model, tokenizer, saves, table_summary_concurrency=4).run(jobs, [])

    assert failed.value.code == "table_summary_unavailable"
    assert failed.value.retry_after_seconds == 90
    assert [line.table_sha256 for line in saves.saved[-1]] == [jobs[0].sha256]
    assert len(model.messages) == 4


async def test_an_unexpected_failure_saves_the_finished_tables_first(
    tokenizer: Tokenizer,
) -> None:
    model = _Model({"t1": RuntimeError("unexpected")})
    saves = _Saves()
    jobs = [job("t0"), job("t1"), job("t2")]

    with pytest.raises(RuntimeError, match="unexpected"):
        await summariser(model, tokenizer, saves, table_summary_concurrency=1).run(jobs, [])

    assert [[line.table_sha256 for line in lines] for lines in saves.saved] == [[jobs[0].sha256]]
    assert len(model.messages) == 2


async def test_a_call_without_usage_counts_no_tokens(
    tokenizer: Tokenizer, metric_points: MetricPoints
) -> None:
    model = _Model({"a": reply(prompt_tokens=0, completion_tokens=0)})

    result = await summariser(model, tokenizer, _Saves()).run([job("a")], [])

    assert (result.summarised, result.input_tokens) == (1, 0)
    assert metric_points("docchat.ingestion.tokens") == []


async def test_an_unexpected_failure_cancels_the_other_calls(tokenizer: Tokenizer) -> None:
    started = asyncio.Event()

    class _Blocking(_Model):
        @override
        async def complete(self, system: str, user: str) -> SummaryReply:
            if "slow value" in user:
                started.set()
                await asyncio.Event().wait()
            await started.wait()
            raise RuntimeError("unexpected")

    saves = _Saves()
    with pytest.raises(RuntimeError, match="unexpected"):
        await summariser(_Blocking(), tokenizer, saves).run([job("slow"), job("fast")], [])
    assert saves.saved == []
