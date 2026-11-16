# R34 — Repair the CI runner group

**Performer:** the bootstrap operator (Owner, and owner of the GitHub organization).

**Trigger:** under the private-networking option only (`ciPrivateNetworking` `true`, ADR 0122): a `dev-tests` or `load-tests` job waits more than 15 minutes for a runner, or its token step can't reach the test vault (`test_vault_unreachable`; `83-infrastructure.md` section 10.16). By default (`false`) the test jobs run on standard GitHub-hosted runners, and no runner group, CI network or private endpoint exists to repair.

**Prerequisites:** the bootstrap operator's prerequisites of R21, with `DOCCHAT_SHARED_OPERATOR_IP_ALLOWLIST` exported in the shell holding the current shared allowlist, because step 4's `shared` step applies it to the registry and the test vault.

Placeholders in angle brackets are filled in at run time.

## Steps

1. Confirm the CI path:

   ```bash
   gh variable get DOCCHAT_CI_PRIVATE_NETWORKING
   ```

   Expected: `true`. With `false` this runbook doesn't apply: a `test_vault_unreachable` comes from the job's read window, and the `firewall.py` line in the job log names its code (`runner_ip_unresolved`, `runner_ip_mismatch` or `rule_not_effective`, each retried with `gh run rerun <run ID> --failed`; `83-infrastructure.md` section 10.18); an address left on the vault goes to R26.

2. Read the runner:

   ```bash
   gh api /orgs/<organization>/actions/hosted-runners --jq '.runners[] | select(.name == "docchat-ci-private") | {status, maximum_runners, runner_group_id}'
   ```

   Expected: `Ready` and `maximum_runners` 10; a missing runner or another status is the fault.

3. Check the vault's private path:

   ```bash
   az network private-endpoint show --name pe-kv-docchat-tst-<sharedToken>-vault --resource-group rg-docchat-shared --query provisioningState -o tsv
   az network private-dns link vnet list --resource-group rg-docchat-shared --zone-name privatelink.vaultcore.azure.net --query "[].virtualNetwork.id" -o tsv
   ```

   Expected: `Succeeded`, and the zone links name `vnet-docchat-ci`.

4. Repair: the `shared` step, then the `ci-runner` step; they recreate a missing endpoint, zone link, network configuration, runner group or runner and read each back:

   ```bash
   uv run --frozen python scripts/bootstrap/bootstrap.py --step shared
   uv run --frozen python scripts/bootstrap/bootstrap.py --step ci-runner
   ```

   Expected: both report success. A stop code names a cause to fix first (`83-infrastructure.md` section 10.18):

   | Code | Cause and fix |
   |---|---|
   | `ci_runner_mismatch` | the network configuration, runner group or runner read back doesn't match the design: run the `ci-runner` step once more |
   | `ci_private_networking_unavailable` | the organization can't create the 2-core Ubuntu 24.04 larger runner that private networking needs (its plan isn't GitHub Team or Enterprise Cloud): set `ciPrivateNetworking` to `false` in `bootstrap.config.json` by a reviewed pull request and re-run the `shared`, `assignments`, `github` and `ci-runner` steps, so the test jobs use standard runners and the vault's transient runner rule (ADR 0122 item 2) |
   | `ci_region_unsupported` | `ciNetworkLocation` isn't one of `githubPrivateNetworkingRegions` in `infra/config/regions.json`: change it in `bootstrap.config.json` by a reviewed pull request and repeat this step |

5. Verify:

   ```bash
   gh run rerun <run ID> --failed
   ```

   Expected: its token step reads the vault.

## Verification

Step 5.

## Undo

None needed; the steps restore the declared state.

## Audit record

The bootstrap's Activity Log entries and the GitHub organization's audit log.
