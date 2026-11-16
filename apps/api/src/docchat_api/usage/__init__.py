"""Per-user limits, token usage and query cost (`12-chat-pipeline.md` sections 3, 15 and 16.5).

- `record_model_call(oid, record)`: the recorder every model call's `ModelCallUsage` goes through;
  `model_call_usage` and `estimate_cost_micros` build and price a record from the snapshot's
  `pricing.model_rates`.
- `check_token_quota(oid) -> QuotaCheck`: the background quota gate; `UsageStoreUnavailable` when
  the counters can't be read.
- `UsageService`, the worker's object (`ApiProcess.usage`): the request-rate window (P3), the token
  quota (P4), the stream lease (P5) and the citation-download window and lease, with the problems
  they raise (`RateLimited`, `TokenQuotaExceeded`, `TooManyStreams`, `TooManyDownloads`, and the
  platform's `DependencyUnavailable` when the store can't decide).
"""

from docchat_api.usage.container import UsageContainer, UsageStoreUnavailable
from docchat_api.usage.leases import Lease, TooManyDownloads, TooManyStreams
from docchat_api.usage.pricing import estimate_cost_micros, model_call_usage
from docchat_api.usage.quotas import QuotaCheck, TokenQuotaExceeded
from docchat_api.usage.service import UsageService, check_token_quota, record_model_call
from docchat_api.usage.windows import RateLimited

__all__ = [
    "Lease",
    "QuotaCheck",
    "RateLimited",
    "TokenQuotaExceeded",
    "TooManyDownloads",
    "TooManyStreams",
    "UsageContainer",
    "UsageService",
    "UsageStoreUnavailable",
    "check_token_quota",
    "estimate_cost_micros",
    "model_call_usage",
    "record_model_call",
]
