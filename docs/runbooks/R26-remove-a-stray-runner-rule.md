# R26 — Remove a stray runner rule

**Performer:** the bootstrap operator, from a machine in the allowlist of the resource concerned (the environment's allowlist for SQL, the shared allowlist for the registry and the test vault).

**Trigger:** a job failure with `rule_close_failed`; step 1 finding a rule during any review, among them a registry or test-vault address outside the shared allowlist, which no job removes.

**Prerequisites:** the operator workstation of `86-operations.md` section 1.5, and in the shell:

- For the registry and the test vault, `DOCCHAT_SHARED_OPERATOR_IP_ALLOWLIST`, holding the shared allowlist that the bootstrap's `shared` step last applied (empty when that list is empty). No CI job holds the shared allowlist (`83-infrastructure.md` section 10.4), so no job removes a registry or test-vault address other than its own runner's: this runbook is the only sweep of both. `firewall.py` reads an unset variable as an unknown list, never an empty one: its registry and vault sweeps then remove nothing, print a warning naming this runbook and exit 0.

  ```bash
  export DOCCHAT_SHARED_OPERATOR_IP_ALLOWLIST="<comma-separated addresses, or empty>"
  ```

- For the test vault, `DOCCHAT_TEST_VAULT_URI`, which names the vault `firewall.py` works on:

  ```bash
  export DOCCHAT_TEST_VAULT_URI=$(gh variable get DOCCHAT_TEST_VAULT_URI --env dev-tests)
  ```

Placeholders in angle brackets are filled in at run time; `<registry>` is `crdocchat<sharedToken>`. Addresses are compared without a trailing `/32`.

Runner rules and who holds them (`83-infrastructure.md` section 4.6): SQL takes `ci-runner-*` rules from the `deploy` job and, in `dev`, `deployed-tests.yaml`'s `infra-checks`, and in `load` from `load-test.yaml`; the registry takes one address from `deploy.yaml`'s `publish` and `resolve` and `nightly.yaml`'s `trivy-rescan`; by default the test vault takes one address per read window of the `dev-tests` and `load-tests` jobs of `deploy.yaml` (through `deployed-tests.yaml`), `deployed-tests.yaml`, `load-test.yaml` and `nightly.yaml`. Only SQL jobs sweep, because their concurrency groups leave no other job's live rule at that moment. No job sweeps the registry or the vault: their rules are bare addresses and no job holds the shared allowlist, so a job can't tell a crashed runner's address from an operator's. Each job's `open` and `close`, the always-run `close` included, touch only its own runner's address. A runner that died before its `close` leaves its address on the registry or the vault until step 3 removes it, or until the next run of the bootstrap's `shared` step rewrites both IP rule lists. Under the private-networking option the vault takes no runner rule at all.

## Steps

1. Find stray rules.
   - SQL: every name printed is stray.

     ```bash
     az sql server firewall-rule list --resource-group rg-docchat-<env> --server <DOCCHAT_SQL_SERVER_NAME> --query "[?starts_with(name,'ci-runner-')].name" -o tsv
     ```

   - Registry: any address outside the shared allowlist is stray.

     ```bash
     az acr network-rule list --name <registry> --query "ipRules[].ipAddressOrRange" -o tsv
     ```

   - Test vault: first read the CI path, then the vault's rules.

     ```bash
     gh variable get DOCCHAT_CI_PRIVATE_NETWORKING
     az keyvault network-rule list --name kv-docchat-tst-<sharedToken> --query "ipRules[].value" -o tsv
     ```

     By default (`false`), an address outside the shared allowlist while no test job runs (step 2) is a read window its job didn't remove: steps 3 to 5 remove it and find its caller. Under the private-networking option (`true`) the vault never takes a runner rule: an address outside the shared allowlist is a change no pipeline makes → R31.

2. Confirm that no job holding a rule on that target runs, because a sweep also removes a live rule:

   ```bash
   gh api "/repos/<organization>/<repository>/actions/runs?status=in_progress&per_page=100" --jq '.workflow_runs[].path | split("@")[0]'
   ```

   Expected, for the test vault: none of `.github/workflows/deploy.yaml`, `.github/workflows/deployed-tests.yaml`, `.github/workflows/load-test.yaml` and `.github/workflows/nightly.yaml` (which runs at 02:30 UTC); for SQL: none of `deploy.yaml`, `deployed-tests.yaml` and, in `load`, `load-test.yaml`; for the registry: none of `deploy.yaml` and `nightly.yaml`. Otherwise wait until they finish and repeat step 1: a finishing job removes its own rule.

3. Sweep the target. `--env` names the SQL server's environment; for the registry and the test vault, which are shared, use `dev` (the vault target accepts `dev` or `load` and works on the vault of `DOCCHAT_TEST_VAULT_URI` either way):

   ```bash
   uv run --frozen python scripts/deploy/firewall.py sweep --target <sql|registry|vault> --env <env>
   ```

   Expected: exit 0, with each removed rule printed. For SQL it removes every `ci-runner-*` rule; for the registry and the vault, every address outside the shared allowlist. A warning that `DOCCHAT_SHARED_OPERATOR_IP_ALLOWLIST` is not set means nothing was removed: export it (prerequisites) and run the step again. Exit 4 with `rule_close_failed` (the vault target reads each removal back) means an address stayed listed: run the step again. On SQL, without this step the next job that uses SQL removes the rule in its own sweep; a registry or test-vault leftover stays until this step, or until the next run of the bootstrap's `shared` step rewrites both IP rule lists.

4. Verify by repeating step 1: nothing stray remains.

5. Find who added each rule, in `rg-docchat-<env>` for SQL and in `rg-docchat-shared` for the registry and the test vault:

   ```bash
   az monitor activity-log list --resource-group <rg-docchat-<env> or rg-docchat-shared> --offset 7d --query "[?operationName.value=='Microsoft.Sql/servers/firewallRules/write' || operationName.value=='Microsoft.ContainerRegistry/registries/write' || operationName.value=='Microsoft.KeyVault/vaults/write'].{caller: caller, time: eventTimestamp}" -o table
   ```

   SQL or registry: a caller other than a pipeline identity → R31. Test vault: a caller other than `id-docchat-citest` or the bootstrap operator (whose `shared` step writes the vault's rules) → R31.

## Verification

Step 4.

## Undo

None needed.

## Audit record

The rule deletes in the Activity Log.
