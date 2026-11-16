"""The usage package's signals (`12-chat-pipeline.md` sections 2.3, 15.4, 15.6 and 19;
`85-observability.md` sections 4.8 and 4.9).

The rejections of the per-user limits (`docchat.ratelimit.rejections`, the counter the API's burst
and write limits also add to), the recorder's per-record model metrics, the counter-write failures
and the models without a price. Every value is metadata: deployment and model names, call kinds,
token counts and costs, never an identifier as a dimension. The instruments are created once, at
import.
"""

from __future__ import annotations

import logging
from typing import Final, Literal
from uuid import UUID

from docchat_core.cosmos.conversations import ModelCallUsage
from docchat_core.telemetry import LOGGER_NAME, log_event
from docchat_core.telemetry.instruments import counter, histogram

type RejectionReason = Literal[
    "rate", "concurrency", "quota_day", "quota_month", "download_rate", "download_concurrency"
]
"""The `reason` values the chat pipeline's limits record on `docchat.ratelimit.rejections`."""

USAGE_RECORD_FAILED: Final = "usage_record_failed"

RATELIMIT_REJECTIONS: Final = counter("docchat.ratelimit.rejections")
OPENAI_TOKENS: Final = counter("docchat.openai.tokens")
OPENAI_DURATION: Final = histogram("docchat.openai.duration")
OPENAI_ESTIMATED_COST: Final = counter("docchat.openai.estimated_cost")
USAGE_RECORD_FAILURES: Final = counter("docchat.usage.record_failures")
PRICING_RATE_MISSING: Final = counter("docchat.pricing.rate_missing")

_logger: Final = logging.getLogger(LOGGER_NAME)


def record_rejection(reason: RejectionReason) -> None:
    """One `429` of a per-user limit."""
    RATELIMIT_REJECTIONS.add(1, reason=reason)


def record_rate_missing(model: str) -> None:
    """A model call priced with no entry in `pricing.model_rates`."""
    PRICING_RATE_MISSING.add(1, model=model)


def record_model_metrics(record: ModelCallUsage) -> None:
    """The per-record metrics of section 15.6: tokens by kind, the call's duration and, when the
    cost is known, the cost."""
    dimensions = {"deployment": record.deployment, "call": record.call.value}
    for kind, amount in (
        ("prompt", record.prompt_tokens),
        ("completion", record.completion_tokens),
        ("reasoning", record.reasoning_tokens),
        ("cached", record.cached_tokens),
        ("cache_write", record.cache_write_tokens),
    ):
        if amount:
            OPENAI_TOKENS.add(amount, kind=kind, **dimensions)
    OPENAI_DURATION.record(record.duration_ms, outcome=record.outcome.value, **dimensions)
    if record.estimated_cost_micros is not None:
        OPENAI_ESTIMATED_COST.add(
            record.estimated_cost_micros, deployment=record.deployment, model=record.model
        )


def report_record_failed(
    record: ModelCallUsage, *, session_id: UUID | None, turn_id: UUID | None
) -> None:
    """A usage record whose counter writes failed after their attempts: one error record with
    metadata only, and the failure counter the alert `usage-recording-failures` reads."""
    attributes: dict[str, object] = {"docchat.deployment": record.deployment}
    if session_id is not None:
        attributes["docchat.session_id"] = str(session_id)
    if turn_id is not None:
        attributes["docchat.turn_id"] = str(turn_id)
    log_event(_logger, USAGE_RECORD_FAILED, logging.ERROR, **attributes)
    USAGE_RECORD_FAILURES.add(1)
