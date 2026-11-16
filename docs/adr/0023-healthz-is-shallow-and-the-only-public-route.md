# ADR 0023: Healthz is shallow and the only public route

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decided by:** Architect
- **Specified in:** 11-api.md, 82-security.md, 83-infrastructure.md, 84-testing.md, 85-observability.md, 86-operations.md
- **Related ADRs:** 0004, 0006, 0007, 0012, 0017, 0036, 0133, 0138

## Context

The API runs on Azure App Service with at least 2 instances in `prod`, and the platform must take an instance that stops answering out of rotation so users don't keep landing on it. Every route requires a bearer token (ADR 0017) except one: App Service's health check needs a path it can call, and that route must expose nothing. A process with bad configuration must stop at start-up rather than fail every request, so it never reports healthy.

Facts that bound the choice:

- App Service's health check pings the configured path every minute on each instance. It removes an instance from the load balancer after `WEBSITE_HEALTHCHECK_MAXPINGFAILURES` non-`2xx` responses (default 10, range 2 to 10) or no response within 1 minute, replaces an instance that stays unhealthy for 1 hour (at most 1 per hour and 3 per day per plan), never removes more than `WEBSITE_HEALTHCHECK_MAXUNHEALTHYWORKERPERCENT` of instances (default 50), and needs at least 2 instances to reroute.
- The health check integrates with platform authentication (Easy Auth) with no extra settings, so no `excludedPaths` entry is needed, and an anonymous external call to the deployed `/healthz` gets the platform's `401`.
- SQL, Cosmos DB, AI Search and Azure OpenAI outages hit every instance alike; replacing an instance doesn't cure them.
- `dev` SQL is serverless with auto-pause on and a 15-minute delay, and auto-pause starts only when the database has zero sessions for the whole delay.

## Decision

1. **The one public route.** `GET /healthz` (`getHealth`) is the only route outside `/v1` and the only one exempt from in-app token validation, the request limits, provisioning and every other check. It answers `200` with an empty body (`Content-Length: 0`) and the headers every response carries (ADR 0012): `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Strict-Transport-Security: max-age=31536000`. It reads no data, calls no dependency and writes no log record; its server span is still exported. Any other method gets `405 method_not_allowed`.
2. **Liveness only.** It reports that the process has started and its event loop answers, nothing more: a dependency failure isn't an instance failure. It answers only after the lifespan start-up has completed (settings validated, clients created, ops config loaded), because Uvicorn serves only after start-up, and a process whose settings fail validation exits before serving, so a misconfigured instance never reports healthy.
3. **Still behind platform authentication.** Easy Auth has no excluded path. App Service's health check passes through it with no settings; an anonymous external call to the deployed `/healthz` gets the platform's `401`, which deployed tests expect.
4. **App Service settings.** `siteConfig.healthCheckPath: '/healthz'` and `WEBSITE_HEALTHCHECK_MAXPINGFAILURES` = 3, so an instance that fails 3 pings (about 3 minutes at one ping a minute) leaves rotation. `prod` runs at least 2 instances (autoscale minimum 2, maximum 6).
5. **Contract.** In the OpenAPI document `getHealth` is the only operation with `x-docchat-access: public` and `security: []`; the authorisation matrix fails unless the public set is exactly `GET /healthz`, answering an anonymous in-process call `200` with an empty body.
6. **Monitoring and deployment gate.** Alerts `api-down` (`HealthCheckStatus` average below 1 over 5 minutes, severity 1) and `api-instance-unhealthy` (below 100 over 15 minutes, severity 2), both with the incident-response runbook. Availability and latency queries exclude `GET /healthz`. The azd `postdeploy` hook polls `HealthCheckStatus` every 30 s and fails the deployment with `health_check_failed` unless it reads 100 within 10 minutes.

## Alternatives considered

- **A deep readiness check (SQL `SELECT 1`, a Cosmos DB read, a Search or Azure OpenAI call).** A short SQL outage would mark every instance unhealthy at once; App Service would pull up to half of them and replace instances that aren't broken, turning a partial outage into a total one. One query per minute per instance would also keep a session open on the `dev` database often enough that its 15-minute auto-pause could never start (ADR 0036). Dependencies are watched through their own telemetry and alerts.
- **Excluding `/healthz` from platform authentication.** The health check already passes Easy Auth, so an exclusion would only add an anonymous surface with no caller that needs it.
- **Separate liveness and readiness routes.** App Service's health check takes one path, and a second unauthenticated route breaks the one-public-route rule.
- **A body with status, version or build details.** Nothing reads it, and it tells any caller what is deployed; release verification reads the configured image digest from `linuxFxVersion`.
- **The platform default of 10 failed pings.** An unresponsive instance would keep its share of traffic for up to 10 minutes; 3 pings remove it within about 3 minutes while one slow ping doesn't.

## Consequences

- An instance whose process hangs, or whose event loop is blocked for a minute, leaves rotation after 3 failed pings and is replaced after 1 hour unhealthy; an instance with a failing dependency stays in rotation, and the dependency shows in the 5xx alerts, the user-provisioning alert and the dependency dashboards.
- `dev` (B1, 1 instance) and `load` (1 instance at the start) have no second instance to reroute to; there the health check only drives replacement and the alerts.
- Deployed tests: an anonymous `GET /healthz` gets `401` from platform authentication while `HealthCheckStatus` reads healthy (the smoke suite after every `dev` deploy, and the `prod` smoke check); the authorisation matrix fails if another route becomes public or `/healthz` gains a check; a unit test checks the response headers on `/healthz`; the contract test checks that `getHealth` alone has `security: []`.
- Locally, and if platform authentication is ever set to pass anonymous requests through (the fallback ADR 0004 keeps for a failed CORS preflight), `/healthz` answers without a token; it discloses only that the process is up.
- Nothing can use this route to drain an instance before a stop; the shutdown drain of ADR 0007 starts from the stop signal.

## References

- Monitor App Service instances by using health check (ping interval, failure threshold, replacement limits, Easy Auth integration): https://learn.microsoft.com/en-us/azure/app-service/monitor-instances-health-check (checked 2026-10-14)
- App settings reference (`WEBSITE_HEALTHCHECK_MAXPINGFAILURES`, warm-up and start-time settings): https://learn.microsoft.com/en-us/azure/app-service/reference-app-settings (checked 2026-10-14)
- `Microsoft.Web/sites` Bicep reference (`siteConfig.healthCheckPath`): https://learn.microsoft.com/en-us/azure/templates/microsoft.web/sites?pivots=deployment-language-bicep (checked 2026-10-14)
- Serverless tier auto-pause and resume (zero sessions for the whole delay): https://learn.microsoft.com/en-us/azure/azure-sql/database/serverless-tier-auto-pause-resume?view=azuresql (checked 2026-10-14)
