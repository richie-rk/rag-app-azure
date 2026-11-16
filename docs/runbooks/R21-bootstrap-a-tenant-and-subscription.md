# R21 — Bootstrap a tenant and subscription

**Performer:** the bootstrap operator: a person holding Owner on every subscription in use and the Entra roles Privileged Role Administrator, Groups Administrator and Security Administrator, signed in to `gh` as an owner of the GitHub organization (`83-infrastructure.md` section 11.1).

**Trigger:** a new tenant or subscription set; any change to `scripts/bootstrap/bootstrap.config.json`; a step to repair.

**Prerequisites:** the operator workstation of `86-operations.md` section 1.5, with Bicep CLI 0.47.16; `bootstrap.config.json` filled in (`83-infrastructure.md` section 8.5): tenant, subscriptions, region, GitHub organization and repository, `ciPrivateNetworking` (`false` by default: standard GitHub-hosted test runners and the test vault's transient runner rule; `true` only for an organization on GitHub Team or GitHub Enterprise Cloud, with `ciNetworkLocation`), `operators` with every person of `86-operations.md` section 1.1's rule "Who runs Azure reads" other than the approvers, at least two approvers per environment, at least two `groupOwners`, `securityAlertEmails`, `testUserAliases`; `DOCCHAT_SHARED_OPERATOR_IP_ALLOWLIST` exported in the shell, holding the operator's own address for the duration of the run.

Placeholders in angle brackets are filled in at run time.

## Steps

1. Sign in to Azure and check the GitHub sign-in:

   ```bash
   az login --tenant <tenant ID>
   gh auth status
   gh api /orgs/<organization>/memberships/<login> --jq .role
   ```

   Expected: `gh auth status` shows the account, and the membership call returns `admin`.

2. Run the bootstrap:

   ```bash
   uv run --frozen python scripts/bootstrap/bootstrap.py
   ```

   Expected: the steps `prerequisites` to `checklist` each report success, the `ci-runner` step reports `skipped` unless `ciPrivateNetworking` is `true`, and the script exits 0. A failed step stops with its code (`83-infrastructure.md` section 10.18) and leaves the earlier steps in place: fix the cause, then run that step and each later step with `--step`:

   ```bash
   uv run --frozen python scripts/bootstrap/bootstrap.py --step <name>
   ```

   | Code | Step | Cause to fix |
   |---|---|---|
   | `missing_prerequisite`, `approver_is_operator` | `prerequisites` | a tool version, the `gh` sign-in as an organization owner, a caller role, `securityAlertEmails`, two approvers per environment or two `groupOwners`; an approver listed in `operators` |
   | `unknown_action` | `roles` | a custom-role action name |
   | `oidc_subject_shared`, `oidc_subject_customised` | `identity`, `github` | two pipeline identities trusting one OIDC subject; a customised OIDC subject format |
   | `graph_permission_failed` | `graph-permission` | the Graph grant to a deploy identity |
   | `policy_definition_missing` | `policies` | a built-in policy definition missing or unable to deny |
   | `github_protection_mismatch` | `github` | a protected GitHub environment's reviewers, self-review block or administrator bypass |
   | `ci_region_unsupported` | `ci-runner`, private-networking option only | `ciNetworkLocation` isn't a region where GitHub joins hosted runners to an Azure network |
   | `ci_private_networking_unavailable` | `ci-runner`, private-networking option only | the organization can't create the 2-core Ubuntu 24.04 larger runner (it needs GitHub Team or Enterprise Cloud): set `ciPrivateNetworking` to `false` by a reviewed pull request, then re-run the `shared`, `assignments`, `github` and `ci-runner` steps to use standard runners (ADR 0122 item 2) |
   | `ci_runner_mismatch` | `ci-runner`, private-networking option only | a network configuration, runner group or runner read back differently: re-run the step |

   The `shared` step redeploys the test vault's `ipRules` from the shared allowlist and so removes a live test-job runner rule: on a re-run, start it while no test job reads the vault (`83-infrastructure.md` section 11.2).

3. Merge the `infra/env/*.json` changes that the `config` step prints through a pull request.

4. Create the test identities (R13 part A); their Conditional Access policy and first passwords follow `dev`'s first provision (R22, then R13 part C), because the policy names `dev`'s applications.

5. Verify:

   ```bash
   az role assignment list --assignee <dev deploy identity client ID> --all -o table
   az role assignment list --assignee <id-docchat-citest client ID> --all -o table
   gh api /repos/<organization>/<repository>/environments --jq ".environments[].name"
   gh variable get DOCCHAT_CI_PRIVATE_NETWORKING
   az policy assignment list --resource-group rg-docchat-prod --query "[].name" -o tsv
   ```

   Expected:
   - the `dev` deploy identity holds the rights of `83-infrastructure.md` section 6.8;
   - `id-docchat-citest` holds exactly Key Vault Secrets User on the test vault, DocChat Test Vault Firewall Editor on the test vault while `ciPrivateNetworking` is `false` (none under the private-networking option), Log Analytics Reader and DocChat Load Test Operator at `rg-docchat-load` and, once `dev` has been provisioned and R22 step 3 has run the `assignments` step again, Log Analytics Reader on `log-docchat-dev` (before that, the `assignments` step printed that the workspace doesn't exist yet);
   - the environments are `dev`, `prod`, `load`, `load-access`, `dev-tests` and `load-tests`;
   - `DOCCHAT_CI_PRIVATE_NETWORKING` holds the configuration's `ciPrivateNetworking` (`false` by default);
   - the policy assignments are the `docchat-deny-*` assignments of `83-infrastructure.md` section 12.1, `docchat-deny-store-deletion` included.

6. Remove the operator's address from the shared allowlist (R24 step 4 with the list it held before) unless password rotation (R08) follows in the same session.

7. Under fallback 1, a rerun of the `policies` step redeploys `docchat-deny-storage-shared-key` without the host account in `notScopes`: repeat R27 step 1 for each environment under fallback 1 in the same session.

8. Log the run (date, operator handle, commit) in `docs/runbooks/rehearsals.md`.

## Verification

Step 5.

## Undo

R30 per environment.

## Audit record

The subscription Activity Log and Entra `AuditLogs` in `log-docchat-security`; the GitHub organization's audit log.
