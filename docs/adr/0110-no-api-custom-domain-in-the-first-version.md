# ADR 0110: No API custom domain in the first version

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Architect
- **Specified in:** 83-infrastructure.md, 10-web-client.md, 81-config.md
- **Related ADRs:** 0004, 0016, 0017, 0083, 0086, 0095, 0098

## Context

The browser calls the API's own origin with bearer tokens (ADR 0004). That origin appears in two places the deployment writes per environment: `apiBaseUrl` in `config.json` and `connect-src` in the web client's Content Security Policy (ADRs 0083, 0086), so the deploy-time generator needs exactly one source for it. A custom host name on App Service needs a host-name binding, a certificate and two DNS records in a public zone the organisation controls (a CNAME or A record and a TXT `asuid` verification record), which this product's templates can't create or validate; no feature of the product needs the API on a custom host. The web client is different: users type its address, so Static Web Apps custom domains stay a parameter (`webCustomDomains`, at most 2 on Free and 5 on Standard).

## Decision

1. **The default host only.** The API is served only at `app-docchat-api-<env>-<token>.azurewebsites.net`. No `Microsoft.Web/sites/hostNameBindings`, `Microsoft.Web/certificates` or DNS record is declared for the API.
2. **One value.** `main.bicep` outputs `API_BASE_URL` = `https://app-docchat-api-<env>-<token>.azurewebsites.net` (scheme and host, no path, no trailing slash), always. The web deploy generator writes it as `apiBaseUrl` in `config.json` and its origin in place of `__API_ORIGIN__` in the CSP's `connect-src`; the web client calls nothing else on the API side.
3. **Identity is host-independent.** The API registration's identifier URI is `api://<tenantId>/docchat-<env>-<token>-api` and its scope `…/access_as_user` (ADR 0016); Easy Auth answers `401` without redirecting, so no redirect URI names the API's host.
4. **Web custom domains stay.** `webCustomDomains` adds Static Web Apps custom domains with `validationMethod: 'cname-delegation'`; platform CORS on the API allows `https://<Static Web App default host name>` plus `https://<domain>` for each entry, with `supportCredentials: false`; `WEB_BASE_URL` is the first custom domain, else the default host.
5. **Adding an API domain later** is infrastructure and deploy-time configuration only: the organisation adds the two records App Service requires in its public DNS zone (a CNAME to the default host name and a TXT record `asuid.<subdomain>` holding the app's domain verification ID); the template declares the host-name binding with an App Service managed certificate; `API_BASE_URL` changes to the new origin; the API is deployed on it; the web client is redeployed so `config.json` and the CSP change together; the smoke tests run; the old origin is retired (ADR 0083). No application code reads its own host name.

## Alternatives considered

- **A custom API domain with an App Service managed certificate in the template.** Rejected: the certificate's validation depends on DNS records outside the template, so a first provision couldn't complete unattended, and the product gains nothing a user sees.
- **A bring-your-own certificate in Key Vault.** Rejected: a certificate is a secret to renew and rotate, and every environment vault stays empty in the first version.
- **Azure Front Door serving the web client and the API on one origin.** Not adopted now: it is the fallback if the deployed CORS preflight test fails (ADR 0004), and the web application firewall it would carry is outside the first version.
- **A custom domain only in `prod`.** Rejected: `dev` would then test a different origin path from the one `prod` serves.

## Consequences

- The API's host name shows the environment and resource token in browser network traffic; nothing in it is secret, and the token is deterministic, so the host never changes for an environment.
- `config.json` and the CSP are written from one value, so the origin the client calls and the origin the CSP allows can't differ.
- Tests (84-testing.md): after every web deploy `config.json` answers `200` with `apiBaseUrl` equal to `API_BASE_URL`, and the response's `Content-Security-Policy` names that origin; in `dev`, a CORS preflight from the web origin succeeds without a token and one from another origin gets no `Access-Control-Allow-Origin`.
- Every example of `apiBaseUrl` follows the naming rule of ADR 0095: `https://app-docchat-api-prod-a1b2c3.azurewebsites.net`.

## References

- App Service custom domains and managed certificates: https://learn.microsoft.com/en-us/azure/app-service/app-service-web-tutorial-custom-domain (checked 2026-10-21)
- App Service CORS settings (`cors.allowedOrigins`, `supportCredentials`): https://learn.microsoft.com/en-us/azure/templates/microsoft.web/sites (checked 2026-10-14)
- Static Web Apps plans (custom domain limits): https://learn.microsoft.com/en-us/azure/static-web-apps/plans (checked 2026-10-14)
