"""The chat pipeline's signals (`12-chat-pipeline.md` section 19): every metric, event and record
it emits passes the attribute registry."""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from docchat_api.chat import telemetry
from docchat_core.telemetry import LOGGER_NAME


def test_metrics_carry_their_registered_dimensions(captured: Any) -> None:
    telemetry.record_heartbeat()
    telemetry.record_turn("failed", "model_busy", 1200)
    telemetry.record_turn("completed", None, 900)
    telemetry.record_turn("failed", "free text", 10)
    telemetry.record_time_to_first_token(800, broadened=True, deployment="chat")
    telemetry.record_broadened_search("replaced")
    telemetry.record_screening_failure("throttled")
    telemetry.record_prompt(5_000, dropped_turns=2, dropped_sources=1)
    telemetry.record_disconnect("before_finalisation")
    telemetry.record_drained_turn()
    telemetry.record_first_chunk(300, "chat")
    telemetry.record_throttled("chat", "answer")
    telemetry.record_search(40, "first")
    telemetry.record_rerank_partial()

    turns = captured.points("docchat.chat.turns")
    assert turns[(("error_code", "model_busy"), ("outcome", "failed"))] == 1
    assert turns[(("error_code", "none"), ("outcome", "completed"))] == 1
    assert turns[(("error_code", "none"), ("outcome", "failed"))] == 1
    assert captured.points("docchat.chat.prompt.dropped") == {
        (("kind", "turn"),): 2,
        (("kind", "source"),): 1,
    }
    assert telemetry.known_code("model_busy") == "model_busy"
    assert telemetry.known_code("nonsense") is None


def test_events_and_records_hold_identifiers_and_codes_only(captured: Any) -> None:
    logger = logging.getLogger(LOGGER_NAME)
    level = logger.level
    logger.setLevel(logging.INFO)
    try:
        _emit_events_and_records(captured)
    finally:
        logger.setLevel(level)


def _emit_events_and_records(captured: Any) -> None:
    ids = {"project_id": uuid4(), "session_id": uuid4(), "turn_id": uuid4()}

    telemetry.report_flagged_source(
        document_id=uuid4(), chunk_set_id=uuid4(), index_id=uuid4(), **ids
    )
    telemetry.report_content_filter(where="prompt", call="rewrite", deployment="chat-small", **ids)
    telemetry.report_content_filter_degraded(deployment="chat", **ids)
    telemetry.report_unexpected(RuntimeError("cnry-answer-0000000a"))
    telemetry.report_drained(turn_tasks=1, background_tasks=0, downloads=0)
    telemetry.report_turn_finished(
        {
            "docchat.project_id": str(ids["project_id"]),
            "docchat.session_id": str(ids["session_id"]),
            "docchat.turn_id": str(ids["turn_id"]),
            "docchat.outcome": "completed",
        }
    )

    assert len(captured.events("source_screening_flagged")) == 1
    assert captured.events("content_filter_triggered")[0]["where"] == "prompt"
    assert len(captured.events("content_filter_degraded")) == 1
    (failed,) = captured.records("request_failed")
    assert failed.__dict__["error.type"] == "builtins.RuntimeError"
    assert "cnry" not in str(failed.__dict__)
    assert len(captured.records("chat_turn_finished")) == 1
    assert sum(captured.points("docchat.chat.screening.flagged_sources").values()) == 1
