"""The queue `ingestion-requests` in Azurite: T-102, the queue round-trip integration test, and
T-103, the queue starter integration tests (`80-data-model.md` sections 5.2 and 5.3 and acceptance
criteria 32 and 33; `14-ingestion.md` section 4.7 and acceptance criterion 7).

The API sends through `TextBase64EncodePolicy`; the trigger, with `messageEncoding: base64` in
`host.json`, receives the decoded bytes. The Functions host isn't one of the `compose.yaml`
services, so T-103 drives the queue starter with a stand-in for the host's queue listener that
follows `host.json`'s `queues` settings against the real Azurite queue: a message is received,
base64-decoded and handed to the starter with its dequeue count; a function that returns deletes
it, one that raises leaves it for another receive, and the attempt that reaches
`maxDequeueCount` moves it to `ingestion-requests-poison`.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

import pytest
from azure.storage.queue.aio import QueueClient

from docchat_core.clients.emulators import AZURITE_CONNECTION_STRING
from docchat_core.ingestion.models import IngestionQueueMessage
from docchat_ingestion import starters
from docchat_ingestion.boundary import DispatcherUnavailableError
from docchat_testing.fixtures.storage import POISON_QUEUE, StorageClients
from docchat_testing.telemetry_capture import TelemetryCapture

pytestmark = pytest.mark.integration

HOST_JSON: Final = Path(__file__).resolve().parents[2] / "host.json"
TRACEPARENT: Final = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
MESSAGE_MAX_BYTES: Final = 1024


def queue_settings() -> dict[str, Any]:
    settings: dict[str, Any] = json.loads(HOST_JSON.read_text(encoding="utf-8"))["extensions"][
        "queues"
    ]
    return settings


@pytest.fixture
async def raw_queue(storage: StorageClients) -> AsyncIterator[QueueClient]:
    """The session's `ingestion-requests` without an encode or decode policy: the bytes as the
    queue holds them, which the host decodes itself."""
    client = QueueClient.from_connection_string(
        AZURITE_CONNECTION_STRING, storage.account.ingestion_queue
    )
    try:
        yield client
    finally:
        await client.close()


async def _drain(queue: QueueClient) -> None:
    async for message in queue.receive_messages(max_messages=32, visibility_timeout=1):
        await queue.delete_message(message)


# T-102


async def test_t102_a_message_survives_the_base64_round_trip_unchanged(
    storage: StorageClients, raw_queue: QueueClient
) -> None:
    await _drain(raw_queue)
    sent = IngestionQueueMessage(
        run_id=uuid4(), traceparent=TRACEPARENT, tracestate="rojo=00f067aa0ba902b7,congo=t61"
    )

    await storage.queue().send_message(sent.encode())
    received = [
        message
        async for message in raw_queue.receive_messages(max_messages=1, visibility_timeout=5)
    ]

    (message,) = received
    stored = str(message.content)
    assert len(stored.encode()) <= MESSAGE_MAX_BYTES
    assert queue_settings()["messageEncoding"] == "base64"
    body = base64.b64decode(stored, validate=True)
    assert starters.decode_message(body) == sent
    assert body.decode() == sent.encode()
    await raw_queue.delete_message(message)


async def test_t102_an_empty_tracestate_is_left_out_and_still_round_trips(
    storage: StorageClients, raw_queue: QueueClient
) -> None:
    await _drain(raw_queue)
    sent = IngestionQueueMessage(run_id=uuid4(), traceparent=TRACEPARENT, tracestate="")

    await storage.queue().send_message(sent.encode())
    (message,) = [
        message
        async for message in raw_queue.receive_messages(max_messages=1, visibility_timeout=5)
    ]

    body = base64.b64decode(str(message.content), validate=True)
    assert "tracestate" not in json.loads(body)
    assert starters.decode_message(body) == sent
    await raw_queue.delete_message(message)


# T-103: the host's queue listener, as host.json configures it.


class _UnreachableHub:
    """A Durable client whose task hub doesn't answer."""

    async def get_status(self, instance_id: str, *options: bool) -> Any:
        raise OSError("task hub unreachable")

    async def start_new(
        self,
        orchestration_function_name: str,
        instance_id: str | None = None,
        client_input: Any = None,
    ) -> str:
        raise OSError("task hub unreachable")

    async def raise_event(self, instance_id: str, event_name: str, event_data: Any = None) -> None:
        raise OSError("task hub unreachable")

    async def terminate(self, instance_id: str, reason: str) -> None:
        raise OSError("task hub unreachable")


async def host_listener_pass(
    queue: QueueClient, poison: QueueClient, client: starters.DurableClient
) -> int:
    """One receive of the host's listener; the number of messages it handed to the function."""
    settings = queue_settings()
    handled = 0
    async for message in queue.receive_messages(
        max_messages=int(settings["batchSize"]), visibility_timeout=5
    ):
        handled += 1
        body = base64.b64decode(str(message.content))
        try:
            await starters.start_from_message(body, message.id, message.dequeue_count, client)
        except DispatcherUnavailableError:
            if (message.dequeue_count or 0) >= int(settings["maxDequeueCount"]):
                await poison.send_message(str(message.content))
                await queue.delete_message(message)
            else:
                # The host would wait `visibilityTimeout`; the test lets it reappear at once.
                await queue.update_message(message, visibility_timeout=0)
        else:
            await queue.delete_message(message)
    return handled


@pytest.fixture
async def poison_raw(storage: StorageClients) -> AsyncIterator[QueueClient]:
    client = QueueClient.from_connection_string(
        AZURITE_CONNECTION_STRING, storage.account.queues[POISON_QUEUE]
    )
    try:
        await _drain(client)
        yield client
    finally:
        await client.close()


async def test_t103_a_message_that_fails_validation_is_deleted_and_logged(
    raw_queue: QueueClient, poison_raw: QueueClient, telemetry_capture: TelemetryCapture
) -> None:
    await _drain(raw_queue)
    sent = await raw_queue.send_message(base64.b64encode(b'{"runId": "not a uuid"}').decode())

    handled = await host_listener_pass(raw_queue, poison_raw, _UnreachableHub())

    assert handled == 1
    assert await raw_queue.peek_messages(max_messages=32) == []
    assert await poison_raw.peek_messages(max_messages=32) == []
    invalid = [
        dict(record.log_record.attributes or {})
        for record in telemetry_capture.log_records()
        if record.log_record.body == "ingestion_message_invalid"
    ]
    assert any(attributes.get("messageId") == sent.id for attributes in invalid)


async def test_t103_a_message_whose_handling_fails_five_times_lands_in_the_poison_queue(
    storage: StorageClients, raw_queue: QueueClient, poison_raw: QueueClient
) -> None:
    await _drain(raw_queue)
    sent = IngestionQueueMessage(run_id=uuid4(), traceparent=TRACEPARENT)
    await storage.queue().send_message(sent.encode())
    attempts = int(queue_settings()["maxDequeueCount"])
    assert attempts == 5

    handled = [await host_listener_pass(raw_queue, poison_raw, _UnreachableHub())]
    while sum(handled) < attempts and handled[-1]:
        handled.append(await host_listener_pass(raw_queue, poison_raw, _UnreachableHub()))

    assert sum(handled) == attempts
    assert await raw_queue.peek_messages(max_messages=32) == []
    (poisoned,) = await poison_raw.peek_messages(max_messages=32)
    assert starters.decode_message(base64.b64decode(str(poisoned.content))) == sent
