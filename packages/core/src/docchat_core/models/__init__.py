"""Identifiers and value types shared by several `docchat_core` subpackages (ADR 0015)."""

from docchat_core.models.clock import Clock, SystemClock
from docchat_core.models.identifiers import (
    CORRELATION_ID_PATTERN,
    LOWER_CASE_UUID_PATTERN,
    UUID_PATTERN,
    CanonicalUuid,
    CorrelationId,
    LowerCaseUuid,
    parse_uuid,
)
from docchat_core.models.utc import UTC_TIME_PATTERN, UtcDateTime, format_utc, to_utc
from docchat_core.models.values import (
    COUNT_MAX,
    ROW_VERSION_BYTES,
    ROW_VERSION_PATTERN,
    SHA256_PATTERN,
    Count,
    RowVersion,
    Sha256Hex,
    decode_row_version,
    encode_row_version,
)

__all__ = [
    "CORRELATION_ID_PATTERN",
    "COUNT_MAX",
    "LOWER_CASE_UUID_PATTERN",
    "ROW_VERSION_BYTES",
    "ROW_VERSION_PATTERN",
    "SHA256_PATTERN",
    "UTC_TIME_PATTERN",
    "UUID_PATTERN",
    "CanonicalUuid",
    "Clock",
    "CorrelationId",
    "Count",
    "LowerCaseUuid",
    "RowVersion",
    "Sha256Hex",
    "SystemClock",
    "UtcDateTime",
    "decode_row_version",
    "encode_row_version",
    "format_utc",
    "parse_uuid",
    "to_utc",
]
