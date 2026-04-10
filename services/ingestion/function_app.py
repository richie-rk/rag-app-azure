"""Azure Durable Functions entry point for document ingestion.

Uses the v2 programming model (decorators, no function.json files).
Pattern: starter → orchestrator → activity (per file, fan-out).

Fixes over Max AI:
  - Retry policy on activity calls (3 retries with backoff)
  - Per-file fan-out for parallel processing
  - Pydantic request validation
  - No secrets in HTTP body or logs
"""

import json
import logging

import azure.durable_functions as df
import azure.functions as func
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

app = df.DFApp(http_auth_level=func.AuthLevel.FUNCTION)


# ── Request schema ────────────────────────────────────────────────────────────


class IngestionRequest(BaseModel):
    project_name: str = Field(..., min_length=1)
    index_name: str = Field(..., min_length=1)
    container_name: str = Field(..., min_length=1)
    project_id: int
    blob_prefix: str = ""
    chunking_strategy: str = "page_wise"


# ── Starter ───────────────────────────────────────────────────────────────────


@app.route(route="ingest", methods=["POST"])
@app.durable_client_input(client_name="client")
async def ingest_starter(req: func.HttpRequest, client: df.DurableOrchestrationClient):
    """HTTP trigger that starts the ingestion orchestration."""
    try:
        body = req.get_json()
        request = IngestionRequest(**body)
    except Exception as exc:
        return func.HttpResponse(
            json.dumps({"error": f"Invalid request: {exc}"}),
            status_code=400,
            mimetype="application/json",
        )

    instance_id = await client.start_new(
        "ingest_orchestrator",
        client_input=request.model_dump(),
    )

    logger.info("Started orchestration %s for project '%s'", instance_id, request.project_name)
    return client.create_check_status_response(req, instance_id)


# ── Orchestrator ──────────────────────────────────────────────────────────────


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


# ── Activities ────────────────────────────────────────────────────────────────


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
