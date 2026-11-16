from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from starlette.datastructures import Headers

from docchat_api.http.preconditions import (
    IfMatchHeader,
    PreconditionFailed,
    PreconditionRequired,
    entity_tag,
    if_match_header,
    parse_if_match,
)
from docchat_core.models import encode_row_version
from docchat_testing.fixtures.api import ApiUnderTest
from docchat_testing.fixtures.identities import SharedPrincipal

CURRENT = bytes(range(1, 9))
CURRENT_TAG = f'"{encode_row_version(CURRENT)}"'


@pytest.mark.parametrize(
    "values",
    [
        [],
        ["*"],
        ['W/"AQIDBAUGBwg="'],
        ['"a", "b"'],
        ['"a"', '"b"'],
        ["AQIDBAUGBwg="],
        ['""'],
        ['"x'],
    ],
    ids=["missing", "star", "weak", "list", "repeated", "unquoted", "empty", "half-quoted"],
)
def test_t055_an_unusable_header_is_precondition_required(values: list[str]) -> None:
    with pytest.raises(PreconditionRequired):
        parse_if_match(values).require_row_version(CURRENT)


@pytest.mark.parametrize("tag", ['"not-a-version"', '"AQIDBAUGBwg"', '"AAAAAAAAAAA="'])
def test_t055_a_quoted_value_that_is_not_the_row_version_is_precondition_failed(tag: str) -> None:
    with pytest.raises(PreconditionFailed):
        parse_if_match([tag]).require_row_version(CURRENT)


def test_t055_the_exact_strong_tag_passes() -> None:
    parse_if_match([f"  {CURRENT_TAG} "]).require_row_version(CURRENT)


def test_a_memory_etag_compares_the_whole_value() -> None:
    etag = '"00000000-0000-0000-1a2b-3c4d5e6f0000"'

    IfMatchHeader(etag).require_etag(etag)
    with pytest.raises(PreconditionFailed):
        IfMatchHeader('"other"').require_etag(etag)


def test_a_bare_emulator_etag_is_named_by_its_quoted_form() -> None:
    bare = "3e21ce87-c172-4cfb-a4a3-4f0bdefcedd6"

    assert entity_tag(bare) == f'"{bare}"'
    assert entity_tag(f'"{bare}"') == f'"{bare}"'
    IfMatchHeader(f'"{bare}"').require_etag(bare)
    with pytest.raises(PreconditionRequired):
        IfMatchHeader(bare).require_etag(bare)


def test_the_dependency_joins_every_value() -> None:
    request = SimpleNamespace(headers=Headers(raw=[(b"if-match", b'"a"'), (b"if-match", b'"b"')]))

    assert if_match_header(request).value == '"a","b"'  # type: ignore[arg-type]


async def test_preconditions_answer_after_lookups_in_order(
    started: ApiUnderTest, alice: SharedPrincipal
) -> None:
    async def delete(item: UUID, tag: str | None) -> tuple[int, str | None]:
        headers = alice.authorization() | ({} if tag is None else {"If-Match": tag})
        response = await started.client.delete(f"/v1/probe/versioned/{item}", headers=headers)
        return response.status_code, response.json().get("code")

    assert await delete(UUID(int=0), None) == (404, "project_not_found")
    assert await delete(uuid4(), None) == (428, "precondition_required")
    assert await delete(uuid4(), '"AAAAAAAAAAA="') == (412, "precondition_failed")
    assert await delete(uuid4(), CURRENT_TAG) == (200, None)
