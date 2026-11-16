# ADR 0123: azd container deployment for the function app

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Architect
- **Specified in:** 83-infrastructure.md, 01-repository.md, 84-testing.md, 90-build-order.md
- **Related ADRs:** 0005, 0103, 0106, 0121, 0124, 0127, 0137, 0141

## Context

The ingestion app runs on Linux Elastic Premium as a custom container (ADR 0005) with public network access disabled, its deployment (SCM) site refusing every request and basic-auth publishing off (ADR 0106). Every environment deploys the exact image digest that `main`'s pipeline built, scanned and attested (ADR 0124), so the deploy path must neither rebuild nor re-tag an image.

A function app's container image can be set directly through the site's configuration, or by azd as a service of `azure.yaml`. azd's `function` host deploys containers from version 1.30.0, and an option deploys an image reference exactly as given. Because azd's container logic would also accept a function app under the `appservice` host, the design states which host it uses and keeps a direct configuration update as the stage-0 fallback.

Vendor facts (checked 2026-10-14):

- azd 1.30.0 (2026-08-05) added container image deployment for `host: function` (Dockerfile, prebuilt image or remote registry build); 1.32.0 (2026-08-26) added `docker.imagePassthrough`; 1.29.0 updates the image through the dedicated App Service configuration endpoint; the latest stable azd is 1.35.1 (2026-10-07), which fetches Bicep CLI 0.47.16.
- With `docker.imagePassthrough: true`, azd uses the service's `image` (fully qualified) exactly as given, tag and digest preserved, and doesn't build, pull, tag, copy or publish it; `azd deploy <service> --from-package <reference>` overrides it for one run; it can't be combined with `docker.remoteBuild`. `--from-package` needs a service name and can't be combined with `--all` or `--preview`; `--timeout` defaults to 1,200 seconds.
- azd's function target hands container deployment to the App Service container logic, which checks only that the target is a `Microsoft.Web/sites` resource whose kind contains `linux` and whose `linuxFxVersion` starts with `DOCKER|`, then sets `linuxFxVersion` through `WebAppsClient.UpdateConfiguration` (the `sites/{name}/config/web` endpoint) with no Kudu or SCM call, so it works with SCM blocked and basic-auth publishing off. It always deploys to the main site; a prebuilt image needs language `docker` or none.
- The azure.yaml schema's function-host rule "supports code or container deployment from project or image"; the host table on Microsoft Learn lists neither `image` nor `docker` for the function host, which lags the 1.30.0 release.

## Decision

1. **The `ingestion` service is an azd `function` service.** `azure.yaml` (with `requiredVersions.azd: ">= 1.35.1"`) declares:

   ```yaml
   ingestion:
     host: function
     image: ${DOCCHAT_INGESTION_IMAGE}
     docker:
       imagePassthrough: true
   ```

   azd finds the function app by its tag `azd-service-name: ingestion`. azd 1.35.1 is the floor because 1.30.0 added the container deployment, 1.32.0 the passthrough, and 1.35.1 bundles the Bicep version CI lints with.
2. **Deploy by digest.** The environment's `deploy` job runs `azd deploy api --from-package <apiImage> --no-prompt`, then `azd deploy ingestion --from-package <ingestionImage> --no-prompt`, where each reference is `<login server>/docchat/<image>@sha256:<64 hex>` from the run's `publish` or `resolve` job (ADR 0124). azd sets `linuxFxVersion = 'DOCKER|<reference>'` through `sites/config/web`, which restarts the app; nothing is built, pulled or pushed by the deploy, and the app keeps `publicNetworkAccess: 'Disabled'`.
3. **The site azd needs.** Bicep provisions the function app with `kind: 'functionapp,linux,container'`, `linuxFxVersion: 'DOCKER|<current ingestion image reference>'` (the running image, so provisioning never changes it, ADR 0124), `acrUseManagedIdentityCreds: true`, `acrUserManagedIdentityID` = the ingestion identity's client ID (resource ID if spike SP-4 shows that form is needed) and `outboundVnetRouting.imagePullTraffic: true`, so the app pulls the digest through the registry's private endpoint with its own identity.
4. **Verification after deploy.** The azd `postdeploy` hook (`scripts/deploy/hooks/postdeploy.py`) reads the function app's `linuxFxVersion` and fails with `deploy_verification_failed` unless it equals `DOCKER|<expected reference>`; it takes the deploy start as the app's `properties.lastModifiedTimeUtc`, read right after the image change, and polls the Logs query API every 30 seconds, for at most 10 minutes, for an `AppEvents` row `ingestion_worker_started` with `AppRoleName == 'docchat-ingestion'` and `AppVersion` equal to the release commit after that time; no row fails it with `ingestion_start_failed`. Each ingestion worker process emits that event once at start-up, with the image's `DOCCHAT_BUILD_COMMIT` as its version (ADR 0137).
5. **Spike SP-11 decides, at stage 0.** `azd deploy ingestion --from-package <digest>` runs against `dev`'s spike function app with public access disabled. If it sets `linuxFxVersion` and the app starts the image, the service stays as in item 1 and the result is written into this ADR. If it fails, the fallback applies: the `ingestion` service is removed from `azure.yaml`, and the `postdeploy` hook sets the image with `az resource update --ids <function app ID>/config/web --set properties.linuxFxVersion="DOCKER|<reference>"` before running the verification of item 4; the deploy job's step stays the same apart from that.
6. **Operators deploy the same way.** From an allowlisted machine an operator runs `azd provision`, `azd deploy api --from-package <apiImage>`, `azd deploy ingestion --from-package <ingestionImage>` and then `deploy_web.py` (ADR 0121); `azd up` isn't used, because the `web` service's `predeploy` hook refuses azd's own web deployment.

## Alternatives considered

- **A pipeline step and `postdeploy` hook that set the image as the primary path.** Kept only as the fallback: azd's own service deployment gives `azd deploy ingestion` the same digest handling, logging and service mapping as `api`, with one code path less to maintain.
- **azd's `appservice` host pointed at the function app.** Rejected: it would work only because azd's container logic doesn't check for a function app, an accident of the implementation; the `function` host is the documented host for function apps and deploys containers through the same endpoint.
- **azd without `imagePassthrough`.** Rejected: azd would pull, re-tag and push the image to its own repository and tag, so the deployed reference wouldn't be the scanned, attested digest.
- **Zip or code deployment of the ingestion app.** Rejected: the page-wise method needs OS packages baked into the image (ADR 0005), and code deployment needs the SCM site, which refuses every request.
- **Continuous deployment through a registry webhook.** Rejected: any push to the tag would restart the app outside the pipeline's approvals and verification.

## Consequences

- One mechanism deploys both containers: `azd deploy <service> --from-package <digest>`, then the same `postdeploy` verification, in CI and from an operator's machine.
- The deploy depends on azd ≥ 1.35.1 behaving as described; the stage-0 spike proves it before any later stage relies on it, and the fallback needs only the hook change of item 5.
- A failed deploy leaves the previous image running if the image change didn't happen, or the new image starting; `deploy_verification_failed` and `ingestion_start_failed` stop the run before `prod` is touched, and the operator re-runs or rolls back (ADR 0127).
- azd is pinned at 1.35.1 in the deploy job (`Azure/setup-azd`) and floored by `requiredVersions`; moving to a newer azd is a reviewed pull request that changes both and passes the `dev` deploy's verification before it reaches `prod`.
- Tests (84-testing.md): unit tests prove that the `preprovision` hook rejects tag-only references and accepts digest references, and that the `postdeploy` hook builds the Logs query with the release commit and the app's `lastModifiedTimeUtc`, passes on the first row and fails with `ingestion_start_failed` after 10 minutes without one; the deployed `dev` tests prove that the API and ingestion images run from digests equal to the release manifest; stage-0 spike SP-11 proves the deployment itself.

## References

- azd changelog (1.29.0, 1.30.0, 1.32.0, 1.35.1): https://github.com/Azure/azure-dev/blob/main/cli/azd/CHANGELOG.md (checked 2026-10-14)
- azd function service target: https://github.com/Azure/azure-dev/blob/main/cli/azd/pkg/project/service_target_functionapp.go (checked 2026-10-14)
- azd App Service container update: https://github.com/Azure/azure-dev/blob/main/cli/azd/pkg/azapi/webapp.go (checked 2026-10-14)
- azure.yaml schema (`docker.imagePassthrough`, function host): https://github.com/Azure/azure-dev/blob/main/schemas/v1.0/azure.yaml.json (checked 2026-10-14)
- azd command reference (`deploy --from-package`): https://learn.microsoft.com/en-us/azure/developer/azure-developer-cli/reference (checked 2026-10-14)
- azure.yaml schema on Microsoft Learn: https://learn.microsoft.com/en-us/azure/developer/azure-developer-cli/azd-schema (checked 2026-10-14)
