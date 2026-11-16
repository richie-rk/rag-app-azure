"""The objects one worker process serves with, and the request's ops-config snapshot
(`11-api.md` sections 1.15 and 1.16; ADR 0028).

The lifespan builds one `ApiProcess` per worker after the fork and keeps it in `app.state`; the
route class and the endpoints read it through `current_process`. Every request takes one immutable
ops-config snapshot when it first needs one (`request_ops`), so a value changed while a request
runs never mixes two configurations in it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from fastapi import Request

from docchat_api.http.ratelimit import BurstLimiterPort, WriteLimiterPort
from docchat_api.settings import ProcessSettings
from docchat_core.access import AccessChecks
from docchat_core.auth import TokenValidator
from docchat_core.opsconfig import OpsConfig, OpsConfigStore
from docchat_core.sql.engine import SqlEngine
from docchat_core.users import LastSeenThrottle

if TYPE_CHECKING:
    from docchat_api.chat.service import ChatService
    from docchat_api.conversations.store import ConversationStore
    from docchat_api.usage import UsageService

PROCESS_STATE: Final = "docchat_process"
OPS_STATE: Final = "docchat_ops"


@dataclass(frozen=True, slots=True, kw_only=True)
class ApiProcess:
    """The worker's settings, data-access layer, ops-config store, access checks, last-seen
    throttle, token validator, request limiters, usage service, conversation store and chat
    service."""

    settings: ProcessSettings
    sql: SqlEngine
    ops: OpsConfigStore
    access: AccessChecks
    throttle: LastSeenThrottle
    validator: TokenValidator
    burst_limiter: BurstLimiterPort
    write_limiter: WriteLimiterPort
    usage: UsageService
    conversations: ConversationStore
    chat: ChatService


class ProcessNotStartedError(RuntimeError):
    """A request reached a route before the lifespan built the process (a defect: Uvicorn serves
    only after the lifespan start-up)."""


def current_process(request: Request) -> ApiProcess:
    """The worker's `ApiProcess`; endpoints take it with `Depends(current_process)`."""
    process = getattr(request.app.state, PROCESS_STATE, None)
    if not isinstance(process, ApiProcess):
        raise ProcessNotStartedError("the API process isn't started")
    return process


async def request_ops(request: Request) -> OpsConfig:
    """The request's ops-config snapshot, taken on first use and kept for the rest of the
    request; endpoints take it with `Depends(request_ops)`."""
    snapshot = getattr(request.state, OPS_STATE, None)
    if isinstance(snapshot, OpsConfig):
        return snapshot
    snapshot = await current_process(request).ops.snapshot()
    setattr(request.state, OPS_STATE, snapshot)
    return snapshot
