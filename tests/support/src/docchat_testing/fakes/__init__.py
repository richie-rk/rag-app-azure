"""In-memory stand-ins for services without an emulator (`84-testing.md` sections 1.4 and 2.4).

`ReplayTransport` serves recorded HTTP exchanges to Azure SDK clients; `PermissiveBurstLimiter`
and `PermissiveWriteRateLimiter` implement the API's request-limiter ports; `RecordingQueue` is the
`ingestion-requests` queue the `ingestion_queue` dependency hands out in tests; `FakeContainer` is
an in-memory Cosmos DB container for the unit tests of the stores (the emulator serves their
integration tests); `FakeModelClient`, `FakeSearchGateway` and `FakePromptShields` serve the chat
pipeline through `ChatPorts` (`chat`). The layout fake joins this package with its port.
"""

from docchat_testing.fakes.chat import (
    ChatFakes,
    FakeModelClient,
    FakePromptShields,
    FakeSearchGateway,
)
from docchat_testing.fakes.cosmos import FakeContainer
from docchat_testing.fakes.limiters import PermissiveBurstLimiter, PermissiveWriteRateLimiter
from docchat_testing.fakes.queue import RecordingQueue, SentMessage
from docchat_testing.fakes.replay import (
    Exchange,
    ReplayMismatchError,
    ReplayResponse,
    ReplayTransport,
    SentRequest,
    load_exchanges,
)

__all__ = [
    "ChatFakes",
    "Exchange",
    "FakeContainer",
    "FakeModelClient",
    "FakePromptShields",
    "FakeSearchGateway",
    "PermissiveBurstLimiter",
    "PermissiveWriteRateLimiter",
    "RecordingQueue",
    "ReplayMismatchError",
    "ReplayResponse",
    "ReplayTransport",
    "SentMessage",
    "SentRequest",
    "load_exchanges",
]
