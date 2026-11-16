"""Gunicorn's configuration for the API image (`11-api.md` section 1.15; `81-config.md` sections
2.1 and 6.1; `85-observability.md` section 1.6; ADR 0007).

Before Gunicorn forks a worker, every settings class of the API loads and start-up checks 1 to 10
run in order (the test client's prod refusal among them), then the two encodings load from
`TIKTOKEN_CACHE_DIR`; the first failure writes one error record to standard error and exits the
master with code 78, so a misconfigured container never answers `GET /healthz`. Each worker's
lifespan then creates the clients and loads the ops-config snapshot (check 11).

The worker count, the graceful timeout (inside App Service's 120-second stop window) and the
worker heartbeat come from `ApiSettings`. No access log: request lines would carry search text and
identifiers; request telemetry comes from spans.
"""

from docchat_api.settings import check_encodings, load_process_settings
from docchat_core.settings import StartupCheckError, exit_on_startup_failure

try:
    _settings = load_process_settings()
    check_encodings(_settings.api)
except StartupCheckError as failure:
    exit_on_startup_failure(failure)

# Every interface of the container: App Service forwards WEBSITES_PORT 8000 to it.
bind = "0.0.0.0:8000"
workers = _settings.api.api_workers
worker_class = "docchat_api.http.worker.DocChatUvicornWorker"
graceful_timeout = _settings.api.api_graceful_timeout_seconds
timeout = _settings.api.api_worker_timeout_seconds
accesslog = None
errorlog = "-"
loglevel = "warning"
capture_output = False
