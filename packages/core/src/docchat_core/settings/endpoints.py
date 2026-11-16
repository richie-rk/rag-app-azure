"""Outbound endpoint settings and the secure-endpoint rule (`81-config.md` sections 6.1 check 3,
6.3 and 6.4; `01-repository.md` section 6.4 rule 1; ADR 0150).

Every outbound base URL comes from an app setting validated at start-up: outside local-emulator
mode it must be `https://` and must not name a loopback host, so a misconfigured process can't
send its managed-identity tokens or data in clear text or to a process on its own machine.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Annotated, Final
from urllib.parse import urlsplit

from pydantic import BeforeValidator
from pydantic_core import PydanticCustomError

ENDPOINT_SETTINGS: Final = (
    "DOCCHAT_SQL_SERVER",
    "DOCCHAT_COSMOS_ENDPOINT",
    "DOCCHAT_STORAGE_BLOB_ENDPOINT",
    "DOCCHAT_STORAGE_QUEUE_ENDPOINT",
    "DOCCHAT_SEARCH_ENDPOINT",
    "DOCCHAT_OPENAI_ENDPOINT",
    "DOCCHAT_CONTENTSAFETY_ENDPOINT",
    "DOCCHAT_DOCINTEL_ENDPOINT",
)
"""The settings check 3 inspects (`insecure_endpoint`), whichever class of a process reads them."""

_ENDPOINT_SCHEMES: Final = frozenset({"http", "https"})


def _is_endpoint_url(value: str) -> bool:
    if not value or any(char.isspace() or not char.isprintable() for char in value):
        return False
    try:
        parts = urlsplit(value)
        # Reading the port validates it; a malformed one raises.
        _ = parts.port
    except ValueError:
        return False
    return (
        parts.scheme in _ENDPOINT_SCHEMES
        and bool(parts.hostname)
        and parts.username is None
        and parts.password is None
        and "?" not in value
        and "#" not in value
    )


def _endpoint_url(value: object) -> object:
    if isinstance(value, str) and _is_endpoint_url(value):
        return value
    raise PydanticCustomError(
        "url_parsing",
        "Input should be an absolute http or https URL with a host and no credentials, "
        "query or fragment",
    )


EndpointUrl = Annotated[str, BeforeValidator(_endpoint_url)]
"""A service base URL, kept exactly as configured. `http://` and loopback hosts parse, because
local-emulator mode needs them; the start-up check refuses them everywhere else."""


def _ip_address(name: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(name)
    except ValueError:
        pass
    # A resolver also reads the shorthand IPv4 forms ("127.1", "0x7f000001", "2130706433"), and
    # `inet_aton` parses exactly those.
    try:
        return ipaddress.IPv4Address(socket.inet_aton(name))
    except (OSError, ValueError):
        return None


def is_loopback_host(host: str) -> bool:
    """True for `localhost` (and names under it), 127.0.0.0/8 and `::1` in any spelling."""
    name = host.strip().removeprefix("[").removesuffix("]").rstrip(".").lower()
    if name == "localhost" or name.endswith(".localhost"):
        return True
    address = _ip_address(name)
    if address is None:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    # A connection to the unspecified address reaches the local host too.
    return address.is_loopback or address.is_unspecified


def _bare_host(value: str) -> str:
    # A host-only setting (DOCCHAT_SQL_SERVER) in the forms an ODBC server takes:
    # `[tcp:]host[,port]` or `host:port`, with an IPv6 address in brackets.
    text = value.strip()
    if text.lower().startswith("tcp:"):
        text = text[len("tcp:") :]
    text = text.split(",", 1)[0]
    if text.startswith("["):
        return text[1:].split("]", 1)[0]
    if text.count(":") == 1:
        return text.split(":", 1)[0]
    return text


def is_insecure_endpoint(value: str) -> bool:
    """True when an endpoint value isn't a secure remote address: a URL whose scheme isn't
    `https`, a value that names no host, or a loopback host. A value without `://` is a host."""
    if "://" in value:
        try:
            parts = urlsplit(value)
        except ValueError:
            return True
        if parts.scheme != "https":
            return True
        host = parts.hostname or ""
    else:
        host = _bare_host(value)
    return not host or is_loopback_host(host)
