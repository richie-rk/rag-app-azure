# R35 — Change the API origin or add a custom domain

**Performer:** a maintainer; the DNS owner of the domain for part A.

**Trigger:** a new web host name, or a new API origin (`10-web-client.md` section 2.3, item 6).

**Prerequisites:** the operator workstation of `86-operations.md` section 1.5, with `.azure/<env>/.env` refreshed (`azd env refresh -e <env> --no-prompt`); `gh` signed in.

Placeholders in angle brackets are filled in at run time; `<host>` is the new web host name, without scheme or path.

The web client's build holds no origin: its `config.json` and the `connect-src` of its content security policy are written per environment by `deploy_web.py` from the `API_BASE_URL` output, and its redirect URI follows the page's origin, so neither change needs a web build of its own (`10-web-client.md` sections 2.3 and 4.1).

## A. Add a web custom domain

1. Check the plan's limit: `webCustomDomains` in `infra/env/<env>.json` holds at most 2 host names on `Free` and 5 on `Standard` (`web.sku`); preflight check 14 refuses more with `custom_domains_exceed_plan`.

2. The DNS owner creates the CNAME record `<host>` → the Static Web App's default host name, which the `cname-delegation` validation needs:

   ```bash
   az staticwebapp show --name <DOCCHAT_STATIC_WEB_APP_NAME> --resource-group rg-docchat-<env> --query defaultHostname -o tsv
   ```

   Expected: the default host name (`<name>.azurestaticapps.net`), the CNAME record's target.

3. A pull request adds `<host>` to `webCustomDomains` in `infra/env/<env>.json`. Expected: its `preflight-whatif` summary shows the Static Web Apps custom domain, the web registration's new redirect URI `https://<host>/` and the API's new platform CORS origin. When it is the first entry, `WEB_BASE_URL` becomes `https://<host>`, so the deployed tests and the sign-in check run on that origin.

4. Deploy with R01, then:

   ```bash
   az staticwebapp hostname show --name <DOCCHAT_STATIC_WEB_APP_NAME> --resource-group rg-docchat-<env> --hostname <host> --query status -o tsv
   ```

   Expected: `Ready`.

5. Verify CORS:

   ```bash
   curl -s -o /dev/null -D - -X OPTIONS "<API_BASE_URL>/v1/me" -H "Origin: https://<host>" -H "Access-Control-Request-Method: GET" -H "Access-Control-Request-Headers: authorization"
   ```

   Expected: `Access-Control-Allow-Origin: https://<host>`. Then sign in at `https://<host>/` with the release sign-in check (`10-web-client.md` section 13.4).

6. A preflight that the platform answers without the origin after a correct deploy is a platform fault: revert the pull request and take the case to the product owner, who decides through a new ADR.

## B. Change the API origin

1. The API keeps its default host name (`83-infrastructure.md` section 2.22); a custom API domain or a new API site first needs its own ADR, because the template declares no API host-name binding or certificate.

2. Deploy the API on the new origin while the old one keeps working, and check its health:

   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" <new API origin>/healthz
   ```

   Expected: `200`.

3. Change the `API_BASE_URL` output by pull request, then redeploy the web client with R01, so `config.json` and the content security policy change together. The web step's post-upload check fails with `web_config_check_failed` unless `/config.json` holds the new `apiBaseUrl` and the policy's `connect-src` names the new origin. Check it from the workstation:

   ```bash
   curl -s https://<web host>/config.json
   curl -s -o /dev/null -D - https://<web host>/ | grep -i '^content-security-policy'
   ```

   Expected: `apiBaseUrl` is the new origin, and `connect-src` lists it.

4. Run the deployed `smoke` suite (`dev`) or `prod_smoke` (`prod`), then the release sign-in check (`10-web-client.md` section 13.4):

   ```bash
   gh workflow run deployed-tests.yaml -f environment=dev -f suite=smoke
   gh workflow run deployed-tests.yaml -f environment=prod -f suite=prod_smoke \
     -f api_base_url=<API_BASE_URL> -f web_base_url=<WEB_BASE_URL> -f web_client_id=<WEB_CLIENT_ID> -f api_scope=<API_SCOPE>
   ```

   The `prod` values are the public outputs of `azd env get-values -e prod` (`84-testing.md` section 6.3). Expected: the run passes, its `web-deployed` job included.

5. Retire the old origin only after step 4 passes.

## Verification

Part A steps 4 and 5; part B steps 3 and 4.

## Undo

A revert pull request deployed with R01 (part A), or `API_BASE_URL` set back while the old origin still runs, then the web client redeployed with R01 (part B).

## Audit record

The pull requests, the deploy runs, and Entra's audit log for the redirect URI change (`docchat-alert-app-registrations` fires; acknowledge it with the ticket).
