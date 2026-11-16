# R14 — Static Web Apps deployment token reset

**Performer:** the bootstrap operator for the reset (operators hold only Reader, which lacks `Microsoft.Web/staticSites/resetapikey/action`), then a maintainer for the redeploy; in `prod` a release approver approves the run.

**Trigger:** every 90 days, and at once on suspected exposure (`83-infrastructure.md` section 10.10). The deployment token is the one key-based credential in delivery: it is never stored as a GitHub secret, written to a file or logged, and `deploy_web.py` reads it afresh at every web deploy.

**Prerequisites:** the operator workstation of `86-operations.md` section 1.5, with `.azure/<env>/.env` refreshed (`azd env refresh -e <env> --no-prompt`); `gh` signed in with the right to dispatch workflows.

Placeholders in angle brackets are filled in at run time. `<current release commit>` is the commit the environment serves:

```bash
az webapp show --name <DOCCHAT_API_APP_NAME> --resource-group rg-docchat-<env> --query "tags.\"docchat-release-sha\"" -o tsv
```

## Steps

1. The bootstrap operator resets the token:

   ```bash
   az login --tenant <tenant ID>
   az staticwebapp secrets reset-api-key --name <DOCCHAT_STATIC_WEB_APP_NAME> --resource-group rg-docchat-<env> --output none
   ```

   Expected: exit 0 and no output, so the new token never reaches the terminal. The old token stops working at once; the site keeps serving its content, because Static Web Apps keeps the last deployment until an upload succeeds.

2. A maintainer redeploys the current release, whose web step reads the new token:

   ```bash
   gh workflow run deploy.yaml -f environment=<env> -f image_commit=<current release commit> -f rollback=true
   gh run watch $(gh run list --workflow deploy.yaml --limit 1 --json databaseId -q '.[0].databaseId')
   ```

   The run deploys the same images (`resolve`), the web client built at that commit (`resolve-web`) and no provision (`images-only`); its `deploy_web.py` step reads the token with the deploy identity's `Microsoft.Web/staticSites/listSecrets/action`, masks it and passes it only to the pinned Static Web Apps CLI (`83-infrastructure.md` sections 10.10 and 10.12). `prod` waits for its approval (`86-operations.md` section 1.5, "Approving a waiting deployment").

   Expected: the run succeeds. `swa_deploy_failed` or `web_config_check_failed`: the previous web build still serves; read the JSON line in the job summary, then re-run the failed job (`gh run rerun <run ID> --failed`).

3. Verify:

   ```bash
   gh run view <run ID> --log | grep '"web": "deployed"'
   curl -s -o /dev/null -w "%{http_code}\n" https://$(az staticwebapp show --name <DOCCHAT_STATIC_WEB_APP_NAME> --resource-group rg-docchat-<env> --query defaultHostname -o tsv)/config.json
   ```

   Expected: the web step's success line (`deploy_web.py` exit 0, with the deploy binary's build ID and SHA-256 in the job summary), and `200`.

4. Record the reset date and the next due date (90 days later) in the ticket that tracks the token.

## Verification

Step 3: the web step succeeded with the new token, and `/config.json` answers `200`.

## Undo

None; the old token can't be restored.

## Audit record

The reset (`Microsoft.Web/staticSites/resetapikey/action`) under the bootstrap operator and every later token read (`Microsoft.Web/staticSites/listSecrets/action`) under the deploy identity, both Administrative Activity Log events kept 365 days in `log-docchat-security`; the workflow run with its approver; the ticket.
