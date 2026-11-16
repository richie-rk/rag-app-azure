"""`capacity_shares` (T-123's function half; `12-chat-pipeline.md` section 3.7; ADR 0147).

The seeded values are read from the frozen first revision, which writes them, and the `prod`
capacities from `infra/env/prod.json`, which creates the deployments, so the test proves the
invariant on what a first `prod` deploy holds.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue

from docchat_core.chat.capacity import SHARE_LIMIT, CapacityShares, capacity_shares
from docchat_core.chat.models import MODEL_CAPABILITIES
from docchat_core.opsconfig import OpsConfig, area_of, efforts_context, validate_area
from docchat_core.opsconfig.root import Area

AREAS: tuple[Area, ...] = ("chat", "ratelimit", "followups", "summary", "memory")
EFFORTS = MODEL_CAPABILITIES["gpt-5.6-terra"].reasoning_efforts


def _repository_file(*parts: str) -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent.joinpath(*parts)
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("/".join(parts))


def _seeded_rows() -> dict[str, JsonValue]:
    """Every `(key, default)` of the revision's `*_OPS_CONFIG` tuples."""
    source = _repository_file("db", "migrations", "versions", "0001_initial_schema.py")
    rows: dict[str, JsonValue] = {}
    for node in ast.parse(source.read_text(encoding="utf-8")).body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id.endswith("_OPS_CONFIG")
            and node.value is not None
        ):
            rows.update(dict(ast.literal_eval(node.value)))
    return rows


def _prod_capacities() -> tuple[int, int]:
    document: Any = json.loads(_repository_file("infra", "env", "prod.json").read_text("utf-8"))
    deployments = document["openAi"]["deployments"]
    return deployments["chat"]["capacity"], deployments["chatSmall"]["capacity"]


def _snapshot(**changes: JsonValue) -> OpsConfig:
    rows = _seeded_rows() | {name.replace("__", "."): value for name, value in changes.items()}
    return OpsConfig.model_validate(
        {
            area: validate_area(
                area,
                {key: value for key, value in rows.items() if area_of(key) == area},
                chat_small_efforts=EFFORTS,
            )
            for area in AREAS
        },
        context=efforts_context(EFFORTS),
    )


def test_t123_the_seeded_defaults_against_the_prod_capacities() -> None:
    chat_units, small_units = _prod_capacities()
    assert (chat_units, small_units) == (500, 530)

    shares = capacity_shares(_snapshot(), chat_units, small_units)

    assert shares.chat == pytest.approx(0.24)
    assert shares.small == pytest.approx(0.248, abs=0.0005)
    assert shares.small == 131_370 / 530_000
    assert shares.exceeded == ()


def test_the_chat_small_charge_of_section_3_7() -> None:
    shares = capacity_shares(_snapshot(), 500, 530)

    assert dict(shares.small_call_charges) == {
        "rewrite": 8_340,
        "search_passage": 5_100,
        "followups": 8_300,
        "memory": 7_700,
        "summary": 14_350,
    }
    assert shares.small_charge_tokens == 43_790
    assert shares.chat_charge_tokens == 40_000
    assert shares.requests_per_minute == 3


def test_four_requests_a_minute_exceed_both_shares() -> None:
    shares = capacity_shares(_snapshot(ratelimit__chat_requests_per_minute=4), 500, 530)

    assert shares.chat == pytest.approx(0.32)
    assert shares.exceeded == ("chat", "small")


@pytest.mark.parametrize(
    ("changes", "charge"),
    [
        ({"summary__max_summary_tokens": 1000}, 43_990),
        ({"chat__rewrite_history_turns": 8}, 43_790 + 4 * 610),
        ({"chat__small_call_max_completion_tokens": 6000}, 43_790 + 3 * 5200),
        ({"followups__source_excerpt_tokens": 800}, 43_790 + 3 * 500),
        ({"followups__source_count": 8}, 43_790 + 5 * 800),
        ({"memory__max_completion_tokens": 2000}, 44_590),
        ({"summary__max_input_tokens": 20_000}, 51_790),
        ({"summary__max_completion_tokens": 4000}, 45_790),
    ],
)
def test_every_input_of_s_counts(changes: dict[str, JsonValue], charge: int) -> None:
    assert capacity_shares(_snapshot(**changes), 500, 530).small_charge_tokens == charge


def test_effort_none_refused_needs_713_units() -> None:
    # Section 10.7: with `none` gone, the small calls get 6,000 completion tokens and S becomes
    # 59,390, which needs at least 713 chat-small units in `prod`.
    snapshot = _snapshot(chat__small_call_max_completion_tokens=6000)
    assert capacity_shares(snapshot, 500, 712).exceeded == ("small",)
    assert capacity_shares(snapshot, 500, 713).small_charge_tokens == 59_390
    assert capacity_shares(snapshot, 500, 713).exceeded == ()


def test_a_share_exactly_at_the_limit_passes() -> None:
    shares = capacity_shares(_snapshot(), 480, 530)
    assert shares.chat == SHARE_LIMIT
    assert shares.exceeded == ()


def test_an_unknown_capacity_leaves_its_share_unknown() -> None:
    shares = capacity_shares(_snapshot(ratelimit__chat_requests_per_minute=4), None, 530)
    assert shares.chat is None
    assert shares.exceeded == ("small",)
    assert capacity_shares(_snapshot(), None, None) == CapacityShares(
        chat=None,
        small=None,
        requests_per_minute=3,
        chat_charge_tokens=40_000,
        small_charge_tokens=43_790,
        small_call_charges=shares.small_call_charges,
    )


@pytest.mark.parametrize("units", [0, -1])
def test_a_capacity_below_one_unit_is_refused(units: int) -> None:
    with pytest.raises(ValueError, match="at least 1 unit"):
        capacity_shares(_snapshot(), units, 530)


def test_the_limit_is_a_quarter() -> None:
    assert SHARE_LIMIT == 0.25
