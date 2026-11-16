from __future__ import annotations

import jwt

from docchat_testing.builders.canaries import is_canary
from docchat_testing.fixtures.identities import SharedPrincipal, make_principals
from docchat_testing.tokens import SESSION_AUTH, session_signing_keys


def test_the_five_principals_of_section_2_5() -> None:
    principals = make_principals()
    assert list(principals) == ["alice", "bob", "carol", "dave", "erin"]
    assert principals["alice"].roles == ("User",)
    assert principals["bob"].roles == ("User",)
    assert principals["carol"].roles == ("Admin",)
    assert principals["dave"].roles == ("User",)
    assert principals["erin"].roles is None
    assert len({principal.oid for principal in principals.values()}) == 5


def test_object_ids_are_stable_and_content_is_canary() -> None:
    first, second = make_principals(), make_principals()
    assert first["alice"].oid == second["alice"].oid
    assert first["alice"].display_name != second["alice"].display_name
    for principal in first.values():
        assert is_canary(principal.display_name)
        assert is_canary(principal.email)
        assert principal.tenant_id == SESSION_AUTH.tenant_id
        assert principal.partition_key == str(principal.oid)


def test_tokens_and_headers_come_from_mint_token() -> None:
    alice: SharedPrincipal = make_principals()["alice"]
    keys = session_signing_keys()
    header = alice.authorization()
    scheme, token = header["Authorization"].split(" ")
    assert scheme == "Bearer"
    claims = jwt.decode(
        token,
        keys.public_keys()[keys.kid],
        algorithms=["RS256"],
        audience=str(SESSION_AUTH.api_client_id),
        issuer=SESSION_AUTH.issuer,
    )
    assert claims["oid"] == str(alice.oid)
    assert claims["roles"] == ["User"]
    expired = jwt.decode(alice.token(ver="1.0"), options={"verify_signature": False})
    assert expired["ver"] == "1.0"
