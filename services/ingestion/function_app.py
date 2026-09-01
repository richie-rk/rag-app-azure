"""Durable Functions entry point for document ingestion.

v2 programming model (decorators, no function.json files). An HTTP starter
kicks off the orchestrator, which fans out one activity per file.
"""

import json
import logging

import azure.durable_functions as df
import azure.functions as func
from pydantic import BaseModel

from services.shared.auth import extract_bearer_token, validate_jwt
from services.shared.authz import user_can_access_project_id
from services.shared.config import get_settings
from services.shared.database import get_session_factory
from services.shared.models import Project

logger = logging.getLogger(__name__)

# ANONYMOUS at the platform level because the starter validates the caller's
# JWT itself (same pattern as the utils Function App). Previously this was
# AuthLevel.FUNCTION with no JWT check, which the browser could never call
# (no function key) and which trusted index/container names from the body.
app = df.DFApp(http_auth_level=func.AuthLevel.ANONYMOUS)


# Request schema
#
# The client identifies the project and nothing else. index_name,
# container_name, blob_prefix, and chunking_strategy are all derived
# server-side from the project row so a caller can never point ingestion
# at another project's index or an arbitrary container (extra body fields
# are ignored by pydantic's default config).


class IngestionRequest(BaseModel):
    project_id: int


def _json_error(message: str, status_code: int) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps({"error": message}),
        status_code=status_code,
        mimetype="application/json",
    )


# Starter


@app.route(route="ingest", methods=["POST"])
@app.durable_client_input(client_name="client")
async def ingest_starter(req: func.HttpRequest, client: df.DurableOrchestrationClient):
    """HTTP trigger that starts the ingestion orchestration."""
    # Authenticate: valid JWT, a platform role, and not a guest.
    token = extract_bearer_token(req.headers.get("Authorization"))
    if not token:
        return _json_error("Missing authorization", 401)
    try:
        claims = validate_jwt(token)
    except Exception:
        return _json_error("Invalid token", 401)
    if not claims.get("role"):
        return _json_error("Not authorized", 403)
    if claims.get("role") == "guest":
        return _json_error("Read-only access for guest users", 403)

    try:
        body = req.get_json()
        request = IngestionRequest(**body)
    except Exception as exc:
        return _json_error(f"Invalid request: {exc}", 400)

    # Authorize + resolve the project server-side.
    factory = get_session_factory()
    with factory() as session:
        project = (
            session.query(Project)
            .filter(Project.id == request.project_id, Project.is_active.is_(True))
            .first()
        )
        if project is None:
            return _json_error("Project not found", 404)
        if claims.get("role") != "admin" and not user_can_access_project_id(
            session, claims["sub"], project.id
        ):
            return _json_error("No access to this project", 403)

        settings = get_settings()
        orchestration_input = {
            "project_name": project.name,
            "index_name": project.index_name,
            "container_name": settings.default_blob_container,
            "project_id": project.id,
            "blob_prefix": f"{project.id}/",
            "chunking_strategy": project.chunking_strategy or "page_wise",
        }

    instance_id = await client.start_new(
        "ingest_orchestrator",
        client_input=orchestration_input,
    )

    logger.info(
        "Started orchestration %s for project '%s'",
        instance_id,
        orchestration_input["project_name"],
    )
    # NOT create_check_status_response: those management URLs embed the task
    # hub's system key, which also authorizes terminate/raise-event calls
    # against any orchestration instance. The UI only polls the audit table,
    # so the instance id alone is returned.
    return func.HttpResponse(
        json.dumps({"instance_id": instance_id, "status": "started"}),
        status_code=202,
        mimetype="application/json",
    )


# Orchestrator


@app.orchestration_trigger(context_name="context")
def ingest_orchestrator(context: df.DurableOrchestrationContext):
    """Orchestrate document ingestion with per-file fan-out and retry."""
    request = context.get_input()

    # First: list blobs to process
    blobs = yield context.call_activity("list_blobs_activity", request)

    if not blobs:
        return {"status": "completed", "message": "No files to process", "results": []}

    # Fan-out: process each file in parallel with retry
    retry_options = df.RetryOptions(
        first_retry_interval_in_milliseconds=5000,
        max_number_of_attempts=3,
    )

    tasks = []
    for blob_name in blobs:
        task_input = {**request, "blob_name": blob_name}
        tasks.append(
            context.call_activity_with_retry(
                "process_file_activity",
                retry_options,
                task_input,
            )
        )

    # Fan-in: wait for all files
    results = yield context.task_all(tasks)

    return {"status": "completed", "results": results}


# Activities


@app.activity_trigger(input_name="input")
def list_blobs_activity(input: dict) -> list[str]:
    """List all blobs in the container with the given prefix."""
    from services.shared.azure_clients import get_blob_service_client

    blob_service = get_blob_service_client()
    container_client = blob_service.get_container_client(input["container_name"])

    prefix = input.get("blob_prefix", "")
    blobs = []
    for blob in container_client.list_blobs(name_starts_with=prefix or None):
        blobs.append(blob.name)

    logger.info("Found %d blobs in container '%s'", len(blobs), input["container_name"])
    return blobs


@app.activity_trigger(input_name="input")
def process_file_activity(input: dict) -> dict:
    """Process a single file through the ingestion pipeline."""
    from .orchestrator import process_file

    return process_file(
        project_id=input["project_id"],
        index_name=input["index_name"],
        container_name=input["container_name"],
        blob_name=input["blob_name"],
        chunking_strategy=input.get("chunking_strategy", "page_wise"),
    )
