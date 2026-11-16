# R22 — First provision of an environment

**Performer:** a maintainer starts it; the post-provision steps need a tenant administrator (Cloud Application Administrator or Application Administrator), the bootstrap operator and, in `prod`, a Security Administrator and a Privileged Role Administrator.

**Trigger:** a new environment.

**Prerequisites:** the operator workstation of `86-operations.md` section 1.5; R21 done; the environment's `infra/env/<env>.json` merged; `DOCCHAT_OPERATOR_IP_ALLOWLIST` and `DOCCHAT_OPS_ALERT_EMAILS` set on the GitHub environment (`prod`'s preflight refuses an empty mailbox list). The steps follow `83-infrastructure.md` sections 11.4 and 11.5 in table order, and the record keeps each step's output.

Placeholders in angle brackets are filled in at run time.

## Steps

1. Provision and deploy: `dev` by a merge to `main` (R01); `prod` with this dispatch, approved by `docchat-release-approvers`:

   ```bash
   gh workflow run deploy.yaml -f environment=prod -f image_commit=<release commit on main> -f rollback=false
   ```

   Expected: the run succeeds; every API call answers `401` or `403` until step 2. The first provision raises `docchat-alert-role-assignments` once; acknowledge it with this runbook's ticket.

2. Post-provision step 1, consent and first admins: R13 part B; after `dev`'s, R13 part C once per tenant.

3. Post-provision step 2, `dev` and `prod`. In `dev` only, the bootstrap operator first runs the bootstrap's `assignments` step again, now that the provision has created `log-docchat-dev`, so `id-docchat-citest` gets Log Analytics Reader on it (`83-infrastructure.md` section 6.8):

   ```bash
   uv run --frozen python scripts/bootstrap/bootstrap.py --step assignments
   az role assignment list --assignee <id-docchat-citest client ID> --scope <log-docchat-dev resource ID> -o table
   ```

   Expected: the step reports `succeeded` without saying that `log-docchat-dev` doesn't exist, and the listing shows Log Analytics Reader. The re-run resets every deploy identity's conditions to their initial form: when `prod` was tightened before, run R23 for `prod` afterwards. Then, in `dev` and `prod`, the bootstrap operator runs:

   ```bash
   uv run --frozen python scripts/bootstrap/tighten_conditions.py --env <env>
   ```

   Expected: exit 0 (`principal_ids_missing`: the provision didn't finish; `condition_update_failed`: run it once more); then commit the configuration diff it prints. In `dev` without a scanner it prints that it must run once more before `dev` scanning turns on (R23).

4. Post-provision step 3, `prod`: the bootstrap operator runs:

   ```bash
   uv run --frozen python scripts/bootstrap/lock_prod.py
   az lock list --resource-group rg-docchat-prod -o table
   az policy assignment show --name docchat-deny-store-deletion --resource-group rg-docchat-prod
   ```

   Expected: `lock_prod.py` exits 0 (exit 2 with `prod_not_protected`: rerun `bootstrap.py --step policies`, then this step); the lock list shows exactly six `docchat-protect` locks, and the policy assignment read succeeds. Paste the output into the record.

5. Post-provision step 4, `prod`: a Security Administrator creates `docchat-prod-audit`:

   ```bash
   az rest --method PUT --url "https://management.azure.com/providers/microsoft.aadiam/diagnosticSettings/docchat-prod-audit?api-version=2017-04-01" --body @<file naming the prod workspace ID and the AuditLogs category>
   ```

6. Post-provision step 5, scanning on (`prod` by default): R12.

7. Post-provision step 6, once after the bootstrap: the first test-user passwords (R13 part C step 3).

8. Post-provision step 7, only when `cosmosGroupDataAccess` is `false`: the bootstrap operator assigns DocChat Cosmos Data Access Granter to each approver:

   ```bash
   az role assignment create --assignee-object-id <approver object ID> --assignee-principal-type User --role f6b96867-d30e-4380-9a16-fbac2370fcd8 --scope <Cosmos DB account ID>
   ```

9. Post-provision step 8, tenants with Entra ID P2 or ID Governance: a Privileged Role Administrator configures PIM for Groups on the three just-in-time groups: eligible assignments for the people in `operators` only, never an approver, whose activation needs a group owner's approval, a justification, a ticket number and MFA, with a maximum activation of 4 hours (`13-conversations-and-memory.md` section 10.1).

10. Post-provision step 9, only under fallback 1: R27 step 1.

11. Prices: R03 with `pricing.model_rates` and `pricing.currency` for every deployed model; `prices-missing` stays silent for an hour before users are assigned. This is the deployed pricing check (T-275): record the `config show --key pricing.model_rates` correlation ID, which lists a rate for every deployment of `infra/env/<env>.json`, and the hour in which `docchat.pricing.rate_missing` stayed 0.

12. Verify: the deployed `smoke` suite (`dev`) or `prod_smoke` (`prod`) passes; log the first provision and every step's output in `docs/runbooks/rehearsals.md`.

13. Follow-up, after the environment's first full idle month: the idle-cost check (T-478). The product owner reads Cost Management's actual cost for that calendar month grouped by resource group, takes `rg-docchat-<env>`'s total and compares it with the environment's figure in `83-infrastructure.md` section 14.2 (`dev` about $426, `prod` about $1,433). Expected: within 10% of the figure. The month, the actual cost, the figure and the result go into the follow-ups field of the acceptance record of the stage that provisioned the environment (`90-build-order.md` section 1.3.2); the check gates no stage.

## Verification

Step 12; step 13 when the first idle month closes.

## Undo

R30.

## Audit record

The workflow runs, the Activity Log, Entra's audit log, the rehearsal log entry.
