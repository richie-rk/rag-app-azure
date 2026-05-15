"""Prompt library CRUD. One blob container per user; prompts keyed {project}/{prompt_name}."""

import logging

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError

from services.shared.azure_clients import get_blob_service_client

logger = logging.getLogger(__name__)


def _get_container_client(username: str):
    """Get or create a per-user blob container."""
    blob_service = get_blob_service_client()
    container_name = username.lower().replace("@", "-").replace(".", "-")
    container_client = blob_service.get_container_client(container_name)

    if not container_client.exists():
        container_client.create_container()

    return container_client


def create_prompt(username: str, project: str, prompt_name: str, content: str) -> dict:
    """Create a new prompt. Fails if it already exists."""
    container = _get_container_client(username)
    blob_path = f"{project}/{prompt_name}"
    blob_client = container.get_blob_client(blob_path)

    if blob_client.exists():
        return {"error": "Prompt already exists", "status": 409}

    blob_client.upload_blob(content, encoding="UTF-8")
    return {"prompt_name": prompt_name, "status": 201}


def update_prompt(username: str, project: str, prompt_name: str, content: str) -> dict:
    """Update an existing prompt (overwrite)."""
    container = _get_container_client(username)
    blob_path = f"{project}/{prompt_name}"
    blob_client = container.get_blob_client(blob_path)

    blob_client.upload_blob(content, encoding="UTF-8", overwrite=True)
    return {"prompt_name": prompt_name, "status": 200}


def get_prompt(username: str, project: str, prompt_name: str) -> dict:
    """Get a single prompt by name."""
    container = _get_container_client(username)
    blob_path = f"{project}/{prompt_name}"
    blob_client = container.get_blob_client(blob_path)

    try:
        content = blob_client.download_blob(encoding="UTF-8").readall()
        return {"prompt_name": prompt_name, "prompt_content": content}
    except ResourceNotFoundError:
        return {"error": "Prompt not found", "status": 404}


def get_all_prompts(username: str) -> list[dict]:
    """Get all prompts for a user across all projects."""
    container = _get_container_client(username)
    prompts = []

    for blob in container.list_blobs():
        blob_client = container.get_blob_client(blob.name)
        content = blob_client.download_blob(encoding="UTF-8").readall()
        prompts.append({
            "project_path": blob.name,
            "prompt_content": content,
        })

    return prompts
