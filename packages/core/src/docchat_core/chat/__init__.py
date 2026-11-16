"""The chat pipeline's pieces that other components share (`12-chat-pipeline.md` section 1.3).

`citations.strip_citation_tokens` serves the conversation store, the pipeline and the operator
CLI; `models.MODEL_CAPABILITIES` is the model capability table that start-up, the ops-config
models and the deployed model check read; `capacity.capacity_shares` computes the two capacity
shares that bound the per-user request rate in `prod`. The pipeline itself lives in
`docchat_api.chat`.
"""

from docchat_core.chat.capacity import (
    SHARE_LIMIT,
    CapacityShares,
    SmallCall,
    capacity_shares,
)
from docchat_core.chat.citations import (
    CITATION_NUMBER_MAX,
    CITATION_TOKEN_CLOSE,
    CITATION_TOKEN_OPEN,
    strip_citation_tokens,
)
from docchat_core.chat.models import (
    MODEL_CAPABILITIES,
    REASONING_EFFORTS,
    ModelCapability,
    ReasoningEffort,
)

__all__ = [
    "CITATION_NUMBER_MAX",
    "CITATION_TOKEN_CLOSE",
    "CITATION_TOKEN_OPEN",
    "MODEL_CAPABILITIES",
    "REASONING_EFFORTS",
    "SHARE_LIMIT",
    "CapacityShares",
    "ModelCapability",
    "ReasoningEffort",
    "SmallCall",
    "capacity_shares",
    "strip_citation_tokens",
]
