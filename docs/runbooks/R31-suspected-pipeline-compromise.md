# R31 — Suspected pipeline compromise

**Performer:** the bootstrap operator with a member of `docchat-security`.

**Trigger:** a security alert that no pipeline run or ticket explains; a credential in a workflow log; an unreviewed change on `main`; `image_attestation_invalid`.

**Prerequisites:** the operator workstation of `86-operations.md` section 1.5, with the bootstrap operator's prerequisites of R21.

Placeholders in angle brackets are filled in at run time. KQL steps run through the Logs query API (`86-operations.md` section 1.5), with the KQL and the timespan the step names, against `log-docchat-security`, whose workspace ID is its customer ID:

```bash
az monitor log-analytics workspace show --resource-group rg-docchat-identity --workspace-name log-docchat-security --query customerId -o tsv
az rest --method post \
  --url "https://api.loganalytics.io/v1/workspaces/<log-docchat-security customer ID>/query" \
  --resource "https://api.loganalytics.io" \
  --body "{\"query\": \"<KQL with single-quoted strings>\", \"timespan\": \"<ISO 8601 duration>\"}" \
  --query "tables[0].rows"
```

## Steps

1. Contain: stop every workflow, then delete the federated credentials of each implicated identity (`github-environment-<env>` of the deploy identities, `github-pull-request` of `id-docchat-pr`, `github-environment-dev-tests` and `github-environment-load-tests` of `id-docchat-citest`):

   ```bash
   gh api --method PUT /repos/<organization>/<repository>/actions/permissions -F enabled=false
   az identity federated-credential delete --name <credential> --identity-name <identity> --resource-group rg-docchat-identity --yes
   ```

2. Investigate in `log-docchat-security` with this query (timespan `P30D`); then the security alerts' history; Entra `AuditLogs` for registration and group changes; the GitHub organization's audit log for the repository:

   ```kusto
   AzureActivity | where Caller in ('<pipeline identity principal IDs>') | summarize n = count() by OperationNameValue, ResourceGroup, bin(TimeGenerated, 1h)
   ```

3. Remove what doesn't belong:
   - Cosmos DB data-plane assignments the templates didn't create:

     ```bash
     az cosmosdb sql role assignment list --account-name <account> --resource-group rg-docchat-<env> -o table
     az cosmosdb sql role assignment delete --account-name <account> --resource-group rg-docchat-<env> --role-assignment-id <ID> --yes
     ```

   - Role assignments outside `83-infrastructure.md` section 6:

     ```bash
     az role assignment list --resource-group rg-docchat-<env> -o table
     az role assignment delete --ids <ID>
     ```

   - Unexpected federated credentials, registration credentials and redirect URIs.
   - By default (`DOCCHAT_CI_PRIVATE_NETWORKING` `false`), where `id-docchat-citest` can change the test vault's network rules: addresses on the test vault outside the shared allowlist, removed with R26 steps 1, 3 and 4 for the vault while step 1 keeps every workflow stopped.
   - In `prod`, an item under one of the six locked resources is deleted only after removing that one lock; then `lock_prod.py` runs in the same session and exits 0:

     ```bash
     az lock list --resource-group rg-docchat-prod
     az lock delete --ids <lock ID>
     uv run --frozen python scripts/bootstrap/lock_prod.py
     ```

4. Rotate what the pipeline could reach: the Static Web Apps token (R14 step 1 now, its redeploy in step 6), the test-user passwords (R08), and the host key under fallback 1 (R25).

5. Restore the pipeline: the bootstrap's `identity` and `assignments` steps, then R23 for `dev` and `prod`, then re-enable Actions and apply R16 step 5's selected-actions list:

   ```bash
   uv run --frozen python scripts/bootstrap/bootstrap.py --step identity
   uv run --frozen python scripts/bootstrap/bootstrap.py --step assignments
   gh api --method PUT /repos/<organization>/<repository>/actions/permissions -F enabled=true -f allowed_actions=selected
   ```

6. Redeploy a known-good commit to each environment in full mode, so the templates restore every declared setting; approved in `prod`:

   ```bash
   gh workflow run deploy.yaml -f environment=<env> -f image_commit=<known-good commit> -f rollback=false
   ```

7. Finish with R07 steps 9 and 10.

## Verification

Step 6's runs succeed in every environment; the post-incident review of step 7 (R07 step 10) records the outcome.

## Undo

None; the containment is lifted by steps 5 and 6.

## Audit record

The incident ticket and the records of step 2.
