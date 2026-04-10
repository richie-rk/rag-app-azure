"""Feedback endpoint — replicates Max AI's save_feedback upsert pattern."""

import json
import logging

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.data.tables import UpdateMode

from services.shared.azure_clients import get_table_service_client
from services.shared.config import get_settings

logger = logging.getLogger(__name__)


def save_feedback(data: dict) -> dict:
    """Save or update feedback for a chat turn.

    Replicates the upsert pattern from Max AI's save_feedback — the
    best-structured save function.
    """
    settings = get_settings()
    table_service = get_table_service_client()
    table_client = table_service.get_table_client(settings.azure_table_name)

    entity = {
        "PartitionKey": data["session_id"],
        "RowKey": data["timestamp"],
        "feedback_type": data.get("feedback_type", ""),
        "feedback_message": data.get("feedback_message", ""),
    }

    try:
        # Try to get the existing entity and merge feedback
        existing = table_client.get_entity(
            partition_key=entity["PartitionKey"],
            row_key=entity["RowKey"],
        )
        existing.update(entity)
        table_client.update_entity(entity=existing, mode=UpdateMode.MERGE)
    except ResourceNotFoundError:
        # Entity doesn't exist yet — create it with full data
        entity.update({
            "session_name": data.get("session_name", ""),
            "username": data.get("username", ""),
            "conversation": json.dumps({
                "user_query": data.get("user_query", ""),
                "bot": data.get("bot_response", ""),
            }),
            "scope": data.get("scope", ""),
            "app": data.get("app", "rag-app-azure"),
        })
        table_client.create_entity(entity=entity)

    return {"status": "saved", "session_id": data["session_id"]}
