from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from docchat_core.cosmos.containers import (
    CONTAINERS,
    CONVERSATIONS,
    MEMORIES,
    NO_DEFAULT_EXPIRY,
    USAGE,
)

type SpecTable = Callable[[str, str], list[list[str]]]


def _spec_text(name: str) -> str:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "docs" / "spec" / name
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    raise FileNotFoundError(name)


def _section(text: str, heading: str) -> str:
    start = text.index(heading + "\n") + len(heading)
    end = text.find("\n#", start)
    return text[start:end]


def _json_spans(text: str, first_key: str) -> list[Any]:
    return [json.loads(span) for span in re.findall(r"`(\{\"" + first_key + r"\".*?\})`", text)]


def test_the_three_containers() -> None:
    assert [container.name for container in CONTAINERS] == ["conversations", "memories", "usage"]


def test_partition_keys_equal_the_data_model() -> None:
    table = _section(_spec_text("80-data-model.md"), "### 3.1 Account and database")
    keys = {
        row.split("|")[1].strip(" `"): json.loads(re.findall(r"`(\{.*?\})`", row)[0])
        for row in table.splitlines()
        if row.startswith("| `") and '"paths"' in row
    }

    assert keys == {
        container.name: container.partition_key_definition() for container in CONTAINERS
    }
    assert CONVERSATIONS.partition_key_definition() == {
        "paths": ["/userId", "/sessionId"],
        "kind": "MultiHash",
        "version": 2,
    }


def test_the_sdk_partition_keys_carry_the_same_definition() -> None:
    for container in CONTAINERS:
        assert dict(container.partition_key()) == container.partition_key_definition()


def test_the_conversations_indexing_policy_equals_the_data_model(spec_table: SpecTable) -> None:
    rows = {
        row[0]: row[1]
        for row in spec_table("80-data-model.md", "### 3.3 Container `conversations`")
    }

    assert CONVERSATIONS.indexing_policy() == json.loads(rows["Indexing policy"])


@pytest.mark.parametrize(
    ("container", "heading"),
    [
        (MEMORIES, "### 3.8 Container `memories` and the `memory` document"),
        (USAGE, "### 3.9 Container `usage` and its documents"),
    ],
)
def test_point_access_containers_index_nothing(container: Any, heading: str) -> None:
    section = _section(_spec_text("80-data-model.md"), heading)

    assert _json_spans(section, "indexingMode") == [container.indexing_policy()]
    assert container.indexing_policy()["includedPaths"] == []


def test_indexing_policy_is_a_fresh_copy() -> None:
    policy = CONVERSATIONS.indexing_policy()
    policy["includedPaths"].clear()

    assert len(CONVERSATIONS.indexing_policy()["includedPaths"]) == 10


@pytest.mark.parametrize("container", [CONVERSATIONS, MEMORIES])
@pytest.mark.parametrize(
    ("days", "ttl"), [(0, NO_DEFAULT_EXPIRY), (1, 86_400), (30, 2_592_000), (3_650, 315_360_000)]
)
def test_retention_days_set_the_default_ttl(container: Any, days: int, ttl: int) -> None:
    assert container.default_ttl(days) == ttl


@pytest.mark.parametrize("days", [-1, 3_651])
def test_retention_outside_its_range_is_refused(days: int) -> None:
    with pytest.raises(ValueError, match="retention"):
        CONVERSATIONS.default_ttl(days)


def test_usage_never_expires_by_default_and_takes_no_retention() -> None:
    assert USAGE.default_ttl() == NO_DEFAULT_EXPIRY == -1
    assert USAGE.retention_parameter is None
    with pytest.raises(ValueError, match="no retention parameter"):
        USAGE.default_ttl(30)


def test_retention_parameters_are_the_environment_fields() -> None:
    assert CONVERSATIONS.retention_parameter == "conversationRetentionDays"
    assert MEMORIES.retention_parameter == "memoryRetentionDays"
