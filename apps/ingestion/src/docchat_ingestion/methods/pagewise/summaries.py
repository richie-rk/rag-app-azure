"""The table-summary call of the page-wise Enrich step (`14-ingestion.md` sections 6.4 and 12;
ADR 0051).

One call per distinct table (tables with equal sanitised HTML share a summary), at most
`table_summary_concurrency` in flight, the summaries file rewritten after every 16 finished
tables and at the end, so a retry resumes after the last saved batch. A call works for at most
`activity_time_budget_seconds`, then saves and reports `done: false`. Throttling or service errors
after the client's retries save the progress and fail with retryable `table_summary_unavailable`.
A `400` refused with code `content_filter` is the outcome `filtered`; any other `400` is no
outcome of section 12.7, so it reaches the failure boundary as the retryable `internal_error`
(section 12.9 item 3) instead of silently leaving every table without a summary.

The output is checked before it is kept: a filtered or unusable summary leaves the table indexed
as its sanitised HTML alone, with a warning, so an injected instruction in a table can't carry a
link or markup into the index. Logs, spans and metrics carry counts only, never table HTML,
summaries or prompts.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol

import openai
from openai.types.chat import (
    ChatCompletionSystemMessageParam,
    ChatCompletionUserMessageParam,
)

from docchat_core.ingestion.methods import EnrichResult, MethodFailedError
from docchat_core.ingestion.models import ItemSettings, ItemWarning
from docchat_core.models import Clock
from docchat_ingestion import telemetry
from docchat_ingestion.isolation.results import ExtractPartV1
from docchat_ingestion.methods.pagewise.prompts import NO_SUMMARY, SYSTEM_MESSAGE, user_message
from docchat_ingestion.methods.retry import retry_after_seconds
from docchat_ingestion.methods.tables import fit_table, has_cell_text, table_sha256
from docchat_ingestion.methods.text import Tokenizer, count_tokens
from docchat_ingestion.workfiles import SummaryOutcome, TableSummaryLine

SAVE_EVERY: Final = 16
MAX_COMPLETION_TOKENS: Final = 2000
REASONING_EFFORT: Final = "low"
CONTENT_FILTER: Final = "content_filter"
LENGTH: Final = "length"
SERVER_ERROR: Final = 500

_LINK: Final = re.compile(r"https?://|www\.", re.IGNORECASE)
_MARKUP: Final = re.compile(r"<[A-Za-z/]")
_MARKDOWN_LINK: Final = "]("


def summary_texts(lines: Sequence[TableSummaryLine]) -> dict[str, str]:
    """Each summarised table's summary by the SHA-256 of its sanitised HTML."""
    return {
        line.table_sha256: line.summary
        for line in lines
        if line.outcome is SummaryOutcome.SUMMARISED and line.summary
    }


@dataclass(frozen=True, slots=True)
class TableJob:
    """One distinct table of the version: its key, sanitised HTML, page (the first page it
    appears on) and the nearest preceding title on that page."""

    sha256: str
    html: str
    page: int
    title: str | None


def table_jobs(parts: Sequence[ExtractPartV1]) -> list[TableJob]:
    """Every distinct table of the extract parts, in document order."""
    jobs: list[TableJob] = []
    seen: set[str] = set()
    for part in parts:
        for page in part.pages:
            title: str | None = None
            for element in page.elements:
                if element.kind == "title":
                    title = element.text
                elif element.kind == "table" and element.table_html is not None:
                    key = table_sha256(element.table_html)
                    if key not in seen:
                        seen.add(key)
                        jobs.append(TableJob(key, element.table_html, page.page_number, title))
    return jobs


@dataclass(frozen=True, slots=True)
class SummaryReply:
    """What the activity reads from one call: the message's content, refusal and finish reason,
    the usage, and whether the call was refused with `400 content_filter`."""

    content: str | None
    finish_reason: str | None
    refusal: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    content_filtered: bool = False


class SummaryUnavailableError(Exception):
    """Throttling, a server error, a timeout or a connection failure after the client's
    retries, with the wait the last response asked for."""

    def __init__(self, retry_after_seconds: int | None) -> None:
        super().__init__("table_summary_unavailable")
        self.retry_after_seconds = retry_after_seconds


class SummaryModel(Protocol):
    """The table-summary deployment, the port Enrich's unit tests fake."""

    @property
    def deployment(self) -> str: ...

    async def complete(self, system: str, user: str) -> SummaryReply: ...


class OpenAISummaryModel:
    """`SummaryModel` over the ingestion profile's `AsyncOpenAI` client (section 12.3):
    Chat Completions with `reasoning_effort="low"` and `max_completion_tokens=2000`, never
    `temperature`, `top_p`, tools or a response format, because the model reasons and the call
    has no tools."""

    def __init__(self, client: openai.AsyncOpenAI, deployment: str) -> None:
        self._client = client
        self._deployment = deployment

    @property
    def deployment(self) -> str:
        return self._deployment

    async def complete(self, system: str, user: str) -> SummaryReply:
        try:
            completion = await self._client.chat.completions.create(
                model=self._deployment,
                messages=[
                    ChatCompletionSystemMessageParam(role="system", content=system),
                    ChatCompletionUserMessageParam(role="user", content=user),
                ],
                reasoning_effort=REASONING_EFFORT,
                max_completion_tokens=MAX_COMPLETION_TOKENS,
                stream=False,
            )
        except openai.BadRequestError as failure:
            if failure.code == CONTENT_FILTER:
                return SummaryReply(content=None, finish_reason=None, content_filtered=True)
            raise
        except openai.RateLimitError as failure:
            telemetry.record_throttled("openai", self._deployment)
            raise SummaryUnavailableError(retry_after_seconds(failure.response.headers)) from None
        except openai.APIStatusError as failure:
            if failure.status_code < SERVER_ERROR:
                raise
            raise SummaryUnavailableError(retry_after_seconds(failure.response.headers)) from None
        except openai.APIConnectionError:
            raise SummaryUnavailableError(None) from None
        usage = completion.usage
        prompt_tokens = usage.prompt_tokens if usage is not None else 0
        completion_tokens = usage.completion_tokens if usage is not None else 0
        if not completion.choices:
            return SummaryReply(None, None, None, prompt_tokens, completion_tokens)
        choice = completion.choices[0]
        return SummaryReply(
            content=choice.message.content,
            finish_reason=choice.finish_reason,
            refusal=choice.message.refusal,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )


def check_output(
    reply: SummaryReply, max_output_tokens: int, tokenizer: Tokenizer
) -> tuple[SummaryOutcome, str | None]:
    """The outcome of one reply and the summary kept, if any (section 12.7)."""
    if reply.content_filtered or reply.finish_reason == CONTENT_FILTER or reply.refusal is not None:
        return SummaryOutcome.FILTERED, None
    text = (reply.content or "").strip()
    if (
        reply.finish_reason == LENGTH
        or not text
        or text == NO_SUMMARY
        or count_tokens(tokenizer, text) > max_output_tokens
        or _LINK.search(text)
        or _MARKUP.search(text)
        or _MARKDOWN_LINK in text
    ):
        return SummaryOutcome.DISCARDED, None
    return SummaryOutcome.SUMMARISED, text


_OUTCOME_WARNINGS: Final = {
    SummaryOutcome.FILTERED: "table_summary_filtered",
    SummaryOutcome.DISCARDED: "table_summary_discarded",
}


@dataclass(frozen=True, slots=True)
class _Finished:
    line: TableSummaryLine
    warnings: tuple[ItemWarning, ...]


@dataclass(slots=True)
class _Tally:
    tables: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    outcomes: dict[SummaryOutcome, int] = field(default_factory=dict)
    warnings: list[ItemWarning] = field(default_factory=list)

    def add(self, finished: _Finished) -> None:
        line = finished.line
        self.tables += 1
        self.input_tokens += line.input_tokens
        self.output_tokens += line.output_tokens
        self.outcomes[line.outcome] = self.outcomes.get(line.outcome, 0) + 1
        self.warnings.extend(finished.warnings)

    def result(self, *, done: bool) -> EnrichResult:
        return EnrichResult(
            done=done,
            tables=self.tables,
            summarised=self.outcomes.get(SummaryOutcome.SUMMARISED, 0),
            filtered=self.outcomes.get(SummaryOutcome.FILTERED, 0),
            discarded=self.outcomes.get(SummaryOutcome.DISCARDED, 0),
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            warnings=tuple(self.warnings),
        )


@dataclass(slots=True)
class _Progress:
    """One call's finished lines (the saved ones first), how many are unsaved, and the failures
    its calls met."""

    lines: list[TableSummaryLine]
    tally: _Tally = field(default_factory=_Tally)
    unsaved: int = 0
    failure: SummaryUnavailableError | None = None
    unexpected: BaseException | None = None
    keys: set[str] = field(init=False)

    def __post_init__(self) -> None:
        self.keys = {line.table_sha256 for line in self.lines}

    def take(self, task: asyncio.Task[_Finished]) -> bool:
        """Record a finished call; `True` when it added a line."""
        error = task.exception()
        if isinstance(error, SummaryUnavailableError):
            self.failure = _later(self.failure, error)
            return False
        if error is not None:
            self.unexpected = self.unexpected or error
            return False
        finished = task.result()
        self.lines.append(finished.line)
        self.keys.add(finished.line.table_sha256)
        self.tally.add(finished)
        self.unsaved += 1
        return True

    def raise_failure(self) -> None:
        if self.unexpected is not None:
            raise self.unexpected
        if self.failure is not None:
            raise MethodFailedError(
                "table_summary_unavailable", retry_after_seconds=self.failure.retry_after_seconds
            )


class TableSummariser:
    """One Enrich call's summaries: the model, the summary encoding's tokenizer, the item's
    settings snapshot, a clock for the time budget and the function that rewrites the file."""

    def __init__(
        self,
        model: SummaryModel,
        tokenizer: Tokenizer,
        settings: ItemSettings,
        clock: Clock,
        save: Callable[[Sequence[TableSummaryLine]], Awaitable[None]],
    ) -> None:
        self._model = model
        self._tokenizer = tokenizer
        self._settings = settings
        self._clock = clock
        self._save = save

    async def _summarise(self, job: TableJob) -> _Finished:
        if not has_cell_text(job.html):
            line = TableSummaryLine(
                table_sha256=job.sha256,
                page_number=job.page,
                outcome=SummaryOutcome.SKIPPED,
                summary=None,
                input_tokens=0,
                output_tokens=0,
            )
            return _Finished(line, ())
        # Counting a table of thousands of tokens, row by row, would hold the loop.
        table, truncated = await asyncio.to_thread(
            fit_table, job.html, self._settings.table_summary_max_input_tokens, self._tokenizer
        )
        reply = await self._model.complete(SYSTEM_MESSAGE, user_message(job.page, job.title, table))
        deployment = self._model.deployment
        telemetry.record_tokens(deployment, "summary_input", reply.prompt_tokens)
        telemetry.record_tokens(deployment, "summary_output", reply.completion_tokens)
        outcome, summary = check_output(
            reply, self._settings.table_summary_max_output_tokens, self._tokenizer
        )
        warnings: list[ItemWarning] = []
        if truncated:
            warnings.append(ItemWarning(code="table_summary_input_truncated", page=job.page))
        if outcome in _OUTCOME_WARNINGS:
            warnings.append(ItemWarning(code=_OUTCOME_WARNINGS[outcome], page=job.page))
        line = TableSummaryLine(
            table_sha256=job.sha256,
            page_number=job.page,
            outcome=outcome,
            summary=summary,
            input_tokens=reply.prompt_tokens,
            output_tokens=reply.completion_tokens,
        )
        return _Finished(line, tuple(warnings))

    def _start(
        self,
        in_flight: set[asyncio.Task[_Finished]],
        pending: Iterator[TableJob],
        progress: _Progress,
        started: float,
    ) -> None:
        """Start calls up to the concurrency, none after an unavailable service or the budget."""
        while (
            progress.failure is None
            and len(in_flight) < self._settings.table_summary_concurrency
            and self._clock.monotonic() - started < self._settings.activity_time_budget_seconds
        ):
            job = next(pending, None)
            if job is None:
                return
            in_flight.add(asyncio.create_task(self._summarise(job)))

    async def run(
        self, jobs: Sequence[TableJob], saved: Sequence[TableSummaryLine]
    ) -> EnrichResult:
        """Summarise every job not yet in `saved`; `done` once every job has its line.

        Whatever ends the call, the tables finished so far are saved before it returns or raises,
        so a retry resumes after them; an unexpected failure cancels the calls still in flight
        and reaches the failure boundary once the progress is saved."""
        progress = _Progress(list(saved))
        pending = iter([job for job in jobs if job.sha256 not in progress.keys])
        started = self._clock.monotonic()
        in_flight: set[asyncio.Task[_Finished]] = set()
        try:
            while progress.unexpected is None:
                self._start(in_flight, pending, progress, started)
                if not in_flight:
                    break
                done, in_flight = await asyncio.wait(in_flight, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if progress.take(task) and progress.unsaved == SAVE_EVERY:
                        await self._save(progress.lines)
                        progress.unsaved = 0
        finally:
            for task in in_flight:
                task.cancel()
            await asyncio.gather(*in_flight, return_exceptions=True)
        if progress.unsaved:
            await self._save(progress.lines)
        progress.raise_failure()
        return progress.tally.result(done=all(job.sha256 in progress.keys for job in jobs))


def _later(
    current: SummaryUnavailableError | None, new: SummaryUnavailableError
) -> SummaryUnavailableError:
    """The failure whose wait is longest, so the backoff honours every response."""
    if current is None or (new.retry_after_seconds or 0) > (current.retry_after_seconds or 0):
        return new
    return current
