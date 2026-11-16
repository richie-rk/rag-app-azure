"""The `web` service's azd `predeploy` hook: it always refuses (section 9.1).

`azd deploy web` would run the Static Web Apps CLI at a floating version and skip the
`config.json` generator, so neither `azd deploy web` nor `azd up` may publish the web client;
`scripts/deploy/deploy_web.py` deploys it with the pinned CLI.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path

CODE = "azd_web_deploy_refused"
MESSAGE = (
    "Deploy the web client with scripts/deploy/deploy_web.py, which pins the Static Web Apps CLI."
)


def main() -> int:
    line = json.dumps({"code": CODE, "message": MESSAGE, "detail": "azd deploy web"})
    sys.stderr.write(line + "\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        # The exit code carries the refusal; a summary that can't be written mustn't mask it.
        with contextlib.suppress(OSError), Path(summary).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
