"""The ops-config root model, the catalogue of keys and the per-area validation
(`81-config.md` section 3.1; ADR 0028 items 2 and 4).

`OpsConfig` holds one sub-model per area. A process registers only the areas it reads (the API the
first eleven, the ingestion app `ingestion`), so a snapshot holds only those; reading an area the
snapshot doesn't hold raises `AreaUnavailableError`, a defect of the caller's registration rather
than a configuration problem. `validate_area` is the loader's rule, shared by `OpsConfigStore` and
`docchat-ops config set`: keys the area's model doesn't declare are dropped first, because an
expand migration ships a key before the code that reads it.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from types import MappingProxyType
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from docchat_core.ingestion.settings import IngestionOpsConfig
from docchat_core.opsconfig.areas import (
    ApiOps,
    ChatOps,
    ConversationsOps,
    FeedbackOps,
    FollowupsOps,
    MemoryOps,
    OpsRuleError,
    PricingOps,
    QuotaOps,
    RateLimitOps,
    RetrievalOps,
    SummaryOps,
    efforts_context,
)

type Area = Literal[
    "api",
    "chat",
    "retrieval",
    "followups",
    "ratelimit",
    "quota",
    "pricing",
    "conversations",
    "summary",
    "memory",
    "feedback",
    "ingestion",
]

AREA_MODELS: Final[Mapping[Area, type[BaseModel]]] = MappingProxyType(
    {
        "api": ApiOps,
        "chat": ChatOps,
        "retrieval": RetrievalOps,
        "followups": FollowupsOps,
        "ratelimit": RateLimitOps,
        "quota": QuotaOps,
        "pricing": PricingOps,
        "conversations": ConversationsOps,
        "summary": SummaryOps,
        "memory": MemoryOps,
        "feedback": FeedbackOps,
        "ingestion": IngestionOpsConfig,
    }
)
"""The twelve areas and their models, in the order of `81-config.md` section 3.1."""

API_AREAS: Final[tuple[Area, ...]] = (
    "api",
    "chat",
    "retrieval",
    "followups",
    "ratelimit",
    "quota",
    "pricing",
    "conversations",
    "summary",
    "memory",
    "feedback",
)
"""The areas the API registers."""

INGESTION_AREAS: Final[tuple[Area, ...]] = ("ingestion",)
"""The areas the ingestion app registers."""

EFFORT_AREAS: Final[frozenset[Area]] = frozenset({"summary", "memory"})
"""The areas whose validation needs the chat-small model's accepted reasoning efforts."""


class AreaUnavailableError(LookupError):
    """The snapshot holds no valid sub-model of an area: the process didn't register it, or no
    stored value of it has validated since the process started."""

    def __init__(self, area: Area) -> None:
        super().__init__(f"the ops-config snapshot holds no valid area {area!r}")
        self.area: Area = area


def _present[M: BaseModel](model: M | None, area: Area) -> M:
    if model is None:
        raise AreaUnavailableError(area)
    return model


class OpsConfig(BaseModel):
    """One immutable snapshot of the registered areas. Callers read an area through its property
    (`ops.chat.context_token_budget`); the `<area>_area` fields are `None` for an area the
    snapshot doesn't hold.

    Pydantic runs an area's cross-field rules again when the root takes the area's instance, so a
    root holding `summary` or `memory` is validated with `efforts_context` too.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_by_alias=True,
        validate_by_name=True,
        serialize_by_alias=True,
    )

    api_area: ApiOps | None = Field(default=None, alias="api")
    chat_area: ChatOps | None = Field(default=None, alias="chat")
    retrieval_area: RetrievalOps | None = Field(default=None, alias="retrieval")
    followups_area: FollowupsOps | None = Field(default=None, alias="followups")
    ratelimit_area: RateLimitOps | None = Field(default=None, alias="ratelimit")
    quota_area: QuotaOps | None = Field(default=None, alias="quota")
    pricing_area: PricingOps | None = Field(default=None, alias="pricing")
    conversations_area: ConversationsOps | None = Field(default=None, alias="conversations")
    summary_area: SummaryOps | None = Field(default=None, alias="summary")
    memory_area: MemoryOps | None = Field(default=None, alias="memory")
    feedback_area: FeedbackOps | None = Field(default=None, alias="feedback")
    ingestion_area: IngestionOpsConfig | None = Field(default=None, alias="ingestion")

    @property
    def api(self) -> ApiOps:
        return _present(self.api_area, "api")

    @property
    def chat(self) -> ChatOps:
        return _present(self.chat_area, "chat")

    @property
    def retrieval(self) -> RetrievalOps:
        return _present(self.retrieval_area, "retrieval")

    @property
    def followups(self) -> FollowupsOps:
        return _present(self.followups_area, "followups")

    @property
    def ratelimit(self) -> RateLimitOps:
        return _present(self.ratelimit_area, "ratelimit")

    @property
    def quota(self) -> QuotaOps:
        return _present(self.quota_area, "quota")

    @property
    def pricing(self) -> PricingOps:
        return _present(self.pricing_area, "pricing")

    @property
    def conversations(self) -> ConversationsOps:
        return _present(self.conversations_area, "conversations")

    @property
    def summary(self) -> SummaryOps:
        return _present(self.summary_area, "summary")

    @property
    def memory(self) -> MemoryOps:
        return _present(self.memory_area, "memory")

    @property
    def feedback(self) -> FeedbackOps:
        return _present(self.feedback_area, "feedback")

    @property
    def ingestion(self) -> IngestionOpsConfig:
        return _present(self.ingestion_area, "ingestion")


def area_keys(area: Area) -> tuple[str, ...]:
    """The dotted keys an area's model declares, in declaration order."""
    keys: list[str] = []
    for name, field in AREA_MODELS[area].model_fields.items():
        if field.alias is None:
            raise TypeError(f"{name} has no dotted key")
        keys.append(field.alias)
    return tuple(keys)


CATALOGUE: Final[Mapping[str, Area]] = MappingProxyType(
    {key: area for area in AREA_MODELS for key in area_keys(area)}
)
"""Every key the running code declares, with its area: the 73 keys of `81-config.md` sections
3.4 to 3.8. `docchat-ops config set` refuses a key outside it."""


def validate_area(
    area: Area,
    values: Mapping[str, JsonValue],
    *,
    chat_small_efforts: Collection[str] | None = None,
) -> BaseModel:
    """Validate one area from the typed values of its keys.

    Keys of another area or undeclared by the area's model are dropped first. The summary and
    memory areas need `chat_small_efforts`, the chat-small model's row of the capability table;
    without it their reasoning-effort keys are refused. Raises `pydantic.ValidationError`;
    `failing_keys` names its keys.
    """
    declared = set(area_keys(area))
    data = {key: value for key, value in values.items() if key in declared}
    context = None if chat_small_efforts is None else efforts_context(chat_small_efforts)
    return AREA_MODELS[area].model_validate(data, context=context)


def failing_keys(area: Area, error: ValidationError) -> tuple[str, ...]:
    """The keys a failed `validate_area` names, without any value: a missing or invalid key by
    its location, a broken cross-field rule by the keys of the rule."""
    declared = area_keys(area)
    failing: dict[str, None] = {}
    for detail in error.errors(include_url=False, include_input=False, include_context=True):
        rule = detail.get("ctx", {}).get("error")
        location = detail["loc"]
        if isinstance(rule, OpsRuleError):
            failing.update(dict.fromkeys(rule.keys))
        elif location and location[0] in declared:
            failing[str(location[0])] = None
        else:
            # Only a rule raised without its keys lands here; every key of the area is suspect.
            failing.update(dict.fromkeys(declared))
    return tuple(failing)
