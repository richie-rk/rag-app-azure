from __future__ import annotations

import re

from docchat_model_stub.recorder import Recorder, Stats


def test_requests_are_numbered_and_kept() -> None:
    recorder = Recorder(enabled=True)
    recorder.record("/openai/v1/chat/completions", "chat", {"model": "chat"})
    recorder.record("/contentsafety/text:shieldPrompt", None, {"documents": []})
    first, second = recorder.after(0)
    assert first.to_json()["seq"] == 1
    assert first.to_json()["path"] == "/openai/v1/chat/completions"
    assert first.to_json()["deployment"] == "chat"
    assert first.to_json()["body"] == {"model": "chat"}
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", first.time)
    assert second.seq == 2
    assert second.deployment is None
    assert [entry.seq for entry in recorder.after(1)] == [2]


def test_only_the_last_entries_are_kept() -> None:
    recorder = Recorder(enabled=True, capacity=3)
    for index in range(5):
        recorder.record("/p", "d", {"i": index})
    assert [entry.seq for entry in recorder.after(0)] == [3, 4, 5]


def test_clear_keeps_the_sequence_counting() -> None:
    recorder = Recorder(enabled=True)
    recorder.record("/p", "d", {})
    recorder.clear()
    assert recorder.after(0) == []
    recorder.record("/p", "d", {})
    assert [entry.seq for entry in recorder.after(0)] == [2]


def test_a_disabled_recorder_keeps_nothing() -> None:
    recorder = Recorder(enabled=False)
    recorder.record("/p", "d", {})
    assert recorder.after(0) == []


def test_stats_per_deployment() -> None:
    stats = Stats()
    stats.of("chat").started += 2
    stats.of("chat").completed += 1
    stats.of("chat-small").throttled += 1
    stats.of("chat").client_disconnected += 1
    assert stats.to_json() == {
        "chat": {"started": 2, "completed": 1, "throttled": 0, "clientDisconnected": 1},
        "chat-small": {"started": 0, "completed": 0, "throttled": 1, "clientDisconnected": 0},
    }
