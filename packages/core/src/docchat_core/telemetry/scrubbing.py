"""The scrubbing safety net (`85-observability.md` section 3.6; ADR 0014 items 5 and 6).

Registered attributes keep content out of telemetry by construction; these attach points catch
what slips through anyway: secret-shaped strings inside an allowed value, URLs with query strings,
SQL text and headers recorded by instrumentation, and exception text, which can quote a question,
a file name or a service's error body that no pattern recognises. So no exception message,
argument or note leaves the process by any path: exceptions are reduced to frames and qualified
type names.

Each attach point is fail-closed: an exception while scrubbing a value replaces that value with
`[redacted:error]` and counts `docchat.telemetry.scrub_errors`, so a record is never exported
unscrubbed and never lost.
"""

from __future__ import annotations

import logging
import re
import traceback
from collections.abc import Iterator, MutableMapping
from typing import Any, Final, override

from opentelemetry.attributes import BoundedAttributes
from opentelemetry.context import Context
from opentelemetry.sdk._logs import LogRecordProcessor, ReadWriteLogRecord
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
from opentelemetry.trace import Status

from docchat_core.errors import CODES, ITEM_CODES
from docchat_core.telemetry.attributes import QUALIFIED_TYPE_NAME_PATTERN
from docchat_core.telemetry.instruments import SCRUB_ERRORS

REDACTED: Final = "[redacted]"
REDACTED_ERROR: Final = "[redacted:error]"
REDACTED_STACKTRACE: Final = "[redacted:stacktrace]"

TRACEBACK_HEADER: Final = "Traceback (most recent call last):"
CAUSE_SENTENCE: Final = "The above exception was the direct cause of the following exception:"
CONTEXT_SENTENCE: Final = "During handling of the above exception, another exception occurred:"

ERROR_CODE_KEY: Final = "docchat.error_code"
EXCEPTION_TYPE_KEY: Final = "exception.type"
EXCEPTION_MESSAGE_KEY: Final = "exception.message"
EXCEPTION_STACKTRACE_KEY: Final = "exception.stacktrace"
EXCEPTION_EVENT: Final = "exception"

# The patterns of section 3.6, applied in this order; `\1` and `\2` are the table's `$1` and `$2`.
PATTERNS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"), "[redacted:jwt]"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [redacted]"),
    (
        re.compile(
            r"(?i)\b(AccountKey|SharedAccessKey|SharedAccessSignature|Password|Pwd|ClientSecret)"
            r"\s*=\s*[^;\"'\s]+"
        ),
        r"\1=[redacted]",
    ),
    (
        re.compile(
            r"(?i)([?&](sig|skoid|sktid|code|token|access_token|id_token|refresh_token"
            r"|client_secret)=)[^&#\s\"']+"
        ),
        r"\1[redacted]",
    ),
    (re.compile(r"\b[A-Za-z0-9+/]{86}=="), "[redacted:key]"),
    (
        re.compile(
            r"(?i)\b(api[-_]?key|secret|password|passwd|token|client[-_]?secret|private[-_]?key"
            r"|ocp-apim-subscription-key)([\"']?\s*[:=]\s*[\"']?)[^\"'\s,;&}]{4,}"
        ),
        r"\1\2[redacted]",
    ),
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
        "[redacted:private-key]",
    ),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[redacted:email]"),
)

URL_KEYS: Final = frozenset({"url.full", "http.url", "http.target"})
"""Attributes that lose everything from the first `?`."""

REMOVED_KEYS: Final = frozenset({"url.query", "db.query.text", "db.statement"})
HEADER_PREFIXES: Final = ("http.request.header.", "http.response.header.")
ALLOWED_HEADERS: Final = frozenset({"content-type", "content-length", "traceparent"})

# Python's own group limits, so a pathological group can't make a stack trace unbounded.
_MAX_GROUP_WIDTH: Final = 15
_MAX_GROUP_DEPTH: Final = 10

_QUALIFIED_TYPE_NAME_RE: Final = re.compile(QUALIFIED_TYPE_NAME_PATTERN)
_GROUP_MARKER_RE: Final = re.compile(r"^(?P<prefix>\s*[|+] )(?P<body>.*)$", re.DOTALL)
_BOX_RE: Final = re.compile(r"^\+[-+]*(?: (?:\d+|\.\.\.) -+)?$")

_STANDARD_RECORD_ATTRIBUTES: Final = frozenset(
    vars(logging.LogRecord("", logging.INFO, "", 0, "", (), None))
) | {"message", "asctime", "taskName"}


def scrub(text: str) -> str:
    """Replace every secret-shaped part of `text` by its marker, patterns 1 to 8 in order.

    UUIDs, 32-hex trace IDs, 64-hex SHA-256 values, ISO 8601 timestamps and numbers match no
    pattern, so they pass unchanged.
    """
    for pattern, replacement in PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def qualified_type_name(exc_type: type[BaseException]) -> str:
    """`module.QualName` of an exception class, the form of `error.type` and `exception.type`."""
    return f"{exc_type.__module__}.{exc_type.__qualname__}"


def catalogue_message(code: object) -> str | None:
    """The catalogue message of an error code (`CODES`) or an ingestion item code, or `None`."""
    if not isinstance(code, str):
        return None
    entry = CODES.get(code)
    if entry is not None:
        return entry.detail
    item = ITEM_CODES.get(code)
    return None if item is None else item.message


def safe_traceback(exc: BaseException) -> str:
    """Render `exc` from its frames alone: for the exception, each chained cause and context
    (Python's order, honouring `__suppress_context__`) and each exception-group member, the
    header, the `traceback.format_tb()` lines and the qualified type name, never a message,
    argument or note."""
    return "\n".join(_render(exc, frozenset(), 0)) + "\n"


def _render(exc: BaseException, path: frozenset[int], depth: int) -> list[str]:
    # `path` holds the exceptions being rendered around this one, so a cycle in the chain or a
    # group that contains itself ends instead of recursing forever.
    path |= {id(exc)}
    lines: list[str] = []
    cause, context = exc.__cause__, exc.__context__
    if cause is not None:
        if id(cause) not in path:
            lines += [*_render(cause, path, depth), "", CAUSE_SENTENCE, ""]
    elif context is not None and not exc.__suppress_context__ and id(context) not in path:
        lines += [*_render(context, path, depth), "", CONTEXT_SENTENCE, ""]
    lines.append(TRACEBACK_HEADER)
    for frame in traceback.format_tb(exc.__traceback__):
        lines.extend(frame.rstrip("\n").split("\n"))
    lines.append(qualified_type_name(type(exc)))
    if isinstance(exc, BaseExceptionGroup) and depth < _MAX_GROUP_DEPTH:
        for member in exc.exceptions[:_MAX_GROUP_WIDTH]:
            if id(member) not in path:
                lines += ["", *_render(member, path, depth + 1)]
    return lines


def reduce_stacktrace(text: str) -> str:
    """Keep only the structure of a rendered stack trace: headers, frame lines with their source
    and position-marker lines, the chaining sentences, exception-group box lines (with the `| `
    prefix of nested lines) and, from each final line, the type name before its first `:`.

    Everything after a final line is dropped until the next header, chaining sentence or box
    line, so message continuation lines and notes never survive. Text with no header at all is
    replaced whole by `[redacted:stacktrace]`.
    """
    if text == REDACTED_STACKTRACE:
        return text
    kept: list[str] = []
    saw_header = False
    state = "expect"
    for line in text.split("\n"):
        marker = _GROUP_MARKER_RE.match(line)
        prefix, body = (marker["prefix"], marker["body"]) if marker else ("", line)
        stripped = body.strip()
        kept_line = line
        if stripped.endswith(TRACEBACK_HEADER):
            saw_header, state = True, "trace"
        elif stripped in (CAUSE_SENTENCE, CONTEXT_SENTENCE) or _BOX_RE.fullmatch(stripped):
            state = "expect"
        elif not stripped:
            # A blank line carries nothing, so the layout survives.
            pass
        elif state == "drop" or (state == "expect" and body.startswith(" ")):
            continue
        elif state == "trace" and body.startswith(" "):
            pass
        else:
            kept_line, state = prefix + body.split(":", 1)[0].rstrip(), "drop"
        kept.append(kept_line)
    if not saw_header:
        return REDACTED_STACKTRACE
    return "\n".join(kept)


def is_removed_attribute(key: str) -> bool:
    """`url.query`, SQL text and every header attribute except the three allowed headers."""
    if key in REMOVED_KEYS:
        return True
    for header_prefix in HEADER_PREFIXES:
        if key.startswith(header_prefix):
            header = key.removeprefix(header_prefix).lower().replace("_", "-")
            return header not in ALLOWED_HEADERS
    return False


def _scrubbed_value(key: str, value: object) -> object:
    if isinstance(value, str):
        if key in URL_KEYS:
            value = value.split("?", 1)[0]
        return scrub(value)
    if isinstance(value, list | tuple):
        return tuple(scrub(item) if isinstance(item, str) else item for item in value)
    return value


def _count_error() -> None:
    try:
        SCRUB_ERRORS.add(1)
    except Exception:  # noqa: BLE001 - counting must never break the record it is about
        return


def _apply_exception_rules(store: MutableMapping[str, Any], error_code: object) -> None:
    if EXCEPTION_MESSAGE_KEY in store:
        try:
            message = catalogue_message(store.get(ERROR_CODE_KEY, error_code))
            if message is None:
                exc_type = store.get(EXCEPTION_TYPE_KEY)
                message = exc_type if isinstance(exc_type, str) else None
            if message is None:
                del store[EXCEPTION_MESSAGE_KEY]
            else:
                store[EXCEPTION_MESSAGE_KEY] = message
        except Exception:  # noqa: BLE001 - fail closed: the value is replaced, never kept
            store[EXCEPTION_MESSAGE_KEY] = REDACTED_ERROR
            _count_error()
    stacktrace = store.get(EXCEPTION_STACKTRACE_KEY)
    if stacktrace is not None:
        try:
            reduced = reduce_stacktrace(str(stacktrace))
        except Exception:  # noqa: BLE001 - fail closed
            reduced = REDACTED_ERROR
            _count_error()
        else:
            if reduced == REDACTED_STACKTRACE and stacktrace != REDACTED_STACKTRACE:
                _count_error()
        store[EXCEPTION_STACKTRACE_KEY] = reduced


def scrub_attributes(
    store: MutableMapping[str, Any], *, exception: bool = False, error_code: object = None
) -> None:
    """Apply the attribute rules, then the exception rules when `exception` is true, then the
    patterns to every string value, in place."""
    for key in list(store):
        try:
            if is_removed_attribute(key):
                del store[key]
        except Exception:  # noqa: BLE001 - fail closed
            store[key] = REDACTED_ERROR
            _count_error()
    if exception:
        _apply_exception_rules(store, error_code)
    for key, value in list(store.items()):
        try:
            scrubbed = _scrubbed_value(key, value)
        except Exception:  # noqa: BLE001 - fail closed
            scrubbed = REDACTED_ERROR
            _count_error()
        if scrubbed is not value:
            store[key] = scrubbed


def _carries_exception(store: MutableMapping[str, Any]) -> bool:
    return any(key.startswith("exception.") for key in store)


def attribute_store(owner: object) -> MutableMapping[str, Any]:
    """The writable dictionary behind the immutable attributes of an ended span or of an event.

    The SDK hands the processor a read-only view of attributes it has made immutable, so the
    span processor writes through the `_attributes` store and its `BoundedAttributes` dictionary;
    a unit test fails when that internal changes, and the exporter pin moves only through a
    reviewed pull request (section 3.6).
    """
    attributes = getattr(owner, "_attributes", None)
    store = getattr(attributes, "_dict", None)
    if not isinstance(attributes, BoundedAttributes) or not isinstance(store, dict):
        raise TypeError("the SDK's span attribute store changed")
    return store


def _safe_description(description: str, error_code: object) -> str:
    if catalogue_message(error_code) is not None and isinstance(error_code, str):
        return error_code
    head = description.split(":", 1)[0].strip()
    if catalogue_message(head) is not None or _QUALIFIED_TYPE_NAME_RE.fullmatch(head):
        return head
    return REDACTED


class ScrubbingSpanProcessor(SpanProcessor):
    """Scrubs every string attribute of an ended span and of each of its events, applies the
    exception rules to every `exception` event (whoever recorded it, instrumentation included),
    and keeps only a code or type name in an error status description."""

    @override
    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        return

    @override
    def on_end(self, span: ReadableSpan) -> None:
        span_store: MutableMapping[str, Any] | None
        try:
            span_store = attribute_store(span)
        except Exception:  # noqa: BLE001 - the internal changed; counted, and its test fails
            span_store = None
            _count_error()
        error_code = None if span_store is None else span_store.get(ERROR_CODE_KEY)
        if span_store:
            scrub_attributes(span_store)
        for event in span.events:
            if not event.attributes:
                continue
            try:
                event_store = attribute_store(event)
            except Exception:  # noqa: BLE001 - as above
                _count_error()
                continue
            scrub_attributes(
                event_store,
                exception=event.name == EXCEPTION_EVENT or _carries_exception(event_store),
                error_code=error_code,
            )
        self._scrub_status(span, error_code)

    @staticmethod
    def _scrub_status(span: ReadableSpan, error_code: object) -> None:
        description = span.status.description
        if not description:
            return
        try:
            safe = _safe_description(description, error_code)
        except Exception:  # noqa: BLE001 - fail closed
            safe = REDACTED_ERROR
            _count_error()
        if safe != description:
            # Like the attributes, the status of an ended span is written through the SDK
            # internal; `ReadableSpan.status` reads it back.
            span._status = Status(span.status.status_code, safe)

    @override
    def shutdown(self) -> None:
        return

    @override
    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


class ScrubbingLogRecordProcessor(LogRecordProcessor):
    """Scrubs the body and every string attribute of an OpenTelemetry log record, with the
    exception rules for a record that carries `exception.*` attributes."""

    @override
    def on_emit(self, log_record: ReadWriteLogRecord) -> None:
        record = log_record.log_record
        if isinstance(record.body, str):
            try:
                record.body = scrub(record.body)
            except Exception:  # noqa: BLE001 - fail closed
                record.body = REDACTED_ERROR
                _count_error()
        attributes = record.attributes
        if isinstance(attributes, MutableMapping) and attributes:
            scrub_attributes(
                attributes,
                exception=_carries_exception(attributes),
                error_code=attributes.get(ERROR_CODE_KEY),
            )

    @override
    def shutdown(self) -> None:
        return

    @override
    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


class ScrubbingLogFilter(logging.Filter):
    """The standard-library filter on the `docchat` logger and on the stderr handler: scrubs the
    rendered message (`msg` with its `args`) and every string `extra` value, and renders a
    record's exception as `safe_traceback`. It never drops a record."""

    @override
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = scrub(record.getMessage())
        except Exception:  # noqa: BLE001 - fail closed
            record.msg = REDACTED_ERROR
            _count_error()
        record.args = ()
        for key, value in list(_extra_strings(record)):
            try:
                setattr(record, key, scrub(value))
            except Exception:  # noqa: BLE001 - fail closed
                setattr(record, key, REDACTED_ERROR)
                _count_error()
        if record.exc_info and record.exc_info[1] is not None:
            try:
                record.exc_text = safe_traceback(record.exc_info[1])
            except Exception:  # noqa: BLE001 - fail closed
                record.exc_text = REDACTED_ERROR
                _count_error()
        return True


def _extra_strings(record: logging.LogRecord) -> Iterator[tuple[str, str]]:
    for key, value in vars(record).items():
        if key not in _STANDARD_RECORD_ATTRIBUTES and isinstance(value, str):
            yield key, value
