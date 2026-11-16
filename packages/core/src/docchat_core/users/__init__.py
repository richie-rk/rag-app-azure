"""Users: first-sign-in provisioning (`ensure_user`, the last-seen throttle) and the user search
query (`11-api.md` sections 3.2.1 and 3.8.1; `01-repository.md` section 2.2; ADR 0021)."""

from docchat_core.sql.repositories.users import UserPosition, UserRole, UserRow
from docchat_core.users.provisioning import (
    THROTTLE_CAPACITY,
    THROTTLE_SECONDS,
    LastSeenThrottle,
    ProvisioningOutcome,
    ensure_user,
)
from docchat_core.users.search import UserSearchPage, search_users

__all__ = [
    "THROTTLE_CAPACITY",
    "THROTTLE_SECONDS",
    "LastSeenThrottle",
    "ProvisioningOutcome",
    "UserPosition",
    "UserRole",
    "UserRow",
    "UserSearchPage",
    "ensure_user",
    "search_users",
]
