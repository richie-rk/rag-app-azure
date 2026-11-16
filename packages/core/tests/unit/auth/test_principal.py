from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from docchat_core.auth.principal import (
    ROLES,
    Principal,
    display_name_from,
    email_from,
    roles_from,
)

OID = UUID("0a11ce00-0000-4000-8000-000000000001")


def _principal(**overrides: object) -> Principal:
    values: dict[str, object] = {
        "oid": OID,
        "tid": uuid4(),
        "azp": uuid4(),
        "client": "web",
        "roles": frozenset({"User"}),
    }
    return Principal.model_validate(values | overrides)


def test_a_user_is_no_administrator() -> None:
    principal = _principal()
    assert principal.is_admin is False
    assert principal.last_known_role == "User"
    assert principal.partition_key == "0a11ce00-0000-4000-8000-000000000001"
    assert principal.display_name == principal.email == ""


@pytest.mark.parametrize("roles", [{"Admin"}, {"Admin", "User"}])
def test_admin_in_roles_makes_an_administrator(roles: set[str]) -> None:
    principal = _principal(roles=frozenset(roles))
    assert principal.is_admin is True
    assert principal.last_known_role == "Admin"


def test_the_repr_leaves_out_the_display_data() -> None:
    principal = _principal(display_name="Ada Lovelace", email="ada@example.test")
    assert "Ada" not in repr(principal)
    assert "example.test" not in repr(principal)
    assert str(OID) in repr(principal)


def test_the_principal_is_frozen_and_closed() -> None:
    principal = _principal()
    with pytest.raises(ValidationError):
        principal.oid = uuid4()  # type: ignore[misc]
    with pytest.raises(ValidationError):
        _principal(token="eyJ")


@pytest.mark.parametrize(
    "overrides",
    [
        {"roles": frozenset()},
        {"roles": frozenset({"Reader"})},
        {"client": "other"},
        {"display_name": "x" * 257},
        {"email": "x" * 321},
    ],
)
def test_the_principal_refuses_values_the_validator_never_builds(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _principal(**overrides)


def test_roles_keep_only_the_two_app_roles() -> None:
    assert roles_from(["Reader", "User", "Admin", "admin"]) == frozenset({"User", "Admin"})
    assert roles_from(["Reader"]) == frozenset()
    assert frozenset({"Admin", "User"}) == ROLES


def test_display_name_is_normalised_and_cleaned() -> None:
    # A decomposed e-acute becomes one code point (NFC); the right-to-left override (Cf), a
    # control (Cc), a private-use code point (Co) and an unassigned one (Cn) are removed.
    assert display_name_from("  Rene\u0301 \u202eX\u0007\ue000\U000e0080  ") == "Ren\u00e9 X"


def test_display_name_is_cut_after_trimming() -> None:
    assert display_name_from(" " + "a" * 300) == "a" * 256


def test_display_name_fits_its_column_in_utf16_units() -> None:
    # Each emoji takes two UTF-16 units of the NVARCHAR(256) column.
    emoji = chr(0x1F600)
    assert display_name_from(emoji * 200) == emoji * 128
    assert display_name_from("a" * 255 + emoji) == "a" * 255


@pytest.mark.parametrize("claim", [None, 7, ["a"], ""])
def test_display_name_is_empty_without_a_string(claim: object) -> None:
    assert display_name_from(claim) == ""


def test_t007_email_takes_the_email_claim() -> None:
    assert email_from(" ada@example.test ", "other@example.test") == "ada@example.test"


@pytest.mark.parametrize("email", [None, "", "   ", "x" * 321, chr(0x1F600) * 161])
def test_t007_email_falls_back_to_a_usable_preferred_username(email: object) -> None:
    assert email_from(email, " ada@example.test ") == "ada@example.test"


@pytest.mark.parametrize(
    "preferred_username",
    [
        "ada#ext#@example.test",
        "ada lovelace@example.test",
        "ada@example@test",
        "ada",
        "",
        None,
        "a" * 310 + "@example.test",
    ],
)
def test_t007_email_is_empty_for_an_unusable_preferred_username(
    preferred_username: object,
) -> None:
    assert email_from(None, preferred_username) == ""
