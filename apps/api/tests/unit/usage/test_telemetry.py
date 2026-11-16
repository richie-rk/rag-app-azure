from __future__ import annotations

import logging
from typing import Protocol

from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from docchat_api.usage.telemetry import (
    record_model_metrics,
    record_rate_missing,
    record_rejection,
    report_record_failed,
)
from docchat_core.cosmos.conversations import CallOutcome, ModelCall, ModelCallUsage


class Captured(Protocol):
    metrics: InMemoryMetricReader

    def points(self, metric_name: str) -> dict[tuple[tuple[str, object], ...], float]: ...

    def records(self, message: str) -> list[logging.LogRecord]: ...


def histogram_points(captured: Captured, name: str) -> list[dict[str, object]]:
    """The attribute sets of a histogram's data points (`points` reads sums only)."""
    data = captured.metrics.get_metrics_data()
    if data is None:
        return []
    return [
        dict(point.attributes or {})
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == name
        for point in metric.data.data_points
    ]


def _record(cost: int | None) -> ModelCallUsage:
    return ModelCallUsage(
        call=ModelCall.QUERY_EMBEDDING,
        deployment="embed-query-te3-large-v1",
        model="text-embedding-3-large",
        prompt_tokens=12,
        completion_tokens=0,
        reasoning_tokens=0,
        cached_tokens=0,
        cache_write_tokens=0,
        total_tokens=12,
        estimated=True,
        estimated_cost_micros=cost,
        duration_ms=10_000,
        outcome=CallOutcome.TIMED_OUT,
    )


def test_rejections_carry_their_reason(captured: Captured) -> None:
    for reason in ("rate", "concurrency", "quota_day", "quota_month"):
        record_rejection(reason)

    assert len(captured.points("docchat.ratelimit.rejections")) == 4


def test_a_missing_rate_names_the_model(captured: Captured) -> None:
    record_rate_missing("gpt-5.6-terra")

    assert captured.points("docchat.pricing.rate_missing") == {(("model", "gpt-5.6-terra"),): 1}


def test_an_unpriced_record_emits_no_cost_and_skips_zero_kinds(captured: Captured) -> None:
    record_model_metrics(_record(None))

    assert captured.points("docchat.openai.tokens") == {
        (
            ("call", "query_embedding"),
            ("deployment", "embed-query-te3-large-v1"),
            ("kind", "prompt"),
        ): 12
    }
    assert captured.points("docchat.openai.estimated_cost") == {}
    (duration,) = histogram_points(captured, "docchat.openai.duration")
    assert duration["outcome"] == "timed_out"


def test_a_failed_record_without_a_turn_logs_its_deployment(captured: Captured) -> None:
    report_record_failed(_record(2), session_id=None, turn_id=None)

    (failed,) = captured.records("usage_record_failed")
    assert failed.__dict__["docchat.deployment"] == "embed-query-te3-large-v1"
    assert "docchat.session_id" not in failed.__dict__
    assert captured.points("docchat.usage.record_failures") == {(): 1}
