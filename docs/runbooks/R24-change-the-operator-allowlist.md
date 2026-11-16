# R24 — Change the operator allowlist

**Performer:** an operator with admin rights on the repository's GitHub environments for an environment list; the bootstrap operator for the shared list.

**Trigger:** an operator's address changes, joins or leaves; the ops mailboxes change.

**Prerequisites:** the operator workstation of `86-operations.md` section 1.5; for the shared list, the bootstrap operator's prerequisites of R21.

Placeholders in angle brackets are filled in at run time. `<current release commit>` is the commit the environment serves:

```bash
az webapp show --name <DOCCHAT_API_APP_NAME> --resource-group rg-docchat-<env> --query "tags.\"docchat-release-sha\"" -o tsv
```

## Steps

1. Open a ticket with the change: single public IPv4 addresses, at most 20 per list.

2. Environment list; for `dev` also the repository variable, which the pull-request what-if reads:

   ```bash
   gh variable set DOCCHAT_OPERATOR_IP_ALLOWLIST --env <env> --body "<comma-separated addresses>"
   gh variable set DOCCHAT_DEV_OPERATOR_IP_ALLOWLIST --body "<the same list>"
   ```

   Ops mailboxes:

   ```bash
   gh variable set DOCCHAT_OPS_ALERT_EMAILS --env <env> --body "<comma-separated shared mailboxes>"
   ```

3. Apply it with a full deploy, approved in `prod`:

   ```bash
   gh workflow run deploy.yaml -f environment=<env> -f image_commit=<current release commit> -f rollback=false
   ```

   The preflight refuses a malformed list with `allowlist_invalid` (exit 2) before anything changes.

4. Shared list (registry and test vault), the bootstrap operator. The `shared` step redeploys the test vault's `ipRules` from the list and so removes a live test-job runner rule (`83-infrastructure.md` section 11.2; ADR 0122), so first confirm that no test job is reading the vault:

   ```bash
   gh api "/repos/<organization>/<repository>/actions/runs?status=in_progress&per_page=100" --jq '.workflow_runs[].path | split("@")[0]'
   ```

   Expected: none of `.github/workflows/deploy.yaml`, `.github/workflows/deployed-tests.yaml`, `.github/workflows/load-test.yaml` and `.github/workflows/nightly.yaml` (which runs at 02:30 UTC). Then:

   ```bash
   export DOCCHAT_SHARED_OPERATOR_IP_ALLOWLIST="<comma-separated addresses, or empty>"
   uv run --frozen python scripts/bootstrap/bootstrap.py --step shared
   ```

   Expected: the step reports success.

5. Verify:

   ```bash
   az sql server firewall-rule list --resource-group rg-docchat-<env> --server <DOCCHAT_SQL_SERVER_NAME> --query "[].name" -o tsv
   az cosmosdb show --name <DOCCHAT_COSMOS_ACCOUNT_NAME> --resource-group rg-docchat-<env> --query "ipRules[].ipAddressOrRange" -o tsv
   ```

   Expected: one `operator-<n>` per address and no `ci-runner-*` rule; Cosmos DB lists the addresses. For the shared list:

   ```bash
   az keyvault network-rule list --name kv-docchat-tst-<sharedToken> --query "ipRules[].value" -o tsv
   az keyvault show --name kv-docchat-tst-<sharedToken> --query "{publicNetworkAccess: properties.publicNetworkAccess, defaultAction: properties.networkAcls.defaultAction}" -o tsv
   ```

   Expected: the rule list holds exactly the shared list's addresses (none while it is empty). By default (`DOCCHAT_CI_PRIVATE_NETWORKING` `false`) the vault returns `Enabled` and `Deny` whatever the list holds, so only those addresses and a test job's read window reach it; under the private-networking option it returns `Disabled` while the list is empty.

6. Record the change in the ticket.

## Verification

Step 5.

## Undo

The earlier list, set and applied the same way.

## Audit record

The variable change in the GitHub audit log; the firewall and network-rule writes in the Activity Log (registry writes raise `docchat-alert-registry-writes`).
