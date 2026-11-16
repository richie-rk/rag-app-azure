# R02 — Rollback

**Performer:** a release approver.

**Trigger:** a release that caused an incident (R07), or a failed `test-dev` or `test-prod`.

**Prerequisites:** the operator workstation of `86-operations.md` section 1.5, with membership of `sg-docchat-operators` for the Azure reads (`86-operations.md` section 1.1).

Placeholders in angle brackets are filled in at run time. `<current release commit>` is the commit the environment serves:

```bash
az webapp show --name <DOCCHAT_API_APP_NAME> --resource-group rg-docchat-<env> --query "tags.\"docchat-release-sha\"" -o tsv
```

KQL steps run through the Logs query API (`86-operations.md` section 1.5), with the KQL and the timespan the step names:

```bash
az rest --method post \
  --url "https://api.loganalytics.io/v1/workspaces/<DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID>/query" \
  --resource "https://api.loganalytics.io" \
  --body "{\"query\": \"<KQL with single-quoted strings>\", \"timespan\": \"<ISO 8601 duration>\"}" \
  --query "tables[0].rows"
```

## Steps

1. List good releases:

   ```bash
   gh run list --workflow deploy.yaml --status success --limit 10 --json databaseId,headSha,createdAt
   ```

   For `prod`, the commits of the `deploy-prod-*` tags:

   ```bash
   git fetch --tags
   git for-each-ref --sort=-creatordate --format "%(refname:short) %(*objectname)" "refs/tags/deploy-prod-*"
   ```

2. Check the schema:

   ```bash
   git diff --name-only --diff-filter=A <good commit> <current release commit> -- db/migrations/versions | xargs -r grep -l "Contract: completes"
   ```

   Expected: nothing printed. A contract revision in that range means fixing forward, because the database is never downgraded.

3. Check the settings:

   ```bash
   git diff <good commit> <current release commit> -- infra/config/app-settings.json infra/modules/compute/app-settings.bicep infra/env/<env>.json
   ```

   Expected: the diff removes or renames no app setting and sets no value outside the range the good commit's settings models accept; otherwise the earlier code stops at start-up, so fix forward. Ops-config rows need no check: an area ignores keys it doesn't know.

4. Dispatch:

   ```bash
   gh workflow run deploy.yaml -f environment=<dev|prod> -f image_commit=<good commit> -f rollback=true
   ```

   The `resolve` job finds the images by commit tag and verifies their attestations, `resolve-web` builds the web client at that commit, and `deploy-environment.yaml` runs `images-only`: no provision and no migration (`83-infrastructure.md` section 10.12). In `prod`, approve it (`86-operations.md` section 1.5); the release-ordering check is skipped:

   ```bash
   gh api /repos/<owner>/<repo>/environments/prod --jq .id
   gh api --method POST /repos/<owner>/<repo>/actions/runs/<run ID>/pending_deployments \
     -F "environment_ids[]=<environment ID>" -f state=approved -f comment="<ticket>"
   ```

5. Verify: the run's `test-dev` or `test-prod` passes, and this query (timespan `PT15M`) shows the good commit:

   ```kusto
   AppRequests | where TimeGenerated > ago(15m) | summarize sum(ItemCount) by AppVersion
   ```

6. Record the rollback in the ticket and open an issue for the fix forward.

A rollback reaches any commit whose images the registry still holds; R28 keeps every image a `deploy-prod-*` tag references.

## Verification

Step 5.

## Undo

R01 with the fix.

## Audit record

The workflow run and its approval.
