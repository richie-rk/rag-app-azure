# R01 — Deploy

**Performer:** automatic for `dev`; a release approver for `prod`; a maintainer follows the run.

**Trigger:** a merge to `main` (`83-infrastructure.md` section 10.6).

**Prerequisites:** the operator workstation of `86-operations.md` section 1.5. Whoever runs step 5's query is a member of `sg-docchat-operators` (`86-operations.md` section 1.1, "Who runs Azure reads").

Placeholders in angle brackets are filled in at run time. KQL steps run through the Logs query API (`86-operations.md` section 1.5), with the KQL and the timespan the step names:

```bash
az rest --method post \
  --url "https://api.loganalytics.io/v1/workspaces/<DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID>/query" \
  --resource "https://api.loganalytics.io" \
  --body "{\"query\": \"<KQL with single-quoted strings>\", \"timespan\": \"<ISO 8601 duration>\"}" \
  --query "tables[0].rows"
```

## Steps

1. Follow the run:

   ```bash
   gh run watch $(gh run list --workflow deploy.yaml --branch main --limit 1 --json databaseId -q '.[0].databaseId')
   ```

   Expected: `build`, `publish`, `deploy-dev` and `test-dev` succeed, and `deploy-prod` waits for approval.

2. Confirm the `dev` tests:

   ```bash
   gh run view <run ID> --json jobs -q '.jobs[] | select(.name | startswith("test-dev")) | .conclusion'
   ```

   Expected: `success`. A failure leaves `dev` on the new release and `prod` untouched: fix forward, or roll `dev` back with R02.

3. Complete the release checklist (below) in the release ticket.

4. Approve `prod` with the ticket as comment (`86-operations.md` section 1.5, "Approving a waiting deployment"):

   ```bash
   gh api /repos/<owner>/<repo>/environments/prod --jq .id
   gh api --method POST /repos/<owner>/<repo>/actions/runs/<run ID>/pending_deployments \
     -F "environment_ids[]=<environment ID>" -f state=approved -f comment="<ticket>"
   ```

   Expected: `deploy-prod` runs; its `postprovision` hook prints both capacity shares to the job summary. `chat_capacity_exceeded`: raise the capacity in `infra/env/prod.json` by pull request (preflight check 7 confirms the quota) or lower the per-user limit with R03, then deploy once more.

5. After `test-prod`: its `prod_smoke` suite passed, and this query against the `prod` workspace (timespan `PT15M`) lists only the new commit:

   ```kusto
   AppRequests | where TimeGenerated > ago(15m) | summarize requests = sum(ItemCount) by AppVersion
   ```

   Then:

   ```bash
   git fetch --tags
   ```

   Expected: `deploy-prod-<yyyymmdd-hhmm>` on the new commit.

6. Watch the `availability` and `ttft` workbooks for 30 minutes. A Sev1 alert starts R07, and the approver decides within 30 minutes between fixing forward and R02.

7. When the release changes the web client, run the release sign-in check in `prod` (`10-web-client.md` section 13.4) and record it in the checklist.

8. Record the run ID, the commit, the approver and the checklist in the ticket.

## Failures inside the run

| Code | Next step |
|---|---|
| `health_check_failed`, `ingestion_start_failed` | R02 |
| `prod_test_client_present`, `prod_vault_not_empty` | R07 (the release stays deployed) |
| `app_settings_mismatch`, `grants_mismatch` | R07 (the release stays deployed; `83-infrastructure.md` section 10.7 step 10) |
| `swa_deploy_failed`, `web_config_check_failed` | the previous web build still serves; read the JSON line in the job summary, fix its cause, then `gh run rerun <run ID> --failed` (`83-infrastructure.md` section 10.10) |
| `image_attestation_invalid` | R31 |
| `migration_failed`, `sql_principal_failed` | fix forward (nothing was deployed) |

The first deployment of a new environment follows R22.

## The release checklist

The approver completes it in the release ticket before approving `prod`, and links the ticket in the approval comment. "The web client changed" means that this command lists a file:

```bash
git diff --stat <current release commit of prod> <commit> -- apps/web
```

`<current release commit of prod>` is the commit `prod` serves: `az webapp show --name <DOCCHAT_API_APP_NAME> --resource-group rg-docchat-prod --query "tags.\"docchat-release-sha\"" -o tsv`.

| # | Item | Evidence | Blocks the release when |
|---|---|---|---|
| 1 | `dev` deployed and `test-dev` passed | the run ID | it failed |
| 2 | Load evidence for a change to a plan SKU, the Gunicorn worker count, Durable concurrency, the Search tier or replicas, or a new model call on the chat path | the R11 run ID and its verdict table | a verdict failed or is missing |
| 3 | The web client changed: the release accessibility check (`10-web-client.md` section 11.6) passed | its results | any check failed |
| 4 | The web client changed: the release sign-in check in `dev` (`10-web-client.md` section 13.4) passed | its result | it failed |
| 5 | A new model or a price change: `pricing.model_rates` holds a rate for every deployed model in `prod` | `config show --key pricing.model_rates` output | a deployed model has no rate |
| 6 | No app setting removed or renamed, and no value outside the range the running release accepts, before the code that stops needing it is in `prod` | the reviewer's note on `infra/config/app-settings.json`, `infra/modules/compute/app-settings.bicep` and `infra/env/prod.json` | such a change ships with the code |
| 7 | After the deploy: `prod_smoke` passed; the release sign-in check in `prod` passed when item 3 applies | the run and the check | it failed (R02 or a fix forward) |

The completed checklist in the ticket that the approval comment links is the release checklist record (T-555). Item 3 is the release accessibility check (T-413) and items 4 and 7 the release sign-in check in `dev` and `prod` (T-414). The ticket holds, one line per item, with IDs and references only:

```text
Release <commit> to prod: release checklist (86-operations.md section 3.3.1)
Web client changed: yes | no (git diff --stat <current release commit of prod> <commit> -- apps/web)
1. dev deployed and test-dev passed: deploy.yaml run <run ID>
2. Load evidence: R11 run <run ID>, verdict table attached | not needed: <which change of item 2 is absent>
3. Release accessibility check (T-413): <results of 10-web-client.md section 11.6> | web client unchanged
4. Release sign-in check in dev (T-414): <result of 10-web-client.md section 13.4> | web client unchanged
5. pricing.model_rates: config show correlation ID <ID>, a rate for every deployed model | no new model or price change
6. App settings order: <reviewer's note on app-settings.json, app-settings.bicep and prod.json>
7. After the deploy: prod_smoke run <run ID>; release sign-in check in prod (T-414): <result> | web client unchanged
Approver: <GitHub handle>; approval of deploy.yaml run <run ID>
```

## Verification

Steps 2 and 5, and checklist item 7.

## Undo

R02.

## Audit record

The workflow run with its approver, the `deploy-prod-*` tag, and the deployment in the Activity Log.
