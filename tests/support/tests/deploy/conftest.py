"""The deploy scripts run as `python scripts/deploy/<script>.py`, with their own directory on the
import path; the tests import them the same way."""

from __future__ import annotations

import sys
from pathlib import Path

_DEPLOY = str(Path(__file__).resolve().parents[4] / "scripts" / "deploy")
if _DEPLOY not in sys.path:
    sys.path.insert(0, _DEPLOY)
