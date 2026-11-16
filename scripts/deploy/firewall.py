"""Transient runner rules on SQL, the shared registry and the test-credential vault.

`83-infrastructure.md` section 4.6, ADR 0119 and ADR 0122 item 5. Usage:

    uv run --frozen python scripts/deploy/firewall.py sweep|open|close \
        --target sql|registry|vault --env <env>

Jobs run `sweep` only for SQL. `sweep --target registry|vault` belongs to runbook R26, run by the
bootstrap operator with `DOCCHAT_SHARED_OPERATOR_IP_ALLOWLIST` exported: no job holds that list.

Python standard library and the Azure CLI only. Every call that leaves the process goes through
one of the small functions below (`run_command`, `fetch_text`, `http_status`, `open_tds_stream`,
`sleep`, `monotonic`, `now`, `jitter`), which the unit tests replace with recorded answers.
"""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

ENVIRONMENTS = ("dev", "prod", "load")
VAULT_ENVIRONMENTS = ("dev", "load")
TARGETS = ("sql", "registry", "vault")
COMMANDS = ("sweep", "open", "close")
SHARED_RESOURCE_GROUP = "rg-docchat-shared"

OPERATOR_ALLOWLIST = "DOCCHAT_OPERATOR_IP_ALLOWLIST"
SHARED_ALLOWLIST = "DOCCHAT_SHARED_OPERATOR_IP_ALLOWLIST"
TEST_VAULT_URI = "DOCCHAT_TEST_VAULT_URI"

ECHO_SERVICES = ("https://api.ipify.org?format=text", "https://ifconfig.me/ip")
ECHO_TIMEOUT_SECONDS = 5.0
ECHO_MAX_BYTES = 64
NON_PUBLIC_NETWORKS = tuple(
    ipaddress.IPv4Network(network)
    for network in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
    )
)

PROBE_INTERVAL_SECONDS = 15.0
PROBE_LIMIT_SECONDS = 300.0
HTTP_TIMEOUT_SECONDS = 10.0
COMMAND_TIMEOUT_SECONDS = 300.0

VAULT_WRITE_ATTEMPTS = 5
VAULT_WRITE_WAIT_SECONDS = (2.0, 5.0)
VAULT_CLOSE_ATTEMPTS = 5
VAULT_FIREWALL_REFUSAL = "ForbiddenByFirewall"

SQL_RULE_PREFIX = "ci-runner-"
SQL_FIREWALL_REFUSAL = 40615
SQL_DATABASE = "docchat"
SQL_SCOPE = "https://database.windows.net/.default"
REGISTRY_REACHABLE = 401

MESSAGES = {
    "runner_ip_unresolved": "The runner's public address couldn't be confirmed; no rule was added.",
    "runner_ip_mismatch": "The runner's public address couldn't be confirmed; no rule was added.",
    "rule_not_effective": "The firewall rule didn't take effect in time and was removed.",
    "rule_close_failed": "The temporary rule couldn't be removed; a later sweep removes it.",
    "not_in_allowlist": "This machine's address isn't in the operator allowlist.",
}
EXIT_CODES = {
    "runner_ip_unresolved": 2,
    "runner_ip_mismatch": 2,
    "rule_not_effective": 3,
    "rule_close_failed": 4,
    "not_in_allowlist": 2,
}
# An Azure CLI failure has no code of its own in section 10.18: in `open` the rule can't be
# confirmed (and is removed), in `close` and `sweep` a rule may stay for a later sweep (on SQL
# the next job's, on the registry and the vault runbook R26's).
FAILURE_BY_COMMAND = {
    "open": "rule_not_effective",
    "close": "rule_close_failed",
    "sweep": "rule_close_failed",
}


class FirewallError(Exception):
    """A failure with its code of `83-infrastructure.md` section 10.18."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class AzError(Exception):
    """An Azure CLI call that exited non-zero or answered something unusable."""

    def __init__(self, call: str, detail: str) -> None:
        super().__init__(f"{call}: {detail}")
        self.call = call
        self.detail = detail


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


# --- seams to the outside world -------------------------------------------------------------


def run_command(args: Sequence[str]) -> CommandResult:
    """Run one command without a shell and capture its output."""
    executable = shutil.which(args[0])
    if executable is None:
        return CommandResult(127, "", f"{args[0]} is not on PATH")
    try:
        # Fixed command words plus values this script validated; no shell is involved.
        completed = subprocess.run(  # noqa: S603
            [executable, *args[1:]],
            capture_output=True,
            text=True,
            check=False,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"{args[0]} timed out")
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def fetch_text(url: str, timeout: float) -> str:
    """GET a short plain-text answer from one of the two fixed address echo services."""
    # The URL is one of the ECHO_SERVICES constants, always https.
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
        body: bytes = response.read(ECHO_MAX_BYTES)
    return body.decode("ascii")


def http_status(url: str, timeout: float) -> int:
    """The status of an anonymous GET; an HTTP error status is an answer, not a failure."""
    try:
        # The URL is built from the registry name the Azure CLI listed, always https.
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)


def sleep(seconds: float) -> None:
    time.sleep(seconds)


def monotonic() -> float:
    return time.monotonic()


def now() -> datetime:
    return datetime.now(UTC)


def jitter(low: float, high: float) -> float:
    return secrets.SystemRandom().uniform(low, high)


# --- reporting ------------------------------------------------------------------------------


def report_failure(code: str, detail: str) -> None:
    line = json.dumps({"code": code, "message": MESSAGES[code], "detail": detail})
    sys.stderr.write(line + "\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        # The exit code carries the failure; a summary that can't be written mustn't mask it.
        with contextlib.suppress(OSError), Path(summary).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def report_progress(**fields: str | int) -> None:
    sys.stdout.write(json.dumps(fields) + "\n")


# --- Azure CLI helpers ----------------------------------------------------------------------


def az(*args: str) -> str:
    result = run_command(["az", *args])
    call = "az " + " ".join(word for word in args[:4] if not word.startswith("-"))
    if result.returncode != 0:
        last_line = (result.stderr.strip().splitlines() or ["no error output"])[-1]
        raise AzError(call, last_line[:300])
    return result.stdout


def az_json(*args: str) -> Any:
    output = az(*args, "--output", "json")
    if not output.strip():
        return None
    try:
        return json.loads(output)
    except json.JSONDecodeError as error:
        raise AzError("az " + " ".join(args[:3]), "answered no JSON") from error


def access_token(scope: str) -> str:
    token = az("account", "get-access-token", "--scope", scope, "--query", "accessToken")
    token = token.strip().strip('"')
    if not token:
        raise AzError("az account get-access-token", "answered no token")
    return token


# --- addresses ------------------------------------------------------------------------------


def bare_address(value: str) -> str:
    """Rules are compared without a trailing /32 (section 4.6)."""
    return value.strip().removesuffix("/32")


def parse_allowlist(variable: str) -> frozenset[str]:
    raw = os.environ.get(variable, "")
    return frozenset(bare_address(entry) for entry in raw.split(",") if entry.strip())


def known_shared_allowlist() -> frozenset[str] | None:
    """The shared allowlist, or None when this process wasn't given it.

    `DOCCHAT_SHARED_OPERATOR_IP_ALLOWLIST` is set only in the bootstrap operator's shell, never
    as a GitHub variable (83-infrastructure.md section 10.4). An unset list read as empty would
    remove every operator's standing address, so it is unknown, not empty.
    """
    if SHARED_ALLOWLIST not in os.environ:
        return None
    return parse_allowlist(SHARED_ALLOWLIST)


def warn_allowlist_unknown(target: str) -> None:
    sys.stdout.write(
        f"::warning::{SHARED_ALLOWLIST} is not set, so sweep --target {target} removed nothing; "
        'run it from runbook R26 "Remove a stray runner rule" with the shared allowlist '
        "exported.\n"
    )


def is_public_ipv4(address: ipaddress.IPv4Address) -> bool:
    if address.is_unspecified or address.is_multicast or address.is_reserved:
        return False
    return not any(address in network for network in NON_PUBLIC_NETWORKS)


def resolve_runner_address() -> str:
    """Two independent echo services must agree on one public IPv4 address."""
    answers: list[ipaddress.IPv4Address] = []
    for url in ECHO_SERVICES:
        try:
            text = fetch_text(url, ECHO_TIMEOUT_SECONDS).strip()
        except (OSError, ValueError) as error:
            raise FirewallError(
                "runner_ip_unresolved", f"{url} failed: {type(error).__name__}"
            ) from error
        try:
            answers.append(ipaddress.IPv4Address(text))
        except ValueError as error:
            raise FirewallError(
                "runner_ip_unresolved", f"{url} answered no IPv4 address: {text[:45]!r}"
            ) from error
    first, second = answers
    if first != second:
        raise FirewallError(
            "runner_ip_mismatch",
            f"{ECHO_SERVICES[0]} answered {first}, {ECHO_SERVICES[1]} answered {second}",
        )
    if not is_public_ipv4(first):
        raise FirewallError("runner_ip_unresolved", f"{first} is not a public IPv4 address")
    return str(first)


def in_github_actions() -> bool:
    return bool(os.environ.get("GITHUB_ACTIONS"))


def wait_until_effective(probe: Callable[[], bool]) -> bool:
    """Probe every 15 s for at most 300 s (a SQL firewall change can take five minutes)."""
    started = monotonic()
    while True:
        if probe():
            return True
        if monotonic() - started + PROBE_INTERVAL_SECONDS > PROBE_LIMIT_SECONDS:
            return False
        sleep(PROBE_INTERVAL_SECONDS)


def ip_rule_values(listing: Any) -> list[str]:
    """The IP rules of `az acr|keyvault network-rule list`, as the service stores them."""
    rules = listing.get("ipRules") if isinstance(listing, dict) else None
    values: list[str] = []
    for rule in rules if isinstance(rules, list) else []:
        if isinstance(rule, dict):
            value = rule.get("value") or rule.get("ipAddressOrRange")
            if isinstance(value, str) and value:
                values.append(value)
    return values


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def state_path(target: str) -> Path:
    directory = os.environ.get("RUNNER_TEMP", "")
    if not directory:
        raise AzError("RUNNER_TEMP", "is not set, so the rule's state can't be recorded")
    return Path(directory) / f"docchat-firewall-{target}.json"


def read_state(target: str) -> dict[str, Any] | None:
    directory = os.environ.get("RUNNER_TEMP", "")
    if not directory:
        return None
    path = Path(directory) / f"docchat-firewall-{target}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as error:
        raise AzError(str(path), "can't be read") from error
    return data if isinstance(data, dict) else None


def write_state(target: str, data: dict[str, Any]) -> None:
    state_path(target).write_text(json.dumps(data), encoding="utf-8")


# --- TDS 8.0 login probe --------------------------------------------------------------------
# A login with Encrypt=strict: TLS first (ALPN tds/8.0), then PRELOGIN and LOGIN7 with the
# FEDAUTH security-token feature (MS-TDS). Only the outcome matters: the server's first error
# number (40615: the firewall refuses this address; 40613: the database isn't available yet)
# or an accepted login.

TDS_PORT = 1433
TDS_ALPN = "tds/8.0"
TDS_CONNECT_TIMEOUT_SECONDS = 10.0
TDS_PACKET_SIZE = 4096
TDS_HEADER = struct.Struct(">BBHHBB")
TDS_PRELOGIN = 0x12
TDS_LOGIN7 = 0x10
TDS_STATUS_EOM = 0x01
TDS_8_VERSION = 0x08000000
PRELOGIN_VERSION = 0x00
PRELOGIN_ENCRYPTION = 0x01
PRELOGIN_FEDAUTHREQUIRED = 0x06
PRELOGIN_NONCE = 0x07
PRELOGIN_TERMINATOR = 0xFF
PRELOGIN_OPTION = struct.Struct(">BHH")
# Under TDS 8.0 TLS is already up, so the PRELOGIN encryption option says OFF.
ENCRYPT_OFF = 0x00
LOGIN7_FIXED_LENGTH = 94
LOGIN7_OPTION_FLAGS1 = 0xE0  # USE_DB_ON, INIT_DB_FATAL, SET_LANG_ON
LOGIN7_OPTION_FLAGS2 = 0x03  # INIT_LANG_FATAL, ODBC_ON
LOGIN7_OPTION_FLAGS3 = 0x10  # fExtension: a FeatureExt block follows
LOGIN7_LCID = 0x0409
FEATURE_FEDAUTH = 0x02
FEATURE_TERMINATOR = 0xFF
FEDAUTH_SECURITY_TOKEN = 0x01
TOKEN_ERROR = 0xAA
TOKEN_INFO = 0xAB
TOKEN_LOGINACK = 0xAD
TOKEN_ENVCHANGE = 0xE3
TOKEN_FEATUREEXTACK = 0xAE
TOKEN_FEDAUTHINFO = 0xEE
TOKENS_WITH_SHORT_LENGTH = (TOKEN_ERROR, TOKEN_INFO, TOKEN_LOGINACK, TOKEN_ENVCHANGE)
DONE_TOKENS = (0xFD, 0xFE, 0xFF)
DONE_LENGTH = 12
ENVCHANGE_ROUTING = 20
ROUTE = struct.Struct("<BHBHH")
MAX_ROUTES = 1


class TdsProtocolError(OSError):
    """The server's answer didn't follow the TDS framing."""


class TdsStream(Protocol):
    def sendall(self, data: bytes, /) -> None: ...

    def recv(self, size: int, /) -> bytes: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class LoginReply:
    error_number: int | None
    accepted: bool
    route: tuple[str, int] | None


def open_tds_stream(host: str, port: int) -> TdsStream:
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.set_alpn_protocols([TDS_ALPN])
    raw = socket.create_connection((host, port), timeout=TDS_CONNECT_TIMEOUT_SECONDS)
    try:
        return context.wrap_socket(raw, server_hostname=host)
    except OSError:
        raw.close()
        raise


def tds_packets(packet_type: int, payload: bytes) -> bytes:
    chunk = TDS_PACKET_SIZE - TDS_HEADER.size
    pieces = [payload[start : start + chunk] for start in range(0, len(payload), chunk)] or [b""]
    out = bytearray()
    for index, piece in enumerate(pieces):
        status = TDS_STATUS_EOM if index == len(pieces) - 1 else 0
        out += TDS_HEADER.pack(
            packet_type, status, len(piece) + TDS_HEADER.size, 0, (index + 1) % 256, 0
        )
        out += piece
    return bytes(out)


def _read_exact(stream: TdsStream, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = stream.recv(size - len(data))
        if not chunk:
            raise TdsProtocolError("the server closed the connection mid-message")
        data += chunk
    return bytes(data)


def read_tds_message(stream: TdsStream) -> bytes:
    payload = bytearray()
    while True:
        header = TDS_HEADER.unpack(_read_exact(stream, TDS_HEADER.size))
        status, length = header[1], header[2]
        if length < TDS_HEADER.size:
            raise TdsProtocolError("a TDS packet is shorter than its header")
        payload += _read_exact(stream, length - TDS_HEADER.size)
        if status & TDS_STATUS_EOM:
            return bytes(payload)


def build_prelogin() -> bytes:
    options = (
        (PRELOGIN_VERSION, bytes(6)),
        (PRELOGIN_ENCRYPTION, bytes([ENCRYPT_OFF])),
        (PRELOGIN_FEDAUTHREQUIRED, b"\x01"),
    )
    data_start = len(options) * PRELOGIN_OPTION.size + 1
    header = bytearray()
    data = bytearray()
    for token, value in options:
        header += PRELOGIN_OPTION.pack(token, data_start + len(data), len(value))
        data += value
    return bytes(header) + bytes([PRELOGIN_TERMINATOR]) + bytes(data)


def parse_prelogin(payload: bytes) -> dict[int, bytes]:
    options: dict[int, bytes] = {}
    position = 0
    try:
        while payload[position] != PRELOGIN_TERMINATOR:
            token, offset, length = PRELOGIN_OPTION.unpack_from(payload, position)
            if offset + length > len(payload):
                raise TdsProtocolError("a PRELOGIN option points outside the message")
            options[token] = payload[offset : offset + length]
            position += PRELOGIN_OPTION.size
    except (IndexError, struct.error) as error:
        raise TdsProtocolError("the PRELOGIN answer is truncated") from error
    return options


def build_login7(
    *, server: str, database: str, token: str, fedauth_echo: bool, nonce: bytes
) -> bytes:
    token_bytes = token.encode("utf-16-le")
    fedauth = (
        bytes([(FEDAUTH_SECURITY_TOKEN << 1) | int(fedauth_echo)])
        + struct.pack("<I", len(token_bytes))
        + token_bytes
        + nonce
    )
    feature_ext = (
        bytes([FEATURE_FEDAUTH])
        + struct.pack("<I", len(fedauth))
        + fedauth
        + bytes([FEATURE_TERMINATOR])
    )
    server_bytes = server.encode("utf-16-le")
    database_bytes = database.encode("utf-16-le")
    server_at = LOGIN7_FIXED_LENGTH
    extension_at = server_at + len(server_bytes)
    database_at = extension_at + 4
    feature_at = database_at + len(database_bytes)
    total = feature_at + len(feature_ext)
    empty = server_at  # zero-length fields point at the start of the variable part
    head = struct.pack("<IIIIII", total, TDS_8_VERSION, TDS_PACKET_SIZE, 0, os.getpid() & 0xFFFF, 0)
    flags = bytes([LOGIN7_OPTION_FLAGS1, LOGIN7_OPTION_FLAGS2, 0, LOGIN7_OPTION_FLAGS3])
    locale = struct.pack("<iI", 0, LOGIN7_LCID)
    # (offset, length) of host, user, password, application, server and the extension pointer
    first_fields = struct.pack(
        "<12H", empty, 0, empty, 0, empty, 0, empty, 0,
        server_at, len(server_bytes) // 2, extension_at, 4,
    )  # fmt: skip
    # client interface, language, database; then the 6-byte client ID
    second_fields = struct.pack("<6H", empty, 0, empty, 0, database_at, len(database_bytes) // 2)
    # SSPI, attach-database file, change password, long SSPI length
    last_fields = struct.pack("<6HI", empty, 0, empty, 0, empty, 0, 0)
    fixed = head + flags + locale + first_fields + second_fields + bytes(6) + last_fields
    return fixed + server_bytes + struct.pack("<I", feature_at) + database_bytes + feature_ext


def _skip_feature_ack(payload: bytes, position: int) -> int:
    while payload[position] != FEATURE_TERMINATOR:
        (length,) = struct.unpack_from("<I", payload, position + 1)
        position += 5 + length
    return position + 1


def parse_login_reply(payload: bytes) -> LoginReply:
    error_number: int | None = None
    accepted = False
    route: tuple[str, int] | None = None
    position = 0
    try:
        while position < len(payload):
            token = payload[position]
            position += 1
            if token in DONE_TOKENS:
                position += DONE_LENGTH
            elif token == TOKEN_FEATUREEXTACK:
                position = _skip_feature_ack(payload, position)
            elif token == TOKEN_FEDAUTHINFO:
                (length,) = struct.unpack_from("<I", payload, position)
                position += 4 + length
            elif token in TOKENS_WITH_SHORT_LENGTH:
                (length,) = struct.unpack_from("<H", payload, position)
                body = payload[position + 2 : position + 2 + length]
                position += 2 + length
                if token == TOKEN_ERROR and error_number is None:
                    (error_number,) = struct.unpack_from("<i", body)
                elif token == TOKEN_LOGINACK:
                    accepted = True
                elif token == TOKEN_ENVCHANGE and body[:1] == bytes([ENVCHANGE_ROUTING]):
                    _, _, _, port, chars = ROUTE.unpack_from(body)
                    host = body[ROUTE.size : ROUTE.size + 2 * chars].decode("utf-16-le")
                    route = (host, port)
            else:
                raise TdsProtocolError(f"unexpected TDS token 0x{token:02x}")
    except (IndexError, struct.error, UnicodeDecodeError) as error:
        raise TdsProtocolError("the login answer is truncated") from error
    return LoginReply(error_number, accepted, route)


def _login_once(host: str, port: int, database: str, token: str) -> LoginReply:
    stream = open_tds_stream(host, port)
    try:
        stream.sendall(tds_packets(TDS_PRELOGIN, build_prelogin()))
        options = parse_prelogin(read_tds_message(stream))
        login = build_login7(
            server=host,
            database=database,
            token=token,
            fedauth_echo=options.get(PRELOGIN_FEDAUTHREQUIRED, b"")[:1] == b"\x01",
            nonce=options.get(PRELOGIN_NONCE, b""),
        )
        stream.sendall(tds_packets(TDS_LOGIN7, login))
        return parse_login_reply(read_tds_message(stream))
    finally:
        stream.close()


def tds_login(server: str, database: str, token: str) -> int | None:
    """Log in once with an Entra token: None when accepted, else the server's error number.

    Transport failures raise OSError. A gateway that routes the session to the database's node
    is followed once, so a database that is still resuming answers with its own error.
    """
    host, port = server, TDS_PORT
    for hop in range(MAX_ROUTES + 1):
        reply = _login_once(host, port, database, token)
        if reply.error_number is not None:
            return reply.error_number
        if reply.route is not None and hop < MAX_ROUTES:
            host, port = reply.route
            continue
        if reply.accepted:
            return None
        break
    raise TdsProtocolError("the server neither accepted nor refused the login")


# --- targets --------------------------------------------------------------------------------


def sql_rule_name() -> str | None:
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
    if re.fullmatch(r"[0-9]+", run_id) and re.fullmatch(r"[0-9]+", attempt):
        return f"{SQL_RULE_PREFIX}{run_id}-{attempt}"
    return None


class SqlTarget:
    """The environment's SQL server: one firewall rule `ci-runner-<run ID>-<run attempt>`."""

    allowlist_variable = OPERATOR_ALLOWLIST

    def __init__(self, environment: str) -> None:
        self.resource_group = f"rg-docchat-{environment}"

    def _server(self) -> tuple[str, str]:
        servers = az_json(
            "sql", "server", "list", "--resource-group", self.resource_group,
            "--query", "[].{name: name, fqdn: fullyQualifiedDomainName}",
        )  # fmt: skip
        if not isinstance(servers, list) or len(servers) != 1:
            raise AzError("az sql server list", f"{self.resource_group} holds no single server")
        server = servers[0]
        return str(server["name"]), str(server["fqdn"])

    def _rules(self, server: str) -> list[str]:
        names = az_json(
            "sql", "server", "firewall-rule", "list", "--resource-group", self.resource_group,
            "--server", server, "--query", "[].name",
        )  # fmt: skip
        return [str(name) for name in names or []]

    def _delete(self, server: str, name: str) -> None:
        az(
            "sql", "server", "firewall-rule", "delete", "--resource-group", self.resource_group,
            "--server", server, "--name", name,
        )  # fmt: skip
        report_progress(action="removed", target="sql", rule=name)

    def sweep(self) -> None:
        server, _ = self._server()
        for name in self._rules(server):
            if name.startswith(SQL_RULE_PREFIX):
                self._delete(server, name)

    def open(self, address: str) -> None:
        name = sql_rule_name()
        if name is None:
            raise AzError("GITHUB_RUN_ID", "GITHUB_RUN_ID and GITHUB_RUN_ATTEMPT must be set")
        server, fqdn = self._server()
        token = access_token(SQL_SCOPE)
        az(
            "sql", "server", "firewall-rule", "create", "--resource-group", self.resource_group,
            "--server", server, "--name", name, "--start-ip-address", address,
            "--end-ip-address", address, "--output", "none",
        )  # fmt: skip
        report_progress(action="added", target="sql", rule=name)
        try:
            effective = wait_until_effective(lambda: self._probe(fqdn, token))
        except AzError:
            self._remove_after_failure(server, name)
            raise
        if not effective:
            self._remove_after_failure(server, name)
            raise FirewallError("rule_not_effective", f"{name}: TDS logins kept failing")

    @staticmethod
    def _probe(fqdn: str, token: str) -> bool:
        try:
            error_number = tds_login(fqdn, SQL_DATABASE, token)
        except OSError:
            return False
        # Any answer other than the firewall's refusal means the address got through.
        return error_number != SQL_FIREWALL_REFUSAL

    def _remove_after_failure(self, server: str, name: str) -> None:
        # The failure being reported is the open; a rule left behind goes to the next sweep.
        with contextlib.suppress(AzError):
            self._delete(server, name)

    def close(self) -> None:
        name = sql_rule_name()
        if name is None:
            return
        server, _ = self._server()
        if name in self._rules(server):
            self._delete(server, name)


class RegistryTarget:
    """The shared registry: one IP rule for the runner's address."""

    allowlist_variable = SHARED_ALLOWLIST

    def _registry(self) -> str:
        names = az_json(
            "acr", "list", "--resource-group", SHARED_RESOURCE_GROUP, "--query", "[].name"
        )
        if not isinstance(names, list) or len(names) != 1:
            raise AzError("az acr list", f"{SHARED_RESOURCE_GROUP} holds no single registry")
        return str(names[0])

    def _rules(self, registry: str) -> dict[str, str]:
        listing = az_json(
            "acr", "network-rule", "list", "--name", registry,
            "--resource-group", SHARED_RESOURCE_GROUP,
        )  # fmt: skip
        return {bare_address(value): value for value in ip_rule_values(listing)}

    def _add(self, registry: str, address: str) -> None:
        az(
            "acr", "network-rule", "add", "--name", registry,
            "--resource-group", SHARED_RESOURCE_GROUP, "--ip-address", address, "--output", "none",
        )  # fmt: skip
        report_progress(action="added", target="registry", address=address)

    def _remove(self, registry: str, value: str) -> None:
        az(
            "acr", "network-rule", "remove", "--name", registry,
            "--resource-group", SHARED_RESOURCE_GROUP, "--ip-address", value, "--output", "none",
        )  # fmt: skip
        report_progress(action="removed", target="registry", address=bare_address(value))

    def sweep(self) -> None:
        shared = known_shared_allowlist()
        if shared is None:
            warn_allowlist_unknown("registry")
            return
        registry = self._registry()
        for address, value in self._rules(registry).items():
            if address not in shared:
                self._remove(registry, value)

    def open(self, address: str) -> None:
        registry = self._registry()
        write_state("registry", {"address": address, "openedAt": iso(now())})
        if address not in self._rules(registry):
            self._add(registry, address)
        url = f"https://{registry}.azurecr.io/v2/"
        try:
            effective = wait_until_effective(lambda: self._probe(url))
        except AzError:
            self._remove_after_failure(registry, address)
            raise
        if not effective:
            self._remove_after_failure(registry, address)
            raise FirewallError("rule_not_effective", f"{url} kept refusing the runner")

    @staticmethod
    def _probe(url: str) -> bool:
        try:
            # 401 is the anonymous answer of a reachable registry; 403 is its firewall.
            return http_status(url, HTTP_TIMEOUT_SECONDS) == REGISTRY_REACHABLE
        except OSError:
            return False

    def _remove_after_failure(self, registry: str, address: str) -> None:
        with contextlib.suppress(AzError):
            value = self._rules(registry).get(address)
            if value is not None:
                self._remove(registry, value)

    def close(self) -> None:
        state = read_state("registry")
        address = state.get("address") if state else None
        if not isinstance(address, str):
            return
        registry = self._registry()
        value = self._rules(registry).get(address)
        if value is not None:
            self._remove(registry, value)


class VaultTarget:
    """The test-credential vault: a `<address>/32` rule for one read window (ADR 0122 item 5).

    No concurrency group serialises the test jobs, so every write is read back and repeated, and
    `open` and `close` touch only this runner's address: without the shared allowlist a job can't
    tell a dead runner's address from an operator's, so such leftovers wait for runbook R26.
    """

    allowlist_variable = SHARED_ALLOWLIST

    def __init__(self) -> None:
        self.last_probe_error = ""

    @staticmethod
    def _vault() -> str:
        uri = os.environ.get(TEST_VAULT_URI, "")
        match = re.fullmatch(r"https://([a-z0-9-]{3,24})\.vault\.azure\.net/?", uri.strip())
        if match is None:
            raise AzError(TEST_VAULT_URI, "is not https://<vault>.vault.azure.net/")
        return match.group(1)

    @staticmethod
    def _rules(vault: str) -> dict[str, str]:
        listing = az_json(
            "keyvault", "network-rule", "list", "--name", vault,
            "--resource-group", SHARED_RESOURCE_GROUP,
        )  # fmt: skip
        return {bare_address(value): value for value in ip_rule_values(listing)}

    @staticmethod
    def _add(vault: str, address: str) -> None:
        az(
            "keyvault", "network-rule", "add", "--name", vault,
            "--resource-group", SHARED_RESOURCE_GROUP, "--ip-address", f"{address}/32",
            "--output", "none",
        )  # fmt: skip
        report_progress(action="added", target="vault", address=address)

    @staticmethod
    def _remove(vault: str, value: str) -> None:
        az(
            "keyvault", "network-rule", "remove", "--name", vault,
            "--resource-group", SHARED_RESOURCE_GROUP, "--ip-address", value, "--output", "none",
        )  # fmt: skip
        report_progress(action="removed", target="vault", address=bare_address(value))

    def _write_verified(self, vault: str, address: str) -> bool:
        """Write the rule until a read-back lists it: a concurrent job's write can undo it."""
        for attempt in range(1, VAULT_WRITE_ATTEMPTS + 1):
            with contextlib.suppress(AzError):
                self._add(vault, address)
            with contextlib.suppress(AzError):
                if address in self._rules(vault):
                    return True
            if attempt < VAULT_WRITE_ATTEMPTS:
                sleep(jitter(*VAULT_WRITE_WAIT_SECONDS))
        return False

    def _remove_verified(self, vault: str, address: str) -> None:
        for _ in range(VAULT_CLOSE_ATTEMPTS):
            value = self._rules(vault).get(address)
            if value is None:
                return
            with contextlib.suppress(AzError):
                self._remove(vault, value)
        if address in self._rules(vault):
            raise FirewallError(
                "rule_close_failed",
                f"{address} was still listed after {VAULT_CLOSE_ATTEMPTS} removals",
            )

    def _probe(self, vault: str, address: str) -> bool:
        try:
            if address not in self._rules(vault):
                self._add(vault, address)
        except AzError as error:
            self.last_probe_error = error.detail
            return False
        result = run_command(
            [
                "az", "keyvault", "secret", "list", "--vault-name", vault,
                "--maxresults", "1", "--output", "none",
            ]
        )  # fmt: skip
        if result.returncode == 0:
            return True
        # 403 ForbiddenByFirewall means not yet; anything else is retried within the window too.
        self.last_probe_error = (
            VAULT_FIREWALL_REFUSAL
            if VAULT_FIREWALL_REFUSAL in result.stderr
            else (result.stderr.strip().splitlines() or ["no error output"])[-1][:200]
        )
        return False

    def sweep(self) -> None:
        shared = known_shared_allowlist()
        if shared is None:
            warn_allowlist_unknown("vault")
            return
        vault = self._vault()
        for address in self._rules(vault):
            if address not in shared:
                self._remove_verified(vault, address)

    def open(self, address: str) -> None:
        vault = self._vault()
        # Recorded before the first write, so the job's always-run close still finds the address
        # when this process dies mid-write.
        write_state("vault", {"address": address, "openedAt": iso(now())})
        if address not in self._rules(vault) and not self._write_verified(vault, address):
            raise FirewallError(
                "rule_not_effective",
                f"the vault's ipRules lacked {address} after {VAULT_WRITE_ATTEMPTS} writes",
            )
        if not wait_until_effective(lambda: self._probe(vault, address)):
            with contextlib.suppress(AzError, FirewallError):
                self._remove_verified(vault, address)
            raise FirewallError(
                "rule_not_effective",
                f"vault reads failed for {PROBE_LIMIT_SECONDS:.0f} s: {self.last_probe_error}",
            )

    def close(self) -> None:
        state = read_state("vault")
        address = state.get("address") if state else None
        if not isinstance(address, str):
            return
        self._remove_verified(self._vault(), address)


def make_target(target: str, environment: str) -> SqlTarget | RegistryTarget | VaultTarget:
    if target == "sql":
        return SqlTarget(environment)
    if target == "registry":
        return RegistryTarget()
    return VaultTarget()


def open_rule(target: SqlTarget | RegistryTarget | VaultTarget) -> None:
    address = resolve_runner_address()
    if not in_github_actions():
        # Operators work from allowlisted machines, so outside a job nothing is added.
        if address in parse_allowlist(target.allowlist_variable):
            report_progress(action="allowlisted", address=address)
            return
        raise FirewallError("not_in_allowlist", f"{address} isn't in {target.allowlist_variable}")
    target.open(address)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="firewall.py", description="Transient runner rules (83-infrastructure.md 4.6)."
    )
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--target", required=True, choices=TARGETS)
    parser.add_argument("--env", required=True, choices=ENVIRONMENTS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.target == "vault" and args.env not in VAULT_ENVIRONMENTS:
        parser.error("--target vault takes --env dev or --env load")
    target = make_target(args.target, args.env)
    try:
        if args.command == "open":
            open_rule(target)
        elif args.command == "close":
            target.close()
        else:
            target.sweep()
    except FirewallError as error:
        report_failure(error.code, error.detail)
        return EXIT_CODES[error.code]
    except AzError as error:
        code = FAILURE_BY_COMMAND[args.command]
        report_failure(code, str(error))
        return EXIT_CODES[code]
    return 0


if __name__ == "__main__":
    sys.exit(main())
