"""The per-user limits sized against model capacity: `capacity_shares` (`12-chat-pipeline.md`
section 3.7; ADR 0147).

The Azure OpenAI limiter charges each request its prompt tokens plus its `max_completion_tokens`
against the deployment's tokens per minute (capacity units * 1,000) when it arrives, so one user
at the request limit can reserve this share of a deployment:

- chat share = `ratelimit.chat_requests_per_minute` * `chat.context_token_budget` ÷ (chat
  capacity * 1,000);
- chat-small share = `ratelimit.chat_requests_per_minute` * `S` ÷ (chat-small capacity * 1,000),
  where `S` sums the five chat-small calls one turn and its after-response work can cause, each
  as its input allowance plus its `max_completion_tokens`.

In `prod` both shares stay at or below 25%. One function backs the unit test of the seeded
values, `docchat-ops chat capacity-check` and, through that command, the deployment's check.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from docchat_core.opsconfig import OpsConfig

SHARE_LIMIT: Final = 0.25
"""The largest share of a `prod` deployment one user at the request limit may reserve."""
TOKENS_PER_MINUTE_PER_UNIT: Final = 1_000

# The input allowances of section 3.7, in tokens.
CALL_FRAME_TOKENS: Final = 300
"""Each call's constant text and block lines."""
QUESTION_TOKENS: Final = 4_000
"""One token per code point of the longest question."""
EARLIER_EXCHANGE_TOKENS: Final = 610
"""One earlier exchange in the question rewrite: two 300-token cuts and their labels."""
SEARCH_PASSAGE_INPUT_TOKENS: Final = 4_300
FOLLOWUPS_INPUT_TOKENS: Final = 5_100
"""The follow-ups call's frame, question and 800-token answer excerpt."""
FOLLOWUPS_SOURCE_FRAME_TOKENS: Final = 500
"""Each follow-ups source's title, section and block lines, beside its excerpt."""
MEMORY_INPUT_TOKENS: Final = 6_500
"""The memory call's input allowance (`13-conversations-and-memory.md` section 7.13)."""
SUMMARY_FRAME_TOKENS: Final = 350
"""The summary call's rules and framing (`13-conversations-and-memory.md` section 6.7)."""

type SmallCall = Literal["rewrite", "search_passage", "followups", "memory", "summary"]


@dataclass(frozen=True, slots=True, kw_only=True)
class CapacityShares:
    """Both shares of one set of values, and what they are made of. A share is `None` when its
    deployment's capacity is unknown."""

    chat: float | None
    small: float | None
    requests_per_minute: int
    chat_charge_tokens: int
    """What one chat request can reserve: `chat.context_token_budget`."""
    small_charge_tokens: int
    """`S`, the sum of `small_call_charges`."""
    small_call_charges: Mapping[SmallCall, int]

    @property
    def exceeded(self) -> tuple[Literal["chat", "small"], ...]:
        """The deployments whose known share is above `SHARE_LIMIT`."""
        shares: tuple[tuple[Literal["chat", "small"], float | None], ...] = (
            ("chat", self.chat),
            ("small", self.small),
        )
        return tuple(name for name, share in shares if share is not None and share > SHARE_LIMIT)


def _share(
    requests_per_minute: int, charge_tokens: int, capacity_units: int | None
) -> float | None:
    if capacity_units is None:
        return None
    if capacity_units < 1:
        raise ValueError("a deployment's capacity is at least 1 unit")
    return requests_per_minute * charge_tokens / (capacity_units * TOKENS_PER_MINUTE_PER_UNIT)


def small_call_charges(ops: OpsConfig) -> Mapping[SmallCall, int]:
    """Each chat-small call's largest charge: its input allowance plus `max_completion_tokens`."""
    chat, followups = ops.chat, ops.followups
    summary, memory = ops.summary, ops.memory
    completion = chat.small_call_max_completion_tokens
    return MappingProxyType(
        {
            "rewrite": CALL_FRAME_TOKENS
            + summary.max_summary_tokens
            + EARLIER_EXCHANGE_TOKENS * chat.rewrite_history_turns
            + QUESTION_TOKENS
            + completion,
            "search_passage": SEARCH_PASSAGE_INPUT_TOKENS + completion,
            "followups": FOLLOWUPS_INPUT_TOKENS
            + (FOLLOWUPS_SOURCE_FRAME_TOKENS + followups.source_excerpt_tokens)
            * followups.source_count
            + completion,
            "memory": MEMORY_INPUT_TOKENS + memory.max_completion_tokens,
            "summary": summary.max_input_tokens
            + SUMMARY_FRAME_TOKENS
            + summary.max_completion_tokens,
        }
    )


def capacity_shares(
    ops: OpsConfig, chat_capacity_units: int | None, small_capacity_units: int | None
) -> CapacityShares:
    """The chat and chat-small shares of one user at `ratelimit.chat_requests_per_minute`.

    `ops` must hold the areas `chat`, `ratelimit`, `followups`, `summary` and `memory`. A capacity
    of `None` (an absent deployment output) leaves that share `None`; a capacity below 1 unit
    raises `ValueError`.
    """
    requests = ops.ratelimit.chat_requests_per_minute
    budget = ops.chat.context_token_budget
    charges = small_call_charges(ops)
    small = sum(charges.values())
    return CapacityShares(
        chat=_share(requests, budget, chat_capacity_units),
        small=_share(requests, small, small_capacity_units),
        requests_per_minute=requests,
        chat_charge_tokens=budget,
        small_charge_tokens=small,
        small_call_charges=charges,
    )
