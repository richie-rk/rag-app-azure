"""An in-memory `ingestion-requests` queue for the API's queue port (`11-api.md` section 3.10;
`14-ingestion.md` section 3.3).

A test hands one out through the `ingestion_queue` dependency (`app.dependency_overrides`), or
an Azurite client when the test reads the message back from the emulator: it records each
message the API sends, decoded, so a test can see which runs were announced, and it can be told
to fail, so a test can prove that a failed send still answers the operation's success status.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from azure.core.exceptions import ServiceRequestError

from docchat_core.ingestion.models import IngestionQueueMessage


@dataclass(frozen=True, slots=True)
class SentMessage:
    """One message as the API sent it: its JSON text and its time to live."""

    content: str
    time_to_live: int

    def decoded(self) -> IngestionQueueMessage:
        return IngestionQueueMessage.validate_wire(self.content)


@dataclass(slots=True)
class RecordingQueue:
    """Implements the API's ingestion queue port: each send is recorded, or fails as a
    connection failure after the client's retries would have when `failing` is set."""

    failing: bool = False
    sent: list[SentMessage] = field(default_factory=list)

    async def send_message(self, content: str, *, time_to_live: int) -> object:
        if self.failing:
            raise ServiceRequestError("queue unreachable")
        self.sent.append(SentMessage(content, time_to_live))
        return None

    def messages(self) -> list[IngestionQueueMessage]:
        return [message.decoded() for message in self.sent]
