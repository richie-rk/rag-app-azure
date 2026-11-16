"""Ops config: runtime operational values held as typed rows of SQL `app.ops_config`
(`81-config.md` section 3; `80-data-model.md` sections 2.7 and 2.24; ADR 0028).

`OpsConfig` is the root model with one sub-model per area; `OpsConfigStore` loads the areas a
process registers and serves one immutable snapshot per unit of work, refreshed on the first
access after 60 seconds; `encode_value` is the canonical stored form of a value. The `ingestion`
area's model, `IngestionOpsConfig`, lives in `docchat_core.ingestion.settings`.
"""

from docchat_core.ingestion.settings import IngestionOpsConfig
from docchat_core.opsconfig.areas import (
    EFFORTS_CONTEXT_KEY,
    ApiOps,
    ChatOps,
    ConversationsOps,
    FeedbackOps,
    FollowupsOps,
    MemoryOps,
    ModelRate,
    OpsArea,
    OpsReasoningEffort,
    OpsRuleError,
    PricingOps,
    QuotaOps,
    RateLimitOps,
    RetrievalOps,
    SummaryOps,
    efforts_context,
)
from docchat_core.opsconfig.root import (
    API_AREAS,
    AREA_MODELS,
    CATALOGUE,
    EFFORT_AREAS,
    INGESTION_AREAS,
    Area,
    AreaUnavailableError,
    OpsConfig,
    area_keys,
    failing_keys,
    validate_area,
)
from docchat_core.opsconfig.store import (
    READ_ALL,
    READ_STATEMENT_NAME,
    SNAPSHOT_MAX_AGE_SECONDS,
    UNREACHABLE_RETRY_SECONDS,
    OpsConfigInvalidError,
    OpsConfigStore,
    RowReader,
)
from docchat_core.opsconfig.telemetry import report_invalid
from docchat_core.opsconfig.values import StoredValueError, area_of, decode_value, encode_value

__all__ = [
    "API_AREAS",
    "AREA_MODELS",
    "CATALOGUE",
    "EFFORTS_CONTEXT_KEY",
    "EFFORT_AREAS",
    "INGESTION_AREAS",
    "READ_ALL",
    "READ_STATEMENT_NAME",
    "SNAPSHOT_MAX_AGE_SECONDS",
    "UNREACHABLE_RETRY_SECONDS",
    "ApiOps",
    "Area",
    "AreaUnavailableError",
    "ChatOps",
    "ConversationsOps",
    "FeedbackOps",
    "FollowupsOps",
    "IngestionOpsConfig",
    "MemoryOps",
    "ModelRate",
    "OpsArea",
    "OpsConfig",
    "OpsConfigInvalidError",
    "OpsConfigStore",
    "OpsReasoningEffort",
    "OpsRuleError",
    "PricingOps",
    "QuotaOps",
    "RateLimitOps",
    "RetrievalOps",
    "RowReader",
    "StoredValueError",
    "SummaryOps",
    "area_keys",
    "area_of",
    "decode_value",
    "efforts_context",
    "encode_value",
    "failing_keys",
    "report_invalid",
    "validate_area",
]
