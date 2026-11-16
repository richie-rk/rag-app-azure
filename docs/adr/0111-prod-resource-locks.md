# ADR 0111: Prod resource locks

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Architect
- **Specified in:** 83-infrastructure.md, 82-security.md, 84-testing.md, 86-operations.md
- **Related ADRs:** 0024, 0095, 0104, 0108, 0115, 0116, 0117, 0119, 0126, 0142

## Context

Each deploy identity holds Contributor on its environment's resource group (ADR 0115), so a compromised `prod` pipeline could delete `prod`'s stores and logs. Contributor can't create or delete locks or policy assignments, so either can stop it. A lock, though, covers everything beneath the locked resource, and two `prod` paths delete such children by design: every `prod` provision job removes its transient runner rule `ci-runner-<run>-<attempt>`, a child of the SQL server, at the end of the job (ADRs 0104, 0119); and in the per-principal just-in-time mode (`cosmosGroupDataAccess: false`), `docchat-ops jit revoke` deletes the member's Cosmos DB data-plane role assignments, children of the account (ADR 0024). Protection must therefore stop deletion of the stores without blocking those two deletions.

Vendor facts:

- `CanNotDelete` lets authorised users read and modify a resource but not delete it. "When you apply a lock at a parent scope, all resources within that scope inherit the same lock"; extension resources inherit the lock of the resource they apply to, so a diagnostic setting on a locked storage account can't be deleted. "A cannot-delete lock on a resource or resource group prevents the deletion of Azure RBAC assignments"; a cannot-delete lock on a resource group stops Resource Manager from deleting old deployments, and deployments fail at 800. Locks apply only to control-plane operations. Creating or deleting a lock needs `Microsoft.Authorization/*` or `Microsoft.Authorization/locks/*` (Owner, User Access Administrator).
- The Azure Policy effect `denyAction` blocks `DELETE` requests (the only supported action) with `403`. A policy that applies to a parent protects the parent while its children stay deletable; `cascadeBehaviors.resourceGroup: 'deny'` blocks deleting the resource group and is supported only in `mode: 'Indexed'`; locks and policy assignments are exempt from `denyAction`.
- Contributor's NotActions exclude `Microsoft.Authorization/*/Write` and `Microsoft.Authorization/*/Delete`; Role Based Access Control Administrator holds only role-assignment write and delete, reads and support actions.

## Decision

1. **Six locks.** After `prod`'s first provision the bootstrap operator (Owner) runs `scripts/bootstrap/lock_prod.py`, which adds a `CanNotDelete` lock named `docchat-protect` (`Microsoft.Authorization/locks`) to:
   - the SQL database `docchat` (`Microsoft.Sql/servers/databases`);
   - the Cosmos DB database `docchat` (`Microsoft.DocumentDB/databaseAccounts/sqlDatabases`), which covers its containers `conversations`, `memories` and `usage`;
   - the data storage account `stdocchatdataprod<token>`;
   - the Key Vault `kv-docchat-prod-<token>`;
   - the AI Search service `srch-docchat-prod-<token>`;
   - the Log Analytics workspace `log-docchat-prod`.
   The script is idempotent and exits 2 unless `az lock list --resource-group rg-docchat-prod` then shows exactly these six `docchat-protect` locks and the assignment of item 2 exists.
2. **The two parents: a delete-deny policy.** The bootstrap's `policies` step (ADR 0116) creates the custom definition `docchat-deny-store-deletion` (`Microsoft.Authorization/policyDefinitions@2026-07-01`, subscription scope, `mode: 'Indexed'`) with the rule `{"if": {"field": "type", "in": ["Microsoft.Sql/servers", "Microsoft.DocumentDB/databaseAccounts"]}, "then": {"effect": "denyAction", "details": {"actionNames": ["delete"], "cascadeBehaviors": {"resourceGroup": "deny"}}}}` and assigns it as `docchat-deny-store-deletion` at `rg-docchat-prod`, with a `nonComplianceMessages` entry naming this ADR. A `DELETE` of the `prod` SQL server or Cosmos DB account, and of `rg-docchat-prod`, answers `403`; their children (firewall rules, Cosmos DB data-plane role assignments, private endpoint connections) stay deletable, and their databases are locked by item 1.
3. **Who can remove them.** Only a holder of `Microsoft.Authorization/locks/*` removes a lock (Owner, User Access Administrator) and only a holder of `Microsoft.Authorization/policyAssignments/delete` removes the assignment (Owner, Resource Policy Contributor); no pipeline identity holds either. `docchat-alert-governance` reports every lock deletion and every policy assignment write or delete (ADR 0115).
4. **What stays possible.** Data-plane operations (blob and document deletion, index deletion through the search endpoint, SQL and Cosmos DB data changes), every create and update an incremental deployment makes, the runner rule's removal and just-in-time revocation.
5. **What a lock blocks, and the procedure.** Deleting, under a locked resource, a role assignment, a diagnostic setting, a Cosmos DB container, a blob container or queue through Resource Manager, the SQL database, or Defender's scanner role assignment when scanning is switched off. A runbook that needs one of these (R30 decommission, R31 removal of a foreign role assignment on a locked resource, switching `prod` scanning off) removes that one lock with `az lock delete --ids <lock ID>`, makes the change and runs `lock_prod.py` again in the same session; R30 also deletes the policy assignment before it deletes the resource group.
6. **`dev` and `load` carry neither.** Their data is test data, and `load`'s teardown deletes every resource of the run in complete mode (ADR 0126).

## Alternatives considered

- **Locks on the SQL server and the Cosmos DB account.** Rejected: the locks reach their children, so the `prod` provision job couldn't remove its runner rule (every release after the first would fail at `rule_close_failed`) and per-principal revocation couldn't delete a member's data-plane assignment, leaving the member with access.
- **One lock on `rg-docchat-prod`.** Rejected: it reaches every resource, so it blocks the same child deletions, and it stops Resource Manager from pruning deployment history, so deployments fail at 800.
- **ReadOnly locks.** Rejected: they block the updates every provision makes and the role assignments scoped to storage.
- **Delete-deny policies for all six stores.** Rejected: a policy on a parent leaves its children deletable, so a Contributor could delete the `documents` container or the Cosmos DB containers through Resource Manager; locks cover children.
- **Alerts only.** Rejected: an alert arrives after the deletion, and deleting the SQL server deletes its databases with it.

## Consequences

- A compromised `prod` pipeline can't delete `prod`'s SQL database or server, the Cosmos DB account, database or containers, the data account, the vault, the search service, the workspace or the resource group; it can still delete Cosmos DB data-plane assignments and diagnostic settings outside the locked resources, which `docchat-alert-cosmos-data-access` and `docchat-alert-diagnostic-settings` report.
- Owners are held by the locks and the policy too: every exceptional deletion is a deliberate, alerted removal followed by `lock_prod.py`.
- The `prod` release path and just-in-time revocation work with the protection in place.
- Tests (84-testing.md): `lock_prod.py`'s own check of item 1, whose output runbook R22 step 4 records; the anonymous `prod_smoke` suite holds no Azure identity, so it doesn't read locks.

## References

- Resource locks (inheritance, role assignments, deployment history, permissions): https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/lock-resources (checked 2026-10-21)
- Azure Policy `denyAction` effect: https://learn.microsoft.com/en-us/azure/governance/policy/concepts/effect-deny-action (checked 2026-10-21)
- Contributor and privileged built-in roles: https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/privileged (checked 2026-10-14)
- Policy definitions resource (API 2026-07-01): https://learn.microsoft.com/en-us/azure/templates/microsoft.authorization/policydefinitions (checked 2026-10-14)
