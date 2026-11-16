# ADR 0088: Client trace context and no client analytics

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Architect
- **Specified in:** 10-web-client.md, 82-security.md, 84-testing.md, 85-observability.md, 86-operations.md
- **Related ADRs:** 0012, 0014, 0037, 0083, 0085, 0091, 0092, 0137

## Context

- Every user action is one trace, end to end, and every error a user sees is plain, says what to do next and includes a reference that operators can match to its server trace. Logs and telemetry carry metadata only, and no browser-side sink could guarantee that, so the browser's whole part in telemetry is the W3C `traceparent` header on its API calls. The browser console must never hold account names, conversations or access decisions.
- The API side: `correlationId` in every problem body and in the stream's `meta` and `error` events is the 32-character lower-case trace ID of the request's server span, and the Azure Monitor OpenTelemetry distro's FastAPI instrumentation continues the trace the client started, so the ID a user reports is the client's own. The API samples by trace ID at `DOCCHAT_TELEMETRY_SAMPLING_RATIO` and ignores the caller's sampled flag.
- W3C Trace Context: `traceparent` is version `00`, a 16-byte trace ID and an 8-byte parent ID in lower-case hexadecimal, and a flags byte; an all-zero trace ID or parent ID is invalid. Cross-origin scripts read only CORS-safelisted response headers, so any ID the client shows must come from a response body or from the client itself.

## Decision

1. **The header.** Every HTTP request to the API, including each automatic retry, the chat stream, each upload attempt and each file download, carries `traceparent: 00-<trace-id>-<parent-id>-01`, with the 16-byte trace ID and the 8-byte parent ID drawn from `crypto.getRandomValues`, written in lower-case hexadecimal and drawn again when all zero. Every request gets a new trace ID, which the client keeps with the request. Requests never carry `tracestate`, `baggage`, `Request-Id` or `X-Requested-With`. The `config.json` fetch and every Entra ID request carry no `traceparent`.
2. **The sampled flag is always `01`.** The browser has no sampler; the API's sampler decides from the trace ID and ignores the flag, so the browser can neither force nor suppress sampling.
3. **The reference ID.** Every failed API call has one: the problem's `correlationId` when the response carries a problem body, otherwise the trace ID the client sent (a bare `401` or `403` from platform authentication or App Service, a network failure, a CORS refusal). A stream failure the client detects itself shows `meta.correlationId` when `meta` arrived, otherwise the request's trace ID; an `error` event shows its own `correlationId`. A sign-in failure shows Entra ID's correlation ID from the MSAL error. A failure that involved no API call (configuration, clipboard, reading a file) shows none.
4. **Showing it.** Every error is a Fluent `MessageBar` with intent `error`: a title, the body text and, on its own line, the reference ID in a monospace font with a button that copies it and announces the copy. Actions such as Try again sit in the bar's actions.
5. **No client analytics.** No Application Insights JavaScript SDK, no third-party script, no beacon, no CSP report endpoint and no error-reporting service. Production builds write nothing to the console: ESLint `no-console` is an error, and the only writer, `devLog` in `src/platform/devLog.ts`, is a no-op unless `import.meta.env.DEV` is true and then writes only `{ code, referenceId }` and MSAL errors; MSAL's logger runs at `LogLevel.Error` with `piiLoggingEnabled: false` through `devLog`.
6. **Matching a report.** The user gives the reference ID; for API errors, operators find the request in Application Insights by that operation ID; for sign-in failures, they look up the correlation ID in the Entra ID sign-in logs. The browser holds no logs to collect.
7. **CORS.** The API's platform CORS allows the `traceparent` and `if-match` request headers, and the deployed preflight test sends both with `authorization` and `content-type`.

## Alternatives considered

- **The Application Insights JavaScript SDK, or OpenTelemetry in the browser.** Rejected: the header is all that matching a report needs; a browser sink would collect page URLs, titles and exception text that can hold user content, so metadata-only logging couldn't be guaranteed, and it would add an ingestion origin to `connect-src`.
- **One trace ID per page load or per session.** Rejected: every request would share one ID, so a reported reference couldn't single out the failing request.
- **The sampled flag `00`, or a sampler in the browser.** Rejected: a parent-based sampler would then drop every browser-started trace; the API samples by trace ID.
- **Reading the correlation ID from a response header.** Rejected: cross-origin scripts can't read it; the API puts it in the body.
- **CSP violation reports.** Rejected: client telemetry (ADR 0083); the browser tests catch violations.

## Consequences

- Every user-visible error leads to one server trace or one Entra ID sign-in record, so support never needs anything from the user's browser.
- The browser sends no telemetry and costs no ingestion; a rendering failure that never reaches the API is invisible to operators, and the user's reload and the browser tests cover it.
- Obligations (84-testing.md): unit tests that every request's `traceparent` matches `^00-[0-9a-f]{32}-[0-9a-f]{16}-01$` and is never all zeros, that 10,000 generated values hold no duplicate trace ID, and that the reference ID is the problem's `correlationId` when present and the request's trace ID otherwise; a browser test in which the stub API records a valid `traceparent` and a bearer token on every request, the stream, uploads and downloads included, while `config.json` and Entra ID requests carry none; the stub API fails any request without a valid `traceparent` (ADR 0092); a telemetry test proves the API's sampler ignores the parent's flag; the runbook for answering a user's error report (86-operations.md).

## References

- W3C Trace Context: https://www.w3.org/TR/trace-context/ (checked 2026-10-14)
- Azure Monitor OpenTelemetry distro for Python: https://learn.microsoft.com/en-us/python/api/overview/azure/monitor-opentelemetry-readme?view=azure-python (checked 2026-10-14)
- CORS-safelisted response headers: https://developer.mozilla.org/en-US/docs/Glossary/CORS-safelisted_response_header (checked 2026-10-14)
