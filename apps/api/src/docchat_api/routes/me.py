"""`getCurrentUser` — `GET /v1/me` (`11-api.md` section 3.2.1; ADR 0021).

The caller as the validated token describes it, with the features the web client switches on.
Identity values come from the token, never from the stored row, so a role change shows as soon as
the client holds a new token; `features` comes from the request's ops-config snapshot, so a change
shows within 60 seconds. Like every `/v1` route it runs first-sign-in provisioning in the route
class, so the caller's `app.users` row exists afterwards; with the caller's last-seen write under
15 minutes old it makes no SQL call apart from the lazy ops-config refresh, which makes it the
cheap authenticated call of the load test.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import Field

from docchat_api.http.access import access
from docchat_api.http.models import ResponseModel
from docchat_api.http.routing import CurrentPrincipal, DocChatRoute
from docchat_api.http.state import request_ops
from docchat_core.auth import Principal
from docchat_core.auth.principal import DISPLAY_NAME_MAX_LENGTH, EMAIL_MAX_LENGTH
from docchat_core.opsconfig import OpsConfig

router = APIRouter(route_class=DocChatRoute)


class CurrentUserFeatures(ResponseModel):
    """The ops-config switches the web client reads: `memory.enabled` and
    `feedback.comments_enabled`."""

    memory_enabled: bool
    feedback_comments: bool


class CurrentUser(ResponseModel):
    """`getCurrentUser`'s answer."""

    user_id: UUID
    display_name: Annotated[str, Field(max_length=DISPLAY_NAME_MAX_LENGTH)]
    email: Annotated[str, Field(max_length=EMAIL_MAX_LENGTH)]
    roles: Annotated[list[Literal["Admin", "User"]], Field(min_length=1, max_length=2)]
    is_admin: bool
    features: CurrentUserFeatures


def current_user(principal: Principal, ops: OpsConfig) -> CurrentUser:
    return CurrentUser(
        user_id=principal.oid,
        display_name=principal.display_name,
        email=principal.email,
        roles=sorted(principal.roles),
        is_admin=principal.is_admin,
        features=CurrentUserFeatures(
            memory_enabled=ops.memory.enabled,
            feedback_comments=ops.feedback.comments_enabled,
        ),
    )


@router.get(
    "/me",
    operation_id="getCurrentUser",
    tags=["me"],
    response_model=CurrentUser,
)
@access("user")
async def get_current_user(
    principal: CurrentPrincipal, ops: Annotated[OpsConfig, Depends(request_ops)]
) -> CurrentUser:
    return current_user(principal, ops)
