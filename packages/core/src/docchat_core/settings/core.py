"""`CoreSettings`: the `DOCCHAT_CORE_*` settings both apps read (`81-config.md` section 2.3)."""

from __future__ import annotations

from enum import StrEnum

from docchat_core.models.identifiers import CanonicalUuid, LowerCaseUuid
from docchat_core.settings.base import DocChatSettings


class EnvironmentName(StrEnum):
    """`DOCCHAT_CORE_ENVIRONMENT`. Bicep writes only `dev`, `prod` or `load`; `local` exists only
    on a developer machine or in a CI job, and start-up refuses it inside Azure."""

    DEV = "dev"
    PROD = "prod"
    LOAD = "load"
    LOCAL = "local"


class CoreSettings(DocChatSettings):
    """Read by the API and the ingestion app.

    `core_environment` selects the non-production identity rules and the credential (the Azure
    CLI's only in `local`) and is a telemetry dimension. `core_identity_client_id` is the client ID
    of the process's one managed-identity credential, read only by `docchat_core.clients`; start-up
    requires it outside `local` (`check_identity_client_id`). `core_workload_principal_id` is the
    actor of `system` audit rows.
    """

    core_environment: EnvironmentName
    core_identity_client_id: LowerCaseUuid | None = None
    core_workload_principal_id: CanonicalUuid
