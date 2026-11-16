# ADR 0121: Static Web Apps deployment token

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Product owner and architect
- **Specified in:** 83-infrastructure.md, 82-security.md, 84-testing.md, 86-operations.md
- **Related ADRs:** 0083, 0086, 0103, 0115, 0120, 0124, 0127, 0142

## Context

No secret may live in code, the browser bundle or CI, and every Azure sign-in in delivery uses OIDC (ADR 0120). The web client is the exception that the platform imposes: Static Web Apps authorises a content deployment only with its deployment token or a GitHub access token, never with Microsoft Entra ID or an OIDC token for Azure, and azd's `staticwebapp` host reads that token through ARM and passes it to a Static Web Apps CLI it fetches at its latest version. The product owner accepted the token as a recorded exception: it is the one key-based credential in delivery, read at deploy time and never stored, reset on a schedule and whenever exposure is suspected, and handed only to a pinned CLI. The deploy identity already holds Contributor on the environment (ADR 0115), which includes the `listSecrets` action, so reading the token adds no right; what this ADR controls is where the token travels and which program receives it.

Vendor facts (checked 2026-10-14 unless stated):

- "Static Web Apps supports either using an Azure deployment token (recommended), or a GitHub access token" as the deployment authorization policy.
- azd's `staticwebapp` host reads the token through `ListStaticSiteSecrets` (property `apiKey`), then runs `npx -y @azure/static-web-apps-cli@latest deploy --deployment-token <token> --env production`.
- The Static Web Apps CLI environment variable `SWA_CLI_DEPLOY_BINARY_VERSION` is the "Deployment binary version to use", default `stable` (checked 2026-10-15); the CLI downloads that deploy binary at run time.
- `@azure/static-web-apps-cli` 2.0.10 is the latest release; `Microsoft.Web/staticSites@2025-03-01` is the latest GA API.

## Decision

1. **One key in delivery.** The deployment token (`properties.apiKey` of `Microsoft.Web/staticSites/listSecrets/action`) of each environment's static web app is the only key-based credential the delivery path uses; every Azure sign-in is OIDC (ADR 0120). The site has no `repositoryUrl`, `branch` or build properties, so nothing but this token publishes content to it.
2. **Read at deploy time, never stored.** `scripts/deploy/deploy_web.py --env <env> --dist <artifact directory> [--swa-cli <archive>]`, run in the environment's `deploy` job as its deploy identity:
   1. copies the downloaded `web-dist` artifact (one environment-free build, ADR 0086) to a fresh directory;
   2. runs `node apps/web/scripts/write-env-config.mjs --dist <directory>` with exactly `AZURE_TENANT_ID`, `WEB_CLIENT_ID`, `API_BASE_URL` and `API_SCOPE` from `azd env get-values` and no other input;
   3. reads the token with `az staticwebapp secrets list --name <static web app> --resource-group rg-docchat-<env> --query properties.apiKey -o tsv`, prints `::add-mask::<token>` at once, and passes it only in the child process's environment as `SWA_CLI_DEPLOYMENT_TOKEN`: never in a GitHub secret, a file, a log line, a job output or an artifact;
   4. runs the pinned CLI (item 3);
   5. checks that `https://<static web app host>/config.json` answers `200` with the expected `apiBaseUrl` and that the response's `Content-Security-Policy` names the API origin.
   A failed upload exits 2 with `swa_deploy_failed` and a failed check with `web_config_check_failed`; the previous build keeps serving until an upload succeeds, because Static Web Apps swaps content per deployment. The API always deploys before the web client in the same job.
3. **A pinned CLI that no token-holding job installs.** `scripts/deploy/swa-cli/package.json` and its lockfile pin `@azure/static-web-apps-cli` 2.0.10 exactly. The credential-free `build` job (push) or `resolve-web` job (dispatch) runs `npm ci --ignore-scripts` in that folder, packs it with `tar -czf swa-cli.tgz` and uploads the artifact `swa-cli` (90 days). The deploy job extracts it and runs `npx --no-install swa deploy <directory> --env production --no-use-keychain` with a child environment of exactly `PATH`, `HOME`, `SWA_CLI_DEPLOYMENT_TOKEN` and `SWA_CLI_DEPLOY_BINARY_VERSION=stable`, so the CLI never sees `ACTIONS_ID_TOKEN_REQUEST_URL`, `ACTIONS_ID_TOKEN_REQUEST_TOKEN` or `ACTIONS_RUNTIME_TOKEN`. Locally an operator runs `npm ci --ignore-scripts` in that folder and passes `--swa-cli` the packed folder. Dependabot's `npm` entry covers `/scripts/deploy/swa-cli`, so a new CLI version arrives only as a reviewed pull request.
4. **azd's own web deployment is refused.** The `web` service stays in `azure.yaml` (`host: staticwebapp`, `project: apps/web`, `dist: dist`) so azd maps and shows it, and its `predeploy` hook `scripts/deploy/hooks/web_predeploy.py` exits 1 with `azd_web_deploy_refused` ("Deploy the web client with scripts/deploy/deploy_web.py, which pins the Static Web Apps CLI."). Neither `azd deploy web` nor `azd up` can upload the web client through azd's floating CLI or without the generated `config.json`.
5. **The accepted residual.** The CLI package is pinned, but the deploy binary it downloads follows Microsoft's `stable` channel, so the program that receives the token can change without a repository change. `deploy_web.py` writes the deploy binary's build ID and SHA-256 checksum, as the CLI reports them, to the job summary, so every run shows which binary ran.
6. **Rotation every 90 days and on suspected exposure** (runbook R14): the bootstrap operator (Owner on the subscription; operators hold only Reader, which lacks the reset action) runs `az staticwebapp secrets reset-api-key --name <static web app> --resource-group rg-docchat-<env>`, which invalidates the old token at once; a maintainer then dispatches `deploy.yaml` with `environment`, the current release's `image_commit` and `rollback: true` (images only, the web client rebuilt at that commit, `prod` behind its approval), whose web step reads the new token, and confirms that `config.json` answers `200`; the reset date goes into the ticket that tracks this credential. Nothing is copied anywhere, because every deploy reads the token afresh.
7. **Audit.** Every read is an Administrative Activity Log event `Microsoft.Web/staticSites/listSecrets/action` under the deploy identity, and every reset one under the bootstrap operator, both kept 365 days in `log-docchat-security`.

## Alternatives considered

- **azd's built-in web deployment.** Rejected: it runs the CLI at `@latest`, unpinned, in the job that holds the deploy identity's token, and it writes no environment configuration.
- **The token as a GitHub Actions secret.** Rejected: it would be a stored secret readable by any workflow change, and each rotation would mean copying it.
- **The GitHub access-token authorization policy.** Rejected: Microsoft recommends the deployment token, and the GitHub policy links the site to a repository and branch, which this site leaves empty so that only the pipeline's one verified build reaches it.
- **Resetting the token after every deploy.** Rejected: only the deploy identity can read the token, and it can read a new one just as well, so a per-deploy reset protects against nothing that masking and the child environment don't already cover, and it adds a failure mode in which the upload succeeds and the reset fails.

## Consequences

- Whoever holds a token can publish static content to that one environment's site until the next reset; the site serves only static files and its `config.json` and CSP are checked after every upload, so a rogue upload is visible at the next deploy.
- The deploy binary is the one program in delivery that the repository doesn't pin; the job summaries let an operator see when it changed.
- Obligations: runbook R14 every 90 days per environment and at once on suspected exposure; Dependabot pull requests for the CLI pin.
- Tests (84-testing.md): unit tests prove that `web_predeploy.py` exits 1 with `azd_web_deploy_refused`, and that `deploy_web.py` passes the generator exactly four inputs and the CLI a child environment without `ACTIONS_ID_TOKEN_REQUEST_URL`, `ACTIONS_ID_TOKEN_REQUEST_TOKEN` and `ACTIONS_RUNTIME_TOKEN`, with `SWA_CLI_DEPLOY_BINARY_VERSION=stable`; a CI check proves that no job holding `id-token: write` runs `npm ci`; the deployed web checks run after every web deploy.

## References

- Static Web Apps build configuration and deployment authorization: https://learn.microsoft.com/en-us/azure/static-web-apps/build-configuration (checked 2026-10-14)
- Static Web Apps resource (API 2025-03-01): https://learn.microsoft.com/en-us/azure/templates/microsoft.web/staticsites (checked 2026-10-14)
- Static Web Apps CLI environment variables: https://azure.github.io/static-web-apps-cli/docs/cli/env-vars (checked 2026-10-15)
- Static Web Apps CLI releases (2.0.10): https://github.com/Azure/static-web-apps-cli/releases (checked 2026-10-14)
- azd's Static Web Apps deployment: https://github.com/Azure/azure-dev/blob/main/cli/azd/pkg/tools/swa/swa.go and https://github.com/Azure/azure-dev/blob/main/cli/azd/pkg/azapi/static_webapp.go (checked 2026-10-14)
