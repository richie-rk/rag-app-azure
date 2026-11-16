"""Whether the process runs inside App Service or Functions (`81-config.md` sections 6.1 and 6.3).

Both platforms set `WEBSITE_SITE_NAME` and `WEBSITE_INSTANCE_ID` in every site, and no developer
machine or CI job sets either, so their presence is what refuses the local-only settings.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Final

AZURE_HOST_VARIABLES: Final = ("WEBSITE_SITE_NAME", "WEBSITE_INSTANCE_ID")


def running_in_azure(environ: Mapping[str, str] | None = None) -> bool:
    """True when either platform variable is set; an empty value counts, so the guard fails
    closed."""
    env = os.environ if environ is None else environ
    return any(name in env for name in AZURE_HOST_VARIABLES)
