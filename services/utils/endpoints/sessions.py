"""Save, retrieve, and delete chat turns in Azure Table Storage."""

import json
import logging

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.data.tables import UpdateMode

from services.shared.azure_clients import get_table_service_client
from services.shared.config import get_settings

logger = logging.getLogger(__name__)


def _get_table_client():
    settings = get_settings()
    service = get_table_service_client()
    return service.get_table_client(settings.azure_table_name)


def save_session(data: dict) -> dict:
    """Save a chat turn to Table Storage, merging into the existing row if present."""
    table_client = _get_table_client()

    entity = {
        "PartitionKey": data["session_id"],
        "RowKey": data["timestamp"],
        "session_name": data.get("session_name", "")[:100],
        "username": data.get("username", ""),
        "conversation": json.dumps({
            "user_query": data.get("user_query", ""),
            "bot": data.get("bot_response", ""),
        }),
        "scope": data.get("scope", ""),
        "app": data.get("app", "rag-app-azure"),
    }

    try:
        table_client.create_entity(entity=entity)
    except ResourceExistsError:
        # Upsert: merge with existing entity
        try:
            existing = table_client.get_entity(
                partition_key=entity["PartitionKey"],
                row_key=entity["RowKey"],
            )
            existing.update(entity)
            table_client.update_entity(entity=existing, mode=UpdateMode.MERGE)
        except ResourceNotFoundError:
            raise ValueError("Entity conflict during upsert")

    return {"status": "saved", "session_id": data["session_id"]}


def get_sessions(username: str, session_id: str | None = None) -> list[dict]:
    """Turns owned by `username`, or that user's turns within one session.

    A session_id query targets a whole session by partition key and is not
    user-scoped, so per-row ownership is enforced in the loop below.
    """
    table_client = _get_table_client()

    if session_id:
        filter_expr = f"PartitionKey eq '{session_id}'"
    else:
        filter_expr = f"username eq '{username}'"

    entities = table_client.query_entities(filter_expr)

    sessions = []
    for entity in entities:
        if entity.get("username") != username:
            # Skip turns the caller doesn't own (reachable via session_id).
            continue
        conversation = json.loads(entity.get("conversation", "{}"))
        sessions.append({
            "session_id": entity.get("PartitionKey", ""),
            "timestamp": entity.get("RowKey", ""),
            "session_name": entity.get("session_name", ""),
            "username": entity.get("username", ""),
            "user_query": conversation.get("user_query", ""),
            "bot_response": conversation.get("bot", ""),
            "scope": entity.get("scope", ""),
            "feedback_type": entity.get("feedback_type"),
            "feedback_message": entity.get("feedback_message"),
        })

    return sessions


def delete_session(session_id: str) -> dict:
    """Delete all entities for a given session."""
    table_client = _get_table_client()

    entities = table_client.query_entities(f"PartitionKey eq '{session_id}'")
    count = 0
    for entity in entities:
        table_client.delete_entity(
            partition_key=entity["PartitionKey"],
            row_key=entity["RowKey"],
        )
        count += 1

    return {"status": "deleted", "session_id": session_id, "count": count}
