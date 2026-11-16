"""The five principals every in-process suite shares (`84-testing.md` section 2.5).

| Principal | Token | Default role in the seeded world |
|---|---|---|
| `alice` | `roles: ["User"]` | member of project A |
| `bob` | `roles: ["User"]` | member of project B |
| `carol` | `roles: ["Admin"]` | no memberships |
| `dave` | `roles: ["User"]` | no projects |
| `erin` | a valid token with no `roles` claim | none |

The principals carry identity only: each test creates the memberships, mappings, sessions and
facts it needs with builder calls, so suites can lay out projects differently without changing
them. Display names and e-mail addresses are canaries. Tokens come from `tokens.mint_token`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal
from uuid import UUID

import pytest

from docchat_testing.builders.canaries import canary
from docchat_testing.tokens import SESSION_AUTH, SigningKeys, mint_token, session_signing_keys

type PrincipalName = Literal["alice", "bob", "carol", "dave", "erin"]


@dataclass(frozen=True, slots=True, kw_only=True)
class SharedPrincipal:
    """One shared principal: its name in tests, its Entra object ID and the claims its token
    carries."""

    name: PrincipalName
    oid: UUID
    roles: tuple[str, ...] | None
    display_name: str
    email: str

    @property
    def tenant_id(self) -> UUID:
        return SESSION_AUTH.tenant_id

    @property
    def partition_key(self) -> str:
        """The lower-case hyphenated `oid`, every Cosmos DB partition key value of the caller."""
        return str(self.oid)

    def token(self, **claim_overrides: object) -> str:
        """`mint_token(self, **claim_overrides)`."""
        return mint_token(self, **claim_overrides)

    def authorization(self, **claim_overrides: object) -> dict[str, str]:
        """The `Authorization` header of a request as this principal."""
        return {"Authorization": f"Bearer {self.token(**claim_overrides)}"}


_PRINCIPAL_IDS: Final[dict[PrincipalName, UUID]] = {
    "alice": UUID("0a11ce00-0000-4000-8000-000000000001"),
    "bob": UUID("0b0b0000-0000-4000-8000-000000000002"),
    "carol": UUID("0ca20100-0000-4000-8000-000000000003"),
    "dave": UUID("0da0e000-0000-4000-8000-000000000004"),
    "erin": UUID("0e210000-0000-4000-8000-000000000005"),
}
"""Fixed object IDs, so a failure message names the same principal in every run."""

_PRINCIPAL_ROLES: Final[dict[PrincipalName, tuple[str, ...] | None]] = {
    "alice": ("User",),
    "bob": ("User",),
    "carol": ("Admin",),
    "dave": ("User",),
    "erin": None,
}


def make_principals() -> dict[PrincipalName, SharedPrincipal]:
    """The five principals, with fresh canary display names and e-mail addresses."""
    return {
        name: SharedPrincipal(
            name=name,
            oid=oid,
            roles=_PRINCIPAL_ROLES[name],
            display_name=canary("displayname"),
            email=canary("email"),
        )
        for name, oid in _PRINCIPAL_IDS.items()
    }


@pytest.fixture(scope="session")
def signing_keys() -> SigningKeys:
    """The session key pair, generated here, outside any test's event loop, because generating
    an RSA key holds the thread for tens of milliseconds."""
    return session_signing_keys()


@pytest.fixture(scope="session")
def principals(signing_keys: SigningKeys) -> dict[PrincipalName, SharedPrincipal]:
    """The five shared principals, created once per session."""
    del signing_keys
    return make_principals()


@pytest.fixture(scope="session")
def alice(principals: dict[PrincipalName, SharedPrincipal]) -> SharedPrincipal:
    return principals["alice"]


@pytest.fixture(scope="session")
def bob(principals: dict[PrincipalName, SharedPrincipal]) -> SharedPrincipal:
    return principals["bob"]


@pytest.fixture(scope="session")
def carol(principals: dict[PrincipalName, SharedPrincipal]) -> SharedPrincipal:
    return principals["carol"]


@pytest.fixture(scope="session")
def dave(principals: dict[PrincipalName, SharedPrincipal]) -> SharedPrincipal:
    return principals["dave"]


@pytest.fixture(scope="session")
def erin(principals: dict[PrincipalName, SharedPrincipal]) -> SharedPrincipal:
    return principals["erin"]
