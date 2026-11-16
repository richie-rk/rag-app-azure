# R30 — Decommission an environment

**Performer:** the bootstrap operator (Owner) for every Azure step; the tenant administrator (Cloud Application Administrator or Application Administrator, `86-operations.md` section 1.1) for step 2; in `prod`, a Security Administrator for step 6.

**Trigger:** the product owner's decision, recorded in the ticket; for `prod` also the organisation's retention decision for its conversations and documents, with R06 exports first when required.

**Prerequisites:** the operator workstation of `86-operations.md` section 1.5, with `.azure/<env>/.env` refreshed (`azd env refresh -e <env> --no-prompt`) before anything is deleted, because steps 2 and 4 read its values; `gh` signed in as a repository administrator for step 5. This runbook is rehearsed as a walk-through (`86-operations.md` section 4.2): run every read-only command and stop before each step that changes anything.

Placeholders in angle brackets are filled in at run time; a value from `.azure/<env>/.env` is written by its output name.

## Steps

1. `prod` only: remove the six locks and the delete-deny assignment, which would otherwise refuse the deletion:

   ```bash
   az lock list --resource-group rg-docchat-prod --query "[].id" -o tsv
   az lock delete --ids <lock ID>
   az policy assignment delete --name docchat-deny-store-deletion --resource-group rg-docchat-prod
   ```

   `az lock delete` once for each listed lock. `docchat-alert-governance` fires for each; acknowledge with the ticket.

2. The tenant administrator deletes the registrations and their service principals permanently. For `web`, `api` and `test` (`prod` has no `test`), read the application and its service principal:

   ```bash
   az rest --method GET --url "https://graph.microsoft.com/v1.0/applications(uniqueName='docchat-<env>-<DOCCHAT_RESOURCE_TOKEN>-<web|api|test>')" --query "{id: id, appId: appId}"
   az rest --method GET --url "https://graph.microsoft.com/v1.0/servicePrincipals(appId='<appId>')" --query id -o tsv
   ```

   then delete the service principal, then the application, each also from the deleted items:

   ```bash
   az rest --method DELETE --url https://graph.microsoft.com/v1.0/servicePrincipals/<service principal ID>
   az rest --method DELETE --url https://graph.microsoft.com/v1.0/directory/deletedItems/<service principal ID>
   az rest --method DELETE --url https://graph.microsoft.com/v1.0/applications/<object ID>
   az rest --method DELETE --url https://graph.microsoft.com/v1.0/directory/deletedItems/<object ID>
   ```

   A `404` on a service principal means it is gone.

3. Delete the resource group (in `prod` only after step 1; otherwise it answers `403`):

   ```bash
   az group delete --name rg-docchat-<env> --yes
   ```

4. Purge the soft-deleted accounts that would block re-creation: each Cognitive Services account and, outside `prod`, the Key Vault; `prod`'s purge protection keeps its vault for 90 days:

   ```bash
   az cognitiveservices account purge --name <account> --resource-group rg-docchat-<env> --location <region>
   az keyvault purge --name <DOCCHAT_KEY_VAULT_NAME>
   ```

   The accounts are `<DOCCHAT_OPENAI_ACCOUNT_NAME>` (not in `load`), `cs-docchat-<env>-<DOCCHAT_RESOURCE_TOKEN>` and `di-docchat-<env>-<DOCCHAT_RESOURCE_TOKEN>`.

5. Remove the pipeline's access: the deploy identity's federated credential and, for `dev` and `load`, the CI test identity's; the deploy identity's subscription-scope assignments; the GitHub environments:

   ```bash
   az identity federated-credential delete --name github-environment-<env> --identity-name id-docchat-deploy-<env> --resource-group rg-docchat-identity --yes
   az identity federated-credential delete --name github-environment-<env>-tests --identity-name id-docchat-citest --resource-group rg-docchat-identity --yes
   az role assignment list --assignee <deploy identity principal ID> --scope /subscriptions/<subscription ID> --query "[].id" -o tsv
   az role assignment delete --ids <ID>
   gh api --method DELETE /repos/<organization>/<repository>/environments/<env>
   gh api --method DELETE /repos/<organization>/<repository>/environments/<env>-tests
   ```

   The second command and the last only for `dev` and `load`; `az role assignment delete` once for each listed ID.

6. `prod` only: the Security Administrator deletes `docchat-prod-audit`:

   ```bash
   az rest --method DELETE --url "https://management.azure.com/providers/microsoft.aadiam/diagnosticSettings/docchat-prod-audit?api-version=2017-04-01"
   ```

7. Verify:

   ```bash
   az group exists --name rg-docchat-<env>
   ```

   Expected: `false`; step 2's reads answer `404`. Record the decommission in the ticket.

## Verification

Step 7.

## Undo

R21's steps for the environment, then R22.

## Audit record

The Activity Log, Entra's audit log and the GitHub audit log, all kept 365 days in `log-docchat-security` or by GitHub.
