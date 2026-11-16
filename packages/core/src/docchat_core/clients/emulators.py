"""The published constants of the local emulators (`81-config.md` section 2.11; `84-testing.md`
section 2.2).

Local-emulator mode is the only place a client takes a key or a connection string, and only
these: the values Microsoft publishes for Azurite and the Cosmos DB emulator, which every
developer machine shares and which grant nothing in Azure. Start-up refuses the mode inside Azure
(`local_emulators_in_azure`). The model stub, the mode's model and screening endpoint, checks only
that a bearer token is present, so it is sent a fixed one instead of a token the developer's
Azure CLI would have to issue.
"""

from __future__ import annotations

from typing import Final

AZURITE_CONNECTION_STRING: Final = "UseDevelopmentStorage=true"
"""Azurite's `devstoreaccount1`: blobs on 127.0.0.1:10000, queues on 127.0.0.1:10001."""

COSMOS_EMULATOR_KEY: Final = (
    "C2y6yDjf5/R+ob0N8A7Cgv30VRDJIWEHLM+4QDU5DE2nQ9nDuVTqobD4b8mGGyPMbIZnqyMsEcaGQy67XIw/Jw=="
)
"""The Cosmos DB emulator's published account key."""

LOCAL_STUB_BEARER_TOKEN: Final = "local-emulator"  # noqa: S105 - a placeholder, grants nothing
"""The bearer token sent to a loopback model or screening endpoint in local-emulator mode."""
