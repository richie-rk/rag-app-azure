"""The bases of the API's request, query and response models (`11-api.md` sections 1.7 and 4.1;
`82-security.md` SEC-23).

JSON names are the camelCase aliases of the Python fields. An input model forbids unknown members,
so an unknown body field answers `422` with `unknown_field` at `body.<field>` and an unknown query
parameter the same at `query.<name>`; it accepts only the API's names, never the Python ones. A
response model always serialises every field, `null` where empty, and is built in code by field
name.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class RequestModel(BaseModel):
    """A request body: camelCase members only, unknown members refused."""

    # Inputs are matched by their camelCase aliases only; the plugin can't type an `__init__`
    # keyed by generated aliases, and input models are built only by validation.
    model_config = ConfigDict(  # type: ignore[pydantic-alias]
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        validate_by_alias=True,
        validate_by_name=False,
    )


class QueryModel(BaseModel):
    """One operation's query parameters: camelCase names only, unknown parameters refused."""

    # Inputs are matched by their camelCase aliases only; the plugin can't type an `__init__`
    # keyed by generated aliases, and input models are built only by validation.
    model_config = ConfigDict(  # type: ignore[pydantic-alias]
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        validate_by_alias=True,
        validate_by_name=False,
    )


class ResponseModel(BaseModel):
    """A response body: camelCase members, every field present."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        validate_by_alias=True,
        validate_by_name=True,
        serialize_by_alias=True,
    )
