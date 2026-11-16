"""The authorisation refusals (`11-api.md` sections 1.4.2 and 1.4.3; ADR 0020).

`403` is used only for a missing role (`role_not_assigned`, raised by token validation) and for
a non-administrator on an administrator route (`AdminRequired`); every missing or unreachable
resource is a `404` whose body is the same as for a random identifier, so existence never leaks.
"""

from __future__ import annotations

from docchat_core.errors import DocChatProblem


class AdminRequired(DocChatProblem):
    """`403 admin_required`: an administrator route called without the `Admin` role."""

    code = "admin_required"


class ProjectNotFound(DocChatProblem):
    """`404 project_not_found`: the project is absent, archived, or not reachable by the caller,
    all alike. The session, turn and citation paths answer their own `404` code instead."""

    code = "project_not_found"


class ProjectIndexUnavailable(DocChatProblem):
    """`409 project_index_unavailable`: no unexpired override and no default mapping, or no
    active physical index behind the chosen logical index."""

    code = "project_index_unavailable"
