"""The API's HTTP platform (`11-api.md` section 1; `01-repository.md` section 1.2): the route class
and its options, the caller, problem details, the request limits, the text types, paging, the
`If-Match` parser, the response headers, the audit helper and the server process.

What a route author uses:

- `DocChatRoute`, `CurrentPrincipal` / `current_principal`, `access`, `route_options`;
- `current_process` (the worker's `ApiProcess`) and `request_ops` (the request's ops-config
  snapshot), each with `Depends`;
- the problems the platform raises: `ValidationFailed`, `DependencyUnavailable`;
- `RequestModel`, `QueryModel` and `ResponseModel`; `SingleLineText` and `MultiLineText`;
- `CursorModel`, `CursorText`, `encode_cursor`, `decode_cursor`; `IfMatchHeader`,
  `if_match_header`;
- `content_disposition` for file responses; `write_audit`, `report_audit` and
  `write_audit_after_commit` for audited writes.
"""

from docchat_api.http.access import AccessLevel, AccessSpec, access
from docchat_api.http.audit import report_audit, write_audit, write_audit_after_commit
from docchat_api.http.headers import FILE_CSP, content_disposition
from docchat_api.http.models import QueryModel, RequestModel, ResponseModel
from docchat_api.http.paging import CursorModel, CursorText, decode_cursor, encode_cursor
from docchat_api.http.preconditions import (
    IfMatchHeader,
    PreconditionFailed,
    PreconditionRequired,
    if_match_header,
)
from docchat_api.http.problems import DependencyUnavailable, FieldError, Problem, ValidationFailed
from docchat_api.http.routing import (
    DEFAULT_ROUTE_OPTIONS,
    CurrentPrincipal,
    DocChatRoute,
    RouteOptions,
    current_principal,
    route_options,
)
from docchat_api.http.state import ApiProcess, current_process, request_ops
from docchat_api.http.text import MultiLineText, SingleLineText

__all__ = [
    "DEFAULT_ROUTE_OPTIONS",
    "FILE_CSP",
    "AccessLevel",
    "AccessSpec",
    "ApiProcess",
    "CurrentPrincipal",
    "CursorModel",
    "CursorText",
    "DependencyUnavailable",
    "DocChatRoute",
    "FieldError",
    "IfMatchHeader",
    "MultiLineText",
    "PreconditionFailed",
    "PreconditionRequired",
    "Problem",
    "QueryModel",
    "RequestModel",
    "ResponseModel",
    "RouteOptions",
    "SingleLineText",
    "ValidationFailed",
    "access",
    "content_disposition",
    "current_principal",
    "current_process",
    "decode_cursor",
    "encode_cursor",
    "if_match_header",
    "report_audit",
    "request_ops",
    "route_options",
    "write_audit",
    "write_audit_after_commit",
]
