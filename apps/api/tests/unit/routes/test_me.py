from __future__ import annotations

from typing import Any, Protocol
from uuid import uuid4

from docchat_api.routes.me import CurrentUser, current_user
from docchat_core.auth import Principal
from docchat_core.opsconfig import encode_value
from docchat_core.sql.engine import SqlUnavailable
from docchat_core.sql.repositories.users import ProvisionResult
from docchat_testing.clock import FakeClock
from docchat_testing.fixtures.api import ApiUnderTest
from docchat_testing.fixtures.identities import SharedPrincipal


class FakeSql(Protocol):
    rows: dict[str, str]
    provision: ProvisionResult

    def names(self) -> list[str]: ...


async def test_the_caller_as_the_token_describes_them(
    started: ApiUnderTest, alice: SharedPrincipal
) -> None:
    response = await started.client.get("/v1/me", headers=alice.authorization())

    assert response.status_code == 200
    assert response.json() == {
        "userId": str(alice.oid),
        "displayName": alice.display_name,
        "email": alice.email,
        "roles": ["User"],
        "isAdmin": False,
        "features": {"memoryEnabled": True, "feedbackComments": True},
    }


async def test_an_administrator_with_both_roles_gets_them_sorted(
    started: ApiUnderTest, carol: SharedPrincipal
) -> None:
    response = await started.client.get(
        "/v1/me", headers=carol.authorization(roles=["User", "Admin", "Reader"])
    )

    assert response.json()["roles"] == ["Admin", "User"]
    assert response.json()["isAdmin"] is True


async def test_t014_features_follow_the_ops_config_snapshot(
    started: ApiUnderTest, fake_sql: FakeSql, clock: FakeClock, alice: SharedPrincipal
) -> None:
    fake_sql.rows["memory.enabled"] = encode_value(False)
    fake_sql.rows["feedback.comments_enabled"] = encode_value(False)
    before = (await started.client.get("/v1/me", headers=alice.authorization())).json()

    clock.advance(61)
    after = (await started.client.get("/v1/me", headers=alice.authorization())).json()

    assert before["features"] == {"memoryEnabled": True, "feedbackComments": True}
    assert after["features"] == {"memoryEnabled": False, "feedbackComments": False}


async def test_t013_no_sql_while_the_last_seen_write_is_under_15_minutes_old(
    started: ApiUnderTest, fake_sql: FakeSql, clock: FakeClock, dave: SharedPrincipal
) -> None:
    await started.client.get("/v1/me", headers=dave.authorization())
    assert fake_sql.names().count("ensure_user") == 1
    reads = fake_sql.names().count("read_ops_config")

    for _ in range(5):
        clock.advance(60)
        assert (await started.client.get("/v1/me", headers=dave.authorization())).status_code == 200

    assert fake_sql.names().count("ensure_user") == 1
    # The only SQL: the lazy ops-config refresh, at most once per 60 seconds.
    assert fake_sql.names().count("read_ops_config") - reads <= 5
    assert set(fake_sql.names()) == {"read_ops_config", "ensure_user"}

    clock.advance(15 * 60)
    await started.client.get("/v1/me", headers=dave.authorization())

    assert fake_sql.names().count("ensure_user") == 2


async def test_provisioning_failure_answers_dependency_unavailable(
    started: ApiUnderTest, fake_sql: Any, dave: SharedPrincipal
) -> None:
    fake_sql.failures["ensure_user"] = SqlUnavailable("exhausted", 1205, "")

    response = await started.client.get("/v1/me", headers=dave.authorization())

    assert response.status_code == 503
    assert response.json()["code"] == "dependency_unavailable"


def test_current_user_reads_identity_from_the_principal_only() -> None:
    principal = Principal(
        oid=uuid4(),
        tid=uuid4(),
        azp=uuid4(),
        client="web",
        roles=frozenset({"User"}),
        display_name="Ana",
        email="ana@example.com",
    )

    class _Ops:
        memory = type("M", (), {"enabled": False})()
        feedback = type("F", (), {"comments_enabled": True})()

    view = current_user(principal, _Ops())  # type: ignore[arg-type]

    assert isinstance(view, CurrentUser)
    assert view.user_id == principal.oid
    assert view.features.memory_enabled is False
    assert view.features.feedback_comments is True
