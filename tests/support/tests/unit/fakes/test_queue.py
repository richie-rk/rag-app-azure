from __future__ import annotations

from uuid import UUID

import pytest
from azure.core.exceptions import AzureError

from docchat_api.http.queue import IngestionQueuePort
from docchat_core.ingestion.models import IngestionQueueMessage
from docchat_testing.fakes.queue import RecordingQueue, SentMessage

RUN = UUID("9c4d5e6f-7a8b-4c9d-8e1f-3a4b5c6d7e63")
TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


async def test_each_send_is_recorded_and_decoded() -> None:
    queue = RecordingQueue()
    content = IngestionQueueMessage(run_id=RUN, traceparent=TRACEPARENT).encode()

    await queue.send_message(content, time_to_live=86_400)

    assert queue.sent == [SentMessage(content, 86_400)]
    (message,) = queue.messages()
    assert (message.run_id, message.traceparent) == (RUN, TRACEPARENT)


async def test_a_failing_queue_raises_an_azure_error_and_records_nothing() -> None:
    queue = RecordingQueue(failing=True)

    with pytest.raises(AzureError):
        await queue.send_message("{}", time_to_live=1)

    assert queue.sent == []


def test_it_implements_the_api_port() -> None:
    port: IngestionQueuePort = RecordingQueue()

    assert callable(port.send_message)
