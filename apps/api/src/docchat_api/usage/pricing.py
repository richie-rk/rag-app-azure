"""The price table and the cost of a model call (`12-chat-pipeline.md` sections 15.1 and 15.4).

`pricing.model_rates` holds each model's rates in currency units per 1,000,000 tokens as
`decimal.Decimal`, so tokens times a rate is directly in micro-units (10⁻⁶ of `pricing.currency`):

    cost = (prompt - cached - cache_write) * input + cached * cached_input
         + cache_write * cache_write_rate + completion * output

rounded half-up to an integer, never through binary floating point. A model without an entry has
no cost (`None`) and increments `docchat.pricing.rate_missing`; `prod` opens to users only when
every deployed model has a rate.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from docchat_api.usage.telemetry import record_rate_missing
from docchat_core.cosmos.conversations import CallOutcome, ModelCall, ModelCallUsage
from docchat_core.opsconfig import PricingOps

_WHOLE: Final = Decimal(1)


def estimate_cost_micros(
    pricing: PricingOps,
    model: str,
    *,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> int | None:
    """The call's estimated cost in micro-units, or `None` (and the missing-rate count) when the
    model has no rate."""
    rate = pricing.model_rates.get(model)
    if rate is None:
        record_rate_missing(model)
        return None
    # Cached and cache-write tokens are part of the prompt; a response that reports more of them
    # than prompt tokens never makes the uncached share negative.
    uncached = max(0, prompt_tokens - cached_tokens - cache_write_tokens)
    cost = (
        uncached * rate.input_per_million
        + cached_tokens * rate.cached_input_per_million
        + cache_write_tokens * rate.cache_write_per_million
        + completion_tokens * rate.output_per_million
    )
    return int(cost.quantize(_WHOLE, rounding=ROUND_HALF_UP))


def model_call_usage(
    pricing: PricingOps,
    *,
    call: ModelCall,
    deployment: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    reasoning_tokens: int = 0,
    cached_tokens: int = 0,
    cache_write_tokens: int = 0,
    estimated: bool,
    duration_ms: int,
    outcome: CallOutcome,
) -> ModelCallUsage:
    """One call's `ModelCallUsage`: `totalTokens` = prompt + completion, the cost priced from the
    snapshot's table."""
    return ModelCallUsage(
        call=call,
        deployment=deployment,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        reasoning_tokens=reasoning_tokens,
        cached_tokens=cached_tokens,
        cache_write_tokens=cache_write_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        estimated=estimated,
        estimated_cost_micros=estimate_cost_micros(
            pricing,
            model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=cached_tokens,
            cache_write_tokens=cache_write_tokens,
        ),
        duration_ms=duration_ms,
        outcome=outcome,
    )
