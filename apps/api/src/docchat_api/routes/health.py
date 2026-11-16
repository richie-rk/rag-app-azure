"""`getHealth` — `GET /healthz` (`11-api.md` section 3.1.1; `82-security.md` SEC-79; ADR 0023).

The one route outside `/v1` and the one public operation: no token validation, request limit,
provisioning or ops config, no data read, no dependency called and no log record (its server span
is still exported). It answers `200` with an empty body only once the lifespan start-up has
completed, because Uvicorn serves only after start-up, so a misconfigured instance never reports
healthy. It is liveness only: a dependency failure isn't an instance failure, and taking every
instance out of rotation during a short SQL outage would turn a partial outage into a total one.
"""

from __future__ import annotations

from fastapi import APIRouter
from starlette.responses import Response

from docchat_api.http.access import public_operation

router = APIRouter()


@router.get(
    "/healthz",
    operation_id="getHealth",
    tags=["health"],
    response_class=Response,
    responses={200: {"description": "The instance is serving; the body is empty."}},
    openapi_extra=public_operation(),
)
async def get_health() -> Response:
    return Response(status_code=200)
