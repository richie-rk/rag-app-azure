"""Prompt library CRUD. One blob container per user; prompts keyed {project}/{prompt_name}."""

import hashlib
import logging

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError

from services.shared.azure_clients import get_blob_service_client

logger = logging.getLogger(__name__)


def _get_container_client(username: str):
    """Get or create a per-user blob container.

    The name is a hash of the email, not a character-substituted slug: a slug
    breaks Azure's container-name rules for many valid emails (`+`, `_`,
    consecutive dots) and, worse, collides across distinct emails
    ("a.b@x.com" and "a-b@x.com" would share a container, i.e. share prompt
    libraries). The hash is valid, unique, and never user-influenced.
    """
    blob_service = get_blob_service_client()
    digest = hashlib.sha256(username.lower().encode("utf-8")).hexdigest()
    container_name = f"prompts-{digest[:32]}"
    container_client = blob_service.get_container_client(container_name)

    if not container_client.exists():
        container_client.create_container()

    return container_client


def _blob_path(project: str, prompt_name: str) -> str | None:
    """Join the two caller-supplied segments into a blob key, or None if unsafe.

    Both values come from the request body. A separator or dot-segment in
    either could turn the blob URL into `container/../other-container/...`,
    which the storage front end may resolve out of the caller's container, so
    anything path-like is rejected outright.
    """
    for segment in (project, prompt_name):
        if (
            # JSON bodies can carry non-string values (e.g. "project": 1);
            # without this check the `in` tests below raise TypeError (500)
            # instead of returning the intended 400.
            not isinstance(segment, str)
            or not segment
            or ".." in segment
            or "/" in segment
            or "\\" in segment
            or any(ord(ch) < 32 or ch == "\x7f" for ch in segment)
        ):
            return None
    return f"{project}/{prompt_name}"


def create_prompt(username: str, project: str, prompt_name: str, content: str) -> dict:
    """Create a new prompt. Fails if it already exists."""
    blob_path = _blob_path(project, prompt_name)
    if blob_path is None:
        return {"error": "Invalid project or prompt name", "status": 400}
    container = _get_container_client(username)
    blob_client = container.get_blob_client(blob_path)

    if blob_client.exists():
        return {"error": "Prompt already exists", "status": 409}

    blob_client.upload_blob(content, encoding="UTF-8")
    return {"prompt_name": prompt_name, "status": 201}


def update_prompt(username: str, project: str, prompt_name: str, content: str) -> dict:
    """Update an existing prompt (overwrite)."""
    blob_path = _blob_path(project, prompt_name)
    if blob_path is None:
        return {"error": "Invalid project or prompt name", "status": 400}
    container = _get_container_client(username)
    blob_client = container.get_blob_client(blob_path)

    blob_client.upload_blob(content, encoding="UTF-8", overwrite=True)
    return {"prompt_name": prompt_name, "status": 200}


def get_prompt(username: str, project: str, prompt_name: str) -> dict:
    """Get a single prompt by name."""
    blob_path = _blob_path(project, prompt_name)
    if blob_path is None:
        return {"error": "Invalid project or prompt name", "status": 400}
    container = _get_container_client(username)
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
