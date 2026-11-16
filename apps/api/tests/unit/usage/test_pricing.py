from __future__ import annotations

from typing import Protocol

import pytest

from docchat_api.usage.pricing import estimate_cost_micros, model_call_usage
from docchat_core.cosmos.conversations import CallOutcome, ModelCall
from docchat_core.opsconfig import PricingOps

PRICING = PricingOps.model_validate(
    {
        "pricing.currency": "USD",
        "pricing.model_rates": {
            "gpt-5.6-sol": {
                "inputPerMillion": "1.75",
                "cachedInputPerMillion": "0.175",
                "cacheWritePerMillion": "2.1875",
                "outputPerMillion": "14",
            },
            "text-embedding-3-large": {
                "inputPerMillion": "0.13",
                "cachedInputPerMillion": "0",
                "cacheWritePerMillion": "0",
                "outputPerMillion": "0",
            },
            "half": {
                "inputPerMillion": "0.5",
                "cachedInputPerMillion": "0",
                "cacheWritePerMillion": "0",
                "outputPerMillion": "0.000001",
            },
        },
    }
)


class Captured(Protocol):
    def points(self, metric_name: str) -> dict[tuple[tuple[str, object], ...], float]: ...


def test_1000_tokens_at_1_75_per_million_are_1750_micro_units() -> None:
    assert (
        estimate_cost_micros(PRICING, "gpt-5.6-sol", prompt_tokens=1_000, completion_tokens=0)
        == 1_750
    )


def test_every_token_kind_takes_its_rate() -> None:
    cost = estimate_cost_micros(
        PRICING,
        "gpt-5.6-sol",
        prompt_tokens=10_000,
        completion_tokens=2_000,
        cached_tokens=4_000,
        cache_write_tokens=1_000,
    )

    # 5,000 x 1.75 + 4,000 x 0.175 + 1,000 x 2.1875 + 2,000 x 14 = 39,637.5, rounded once
    assert cost == 39_638


@pytest.mark.parametrize(
    ("prompt", "completion", "expected"),
    [
        (1, 0, 1),  # 0.5 rounds half up
        (3, 0, 2),  # 1.5 rounds half up
        (2, 0, 1),
        (0, 499_999, 0),  # 0.499999
        (0, 500_000, 1),  # 0.5
    ],
)
def test_the_cost_rounds_half_up(prompt: int, completion: int, expected: int) -> None:
    cost = estimate_cost_micros(PRICING, "half", prompt_tokens=prompt, completion_tokens=completion)

    assert cost == expected


def test_reported_cached_tokens_never_make_the_input_negative() -> None:
    cost = estimate_cost_micros(
        PRICING, "gpt-5.6-sol", prompt_tokens=100, completion_tokens=0, cached_tokens=200
    )

    assert cost == 35  # 200 x 0.175


def test_a_model_without_a_rate_has_no_cost(captured: Captured) -> None:
    cost = estimate_cost_micros(PRICING, "gpt-5.6-terra", prompt_tokens=10, completion_tokens=10)

    assert cost is None
    assert captured.points("docchat.pricing.rate_missing") == {(("model", "gpt-5.6-terra"),): 1}


def test_a_record_totals_prompt_and_completion_and_prices_itself() -> None:
    record = model_call_usage(
        PRICING,
        call=ModelCall.QUERY_EMBEDDING,
        deployment="embed-query-te3-large-v1",
        model="text-embedding-3-large",
        prompt_tokens=12,
        completion_tokens=0,
        estimated=False,
        duration_ms=40,
        outcome=CallOutcome.OK,
    )

    assert record.total_tokens == 12
    assert record.estimated_cost_micros == 2  # 1.56 rounds to 2
    assert (record.reasoning_tokens, record.cached_tokens, record.cache_write_tokens) == (0, 0, 0)
