# ADR 0126: Load environment lifecycle

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Product owner and architect
- **Specified in:** 83-infrastructure.md, 84-testing.md, 86-operations.md, 82-security.md
- **Related ADRs:** 0016, 0019, 0095, 0112, 0120, 0124, 0134, 0135, 0151

## Context

The scaling targets are proved by a load test that runs in a short-lived environment with production SKUs and a stubbed model (ADR 0134). That environment needs user tokens, so the product owner decided that a test-client registration exists in every non-production environment, `load` included, never in `prod`, and that load-test teardown permanently deletes that environment's app registrations. Deleting a resource group leaves Microsoft Graph objects behind, and a deleted application keeps its `uniqueName` while it sits in deleted items, which would block the next run's provision.

Two costs bound the lifecycle. Idle, `load` costs about $2,427 a month, $3.32 an hour, so an environment that a failed run leaves behind must be removed without anyone having to remember it. And a full run is long: the two S4 sets alone can take 700 minutes, so a guard that tears down by age alone would cut a live run short.

Vendor facts (checked 2026-10-14 unless stated):

- A complete-mode deployment never deletes `Microsoft.Authorization/roleAssignments`, `policyAssignments` or `locks`, and does delete `Microsoft.CognitiveServices/accounts`, `Microsoft.KeyVault/vaults`, `Microsoft.DocumentDB/databaseAccounts` and `Microsoft.EventGrid/systemTopics` (checked 2026-10-15).
- Microsoft Graph: after `DELETE /applications/{id}`, "apps are moved to a temporary container and can be restored within 30 days"; `DELETE /directory/deletedItems/{id}` deletes a recently deleted application permanently; both accept the application permission `Application.ReadWrite.OwnedBy` (checked 2026-10-21).
- "You can't reuse the name of a key vault that was soft-deleted, until the retention period expires", which is 7 to 90 days, set at creation (7 days for the `load` vault, checked 2026-10-21); a deleted Cognitive Services account stays soft-deleted for 48 hours and holds its name.

## Decision

1. **One permanent group, one run at a time.** `rg-docchat-load` is created by the bootstrap and never deleted, so its role and policy assignments survive between runs: the `load` deploy identity's Contributor and conditioned Role Based Access Control Administrator, and `id-docchat-citest`'s Log Analytics Reader and DocChat Load Test Operator. `load-test.yaml` (`workflow_dispatch` only) has the workflow-level concurrency group `docchat-load-test` with `cancel-in-progress: false`, so the group holds at most one run.
2. **Fresh names per run.** In `load` the resource token is `take(uniqueString(subscription().id, environmentName, resourceTokenSalt), 6)` with `resourceTokenSalt = 'run-<GitHub run ID>'`, so every globally unique name, the run's registrations' `uniqueName` values (`docchat-load-<token>-web`, `-api`, `-test`) included, is new; soft-deleted names of the previous run never collide, and the `load` deploy identity needs no purge right.
3. **What a run contains.** Production SKUs for every component under test, with `load`'s own values where the load test needs them: Cosmos DB autoscale at 30,000 RU/s per container and a workspace with no daily cap (ADR 0151); no Azure OpenAI account, both apps calling the model stub (ADR 0112); real Content Safety; the test client and the `load` test users (ADR 0019). Identity rules are non-production: the role-assignment conditions are never tightened, because the run's identities are new every time and the environment holds only synthetic data; the security alert rules for role assignments, Cosmos DB data-plane assignments and diagnostic settings ignore operations by `id-docchat-deploy-load` in `rg-docchat-load` and its AcrPull writes and deletes on the shared registry.
4. **Create** (`load-test.yaml` job `create`): job `manifest` first checks the input `deploy_run_id` with `scripts/ci/check_deploy_run.py` (`path` `.github/workflows/deploy.yaml`, `head_branch` `main`, `conclusion` `success`; otherwise `deploy_run_invalid`) and reads that run's `release-manifest`; `create` then calls `deploy-environment.yaml` with `environment: load`, `mode: full`, `resource_token_salt: run-${{ github.run_id }}`, the three digest references and `web_artifact_run_id: <deploy_run_id>`. Before anything is provisioned, that job tags the group with `az tag update --resource-id /subscriptions/<subscription>/resourceGroups/rg-docchat-load --operation Merge --tags docchat-load-run=<github.run_id> docchat-load-created=<UTC time as yyyy-MM-ddTHH:mm:ssZ>`, so the guard sees every environment from its first resource. Outputs: `api_base_url`, `web_base_url`, `web_client_id`, `test_client_id`, `api_scope`.
5. **Access gate.** Job `access-gate` runs in GitHub environment `load-access` (required reviewers `docchat-tenant-admins`, no administrator bypass, ADR 0120). Before approving, the reviewer completes runbook R11 steps 3 to 6: the post-provision identity script for the run's registrations (consent and the `load` test groups' assignments), the run's API and test client added to the exclusions of the test users' Conditional Access policy, and `docchat-ops --env load testusers check --set load --min-hours 24`.
6. **Teardown.** `scripts/deploy/teardown_load.py`, as the `load` deploy identity, run by `load-test.yaml`'s `teardown` job (`if: always()` after every other job; environment `load`; concurrency `docchat-env-load`) and by the guard of item 7:
   1. deletes each of the run's three registrations, found by `uniqueName`, object by object: its service principal with `DELETE https://graph.microsoft.com/v1.0/servicePrincipals/{id}`, then its application with `DELETE https://graph.microsoft.com/v1.0/applications/{id}`, then both objects permanently with `DELETE https://graph.microsoft.com/v1.0/directory/deletedItems/{id}` (the `load` deploy identity owns them; a `404` counts as already deleted); the service principal is deleted explicitly because Graph's application-delete reference doesn't state that it removes it; any other Graph failure doesn't stop the loop, and after attempting every object the script exits 2 with `graph_delete_failed`;
   2. deletes the run identities' AcrPull assignments on the shared registry;
   3. runs `az deployment group create --resource-group rg-docchat-load --mode Complete --template-file infra/teardown/empty.bicep`, which removes every resource and keeps the group's role and policy assignments;
   4. repeats step 3 up to 3 times, 5 minutes apart, while `az resource list --resource-group rg-docchat-load` returns resources, then exits 2 with `teardown_incomplete` naming them (runbook R11 step 13 applies);
   5. removes the two run tags.
   Soft-deleted Key Vault and Cognitive Services names expire on their own and never block the next run.
7. **Nightly guard.** `nightly.yaml`'s job `load-guard` (daily at 02:30 UTC; environment `load`; the `load` deploy identity; concurrency `docchat-env-load`; `actions: read`): when `rg-docchat-load` carries a `docchat-load-created` tag older than 6 hours, it reads the run named by `docchat-load-run` (`gh run view <run ID> --json status -q .status`) and runs `teardown_load.py` unless that run is `queued`, `waiting` or `in_progress` and the tag is less than 24 hours old. A run's own `teardown` normally ends its environment; when it never runs, the guard's first daily run after the environment is 6 hours old and inactive, or 24 hours old, removes it, so no environment outlives 48 hours, and a run still working inside its first 24 hours is never cut short.

## Alternatives considered

- **A long-lived `load` environment.** Rejected: about $2,427 a month idle for a test run a few times a quarter, and state from one run would leak into the next.
- **Deleting the resource group at teardown.** Rejected: its role assignments and policy assignments would go with it, and re-creating them needs the bootstrap operator (Owner); a complete-mode deployment empties the group and keeps them.
- **Reusing names across runs.** Rejected: soft-deleted Key Vault and Cognitive Services names and deleted applications' `uniqueName` values would block the next run, or the `load` deploy identity would need subscription-level purge rights.
- **Leaving registrations in deleted items.** Rejected: the product owner requires permanent deletion, and a deleted registration stays restorable, with its name and its test-client configuration, for 30 days.
- **A guard by age alone (6 hours).** Rejected: the S4 sets alone can run 700 minutes, so a live run would be torn down; the guard spares a run that is still active and younger than 24 hours.
- **No test client in `load`.** Rejected: the load test would have no way to get user tokens, and it is the only proof of the scaling targets.

## Consequences

- Each run starts from nothing and ends with an empty group, no registrations in the tenant's deleted items and no AcrPull assignment left on the registry.
- A run costs about $3.32 an hour of idle-rate resources (about $20 for a typical 6-hour run, the load generator included), plus up to $9.72 of Cosmos DB throughput above the floor for each hour in which S3 drives the containers to their maxima, plus at most about $5 of runner minutes; it makes no model calls. A teardown that fails leaves the environment running until the guard's next daily run that finds it at least 6 hours old: at most about 30 hours, about $100 at the idle rate.
- Every run waits for a tenant administrator at `load-access`; a password that expires within 24 hours makes the reviewer reject the gate, rotate the `load` set (R08) and start a new run.
- The guard holds the `load` deploy identity's token only in the `load` environment's job, and the teardown needs no right outside `rg-docchat-load`, the run's registrations and AcrPull on the registry.
- Obligations: runbook R11 (run, gate, verdicts, failed teardown); the budget alert procedure R29 checks for a forgotten `load` environment first.
- Tests (84-testing.md): a load test proves that `rg-docchat-load` carries the run's two tags before it holds any resource of the run; the load run's own checks prove that teardown leaves `az resource list` empty and that each registration's `uniqueName` answers `404` both as an application and in deleted items (R11 step 10).

## References

- Complete-mode deletion: https://learn.microsoft.com/en-us/azure/azure-resource-manager/templates/deployment-complete-mode-deletion (checked 2026-10-15)
- Delete an application (Microsoft Graph v1.0): https://learn.microsoft.com/en-us/graph/api/application-delete (checked 2026-10-21)
- Permanently delete an item from deleted items (Microsoft Graph v1.0): https://learn.microsoft.com/en-us/graph/api/directory-deleteditems-delete (checked 2026-10-21)
- Key Vault soft delete: https://learn.microsoft.com/en-us/azure/key-vault/general/soft-delete-overview (checked 2026-10-21)
