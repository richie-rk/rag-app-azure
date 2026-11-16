# ADR 0115: Deploy identities are privileged operators

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Product owner
- **Specified in:** 82-security.md, 83-infrastructure.md, 85-observability.md, 86-operations.md, 84-testing.md
- **Related ADRs:** 0024, 0095, 0103, 0108, 0111, 0113, 0114, 0116, 0117, 0118, 0120, 0124, 0142

## Context

Each environment's deploy identity holds Contributor on its resource group and may assign only listed roles through a role-assignment condition. That alone doesn't confine it: Cosmos DB data-plane role assignments aren't Azure RBAC assignments, so Contributor's `sqlRoleAssignments/write` grants any Cosmos DB data role and no condition applies; a condition can't tie a role to the API or ingestion identity until their principal IDs exist, so the deploy identity could assign data roles to itself; and Contributor can re-enable key authentication, call `listKeys` and add a federated credential to an app identity and sign in as it. Reading another user's conversations must stay an audited just-in-time action. So the product treats each deploy identity as a privileged operator of its environment and bounds it with controls it can't remove, all created by the bootstrap (ADR 0114).

Vendor facts (checked 2026-10-14):

- Creating a Cosmos DB data-plane role assignment needs only the control-plane actions `Microsoft.DocumentDB/databaseAccounts/sqlRoleDefinitions/read`, `sqlRoleAssignments/read` and `sqlRoleAssignments/write`; Cosmos DB data-plane roles are distinct from Azure RBAC roles.
- Contributor's NotActions exclude only `Microsoft.Authorization/*/Write` and `*/Delete` and a few other actions; nothing under `Microsoft.DocumentDB`, `Microsoft.Storage` or `Microsoft.ManagedIdentity` is excluded.
- A role-assignment condition can test `RoleDefinitionId`, `PrincipalId` and `PrincipalType` (`User`, `ServicePrincipal`, `Group`), in `@Request` attributes for writes and `@Resource` attributes for deletes.

## Decision

1. **What each deploy identity is.** `id-docchat-deploy-dev`, `id-docchat-deploy-prod` and `id-docchat-deploy-load` (in `rg-docchat-identity`) are privileged operators of their environments: they deploy the code that runs as the app identities, and Contributor lets them write Cosmos DB data-plane assignments that no condition restricts.
2. **Their rights**, all granted by the bootstrap with `principalType: 'ServicePrincipal'`:

   | Right | Scope | Environments |
   |---|---|---|
   | Contributor (`b24988ac-6180-42a0-ab88-20f7382dd24c`) | `rg-docchat-<env>` | all |
   | Role Based Access Control Administrator (`f58310d9-a9f6-439a-9e8d-f62e7b41a168`) with the condition of item 3 | `rg-docchat-<env>` | all |
   | Role Based Access Control Administrator with an AcrPull-only condition (`7f951dda-4ed3-4680-a7ca-43fe172d538d`, `ServicePrincipal` only) | shared registry | all |
   | DocChat Registry Endpoint Approver; DocChat Shared Deployment Operator (`rg-docchat-shared`) | registry; `rg-docchat-shared` | all (ADR 0118) |
   | DocChat Defender Scanner Enabler; DocChat Preflight Reader | subscription | all (ADR 0117) |
   | AcrPush (`8311e382-0749-4cb8-b61a-304f252e45ec`); DocChat Registry Firewall Editor | shared registry | `dev` only (ADR 0118) |
   | Microsoft Graph `Application.ReadWrite.OwnedBy` (`18a4783c-866b-4cc7-a460-3d5e5662c884`) | its service principal | all |
   | Member of `sg-docchat-<env>-sql-admins` | the SQL server's Entra admin group | all |

3. **The initial condition** (version `2.0`, set by the bootstrap on the resource-group assignment): a `Microsoft.Authorization/roleAssignments/write` passes only when `@Request[…RoleDefinitionId]` is in the set `<R>` and `@Request[…PrincipalType]` is `ServicePrincipal` or `Group`; a `…/delete` passes only under the same test on `@Resource` attributes. `<R>` holds the roles the environment assigns at or below the group: Storage Blob Data Contributor, Storage Blob Data Reader, Storage Blob Data Owner, Storage Queue Data Contributor, Storage Queue Data Reader, Storage Queue Data Message Processor, Storage Queue Data Message Sender, Storage Table Data Contributor, Search Index Data Reader, Search Index Data Contributor, Cognitive Services OpenAI User, Cognitive Services User, Monitoring Metrics Publisher, Key Vault Secrets User (used only under content-share fallback 1), Reader, Log Analytics Reader, Defender for Storage Data Scanner (`1e7ca9b1-60d1-4db8-a914-f2ca1ff27c40`), DocChat Search Index Manager and DocChat Blob Tag Reader.
4. **Tightening after the first provision** of `dev` and of `prod`: the bootstrap operator runs `scripts/bootstrap/tighten_conditions.py --env <env>`, which reads the API and ingestion principal IDs from the identities themselves (`az identity show`), the operator and config-operator group IDs from `infra/env/<env>.json` and the subscription's Defender scanner principal, and rewrites both clauses as a disjunction of `(RoleDefinitionId in <set> AND PrincipalId in {<principal>})` pairs:

   | Principal | Allowed roles |
   |---|---|
   | API identity | Storage Blob Data Contributor, Storage Blob Data Reader, Storage Queue Data Message Sender, Search Index Data Reader, Cognitive Services OpenAI User, Cognitive Services User, Monitoring Metrics Publisher |
   | Ingestion identity | Storage Blob Data Contributor, DocChat Blob Tag Reader, Storage Queue Data Reader, Storage Queue Data Message Processor, Storage Queue Data Message Sender, Storage Blob Data Owner, Storage Queue Data Contributor, Storage Table Data Contributor, Search Index Data Contributor, DocChat Search Index Manager, Cognitive Services OpenAI User, Cognitive Services User, Monitoring Metrics Publisher (Key Vault Secrets User under fallback 1) |
   | `sg-docchat-operators` | Reader, Log Analytics Reader |
   | `sg-docchat-<env>-config-operators` | Storage Queue Data Message Processor |
   | Defender's `StorageDataScanner` identity | Defender for Storage Data Scanner (`dev` and `prod`, ADR 0117) |
   | registry condition | AcrPull for the API and ingestion identities only |

   It reads both assignments back and compares the condition text exactly (`condition_update_failed`), adds the principals to `allowedRoleAssigneePrincipalIds` and the template-declared Cosmos DB assignment IDs to `templateCosmosAssignmentIds`, runs the bootstrap's `security` step again and creates the `master` database's `sql-audit-security` setting (ADR 0108). It runs again whenever a workload identity or a template-declared Cosmos DB assignment is re-created (runbook R23). `load` is never tightened: its identities are new on every run, it holds only synthetic data, and its deploy identity's rights end at `rg-docchat-load` and AcrPull.
5. **Deny policies it can't remove** (ADR 0116): key and local authentication on storage, Cosmos DB, AI Search, the AI services and SQL; anonymous blob access; federated credentials on the app identities.
6. **Locks and a delete-deny policy it can't remove** on `prod`'s stores (ADR 0111).
7. **A record it can't erase:** the privileged SQL audit of every statement it runs, copied to `log-docchat-security` (ADR 0108).
8. **Alerts outside its reach**, deployed by the bootstrap into `rg-docchat-identity` and sent to `ag-docchat-security`. Activity Log alerts (subscription scope, category Administrative, status Succeeded): `docchat-alert-federated-credentials` (`Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials/write` or `/delete`); `docchat-alert-key-access` (storage `listKeys` and `regenerateKey`, Cosmos DB `listKeys` and `listConnectionStrings`, Cognitive Services `listKeys`, AI Search `listAdminKeys` and `listQueryKeys`, registry `listCredentials`); `docchat-alert-governance` (policy assignment write or delete, policy exemption write, lock delete). Log search alerts on `log-docchat-security` (every 15 minutes over 30 minutes, muted 30 minutes after firing): `docchat-alert-role-assignments` (severity 2: every role-assignment delete in the five resource groups or at the subscription, and every write whose principal isn't in `allowedRoleAssigneePrincipalIds`, deploy-identity deployments included); `docchat-alert-cosmos-data-access` (severity 1: every Cosmos DB data-plane assignment write or delete except re-writes of the IDs in `templateCosmosAssignmentIds`); `docchat-alert-diagnostic-settings` (severity 2: every delete, and every write outside a deploy-identity deployment); `docchat-alert-app-registrations` (severity 1: a new credential, or a changed `AppAddress` or `KeyDescription`, on a registration whose name starts with `DocChat`); `docchat-alert-registry-writes` (severity 2); `docchat-alert-jit-grant` (severity 3), `docchat-alert-jit-self-grant` (severity 1) and `docchat-alert-jit-overdue` (severity 2: no removal within 4 hours 15 minutes). The first three log rules ignore `id-docchat-deploy-load`'s operations in `rg-docchat-load` and its AcrPull writes and deletes on the registry.
9. **The gates in front of it.** Its GitHub environment trusts branch `main` only, and `prod` needs a second person's approval that no administrator can bypass (ADR 0120); the jobs that build images and web assets hold no OIDC token, so a compromised build dependency can't sign in as it (ADR 0124).

## Alternatives considered

- **A custom role in place of Contributor that excludes `sqlRoleAssignments/write` and `/delete` and `federatedIdentityCredentials/write`, with the API's Cosmos DB assignment made by a tenant administrator.** Not adopted: the deploy identity deploys the code that runs as the API, so it can already read what the API reads; the custom role would add a manual post-provision step for little protection.
- **The initial condition without tightening.** Rejected: until a condition names principals, the deploy identity can give itself any role in `<R>`, the data roles included.
- **Alerts declared by `main.bicep` in the environment's group.** Rejected: the identity they watch could delete them.

## Consequences

- The deploy identity still reaches data indirectly: the code it deploys runs as the app identities, and it can write Cosmos DB data-plane assignments; every such write outside the template's own assignments raises a severity-1 alert, and every statement it runs in SQL is recorded where it can't erase it.
- Expected alerts: the first provision raises `docchat-alert-role-assignments` once, before tightening; under content-share fallback 1 every provision calls `listKeys` and raises `docchat-alert-key-access`; each `dev` publish job raises `docchat-alert-registry-writes`. Runbook R20 routes an alert that a pipeline run or ticket explains to R07 and an unexplained one to R31.
- A provision that re-creates a workload identity fails at the tightened condition, by design, until R23 runs `tighten_conditions.py`.
- Tests (84-testing.md, deployed `dev`): after tightening, the deploy identity's attempt to assign Storage Blob Data Reader to itself on `documents` fails with `AuthorizationFailed`, and so does an attempt to assign Owner to the API identity; Azure Policy denies `disableLocalAuth: false` on Cosmos DB, `allowSharedKeyAccess: true` or `allowBlobPublicAccess: true` on the data account, and a federated credential on `id-docchat-api-dev`; a Cosmos DB data-plane assignment by the bootstrap operator raises `docchat-alert-cosmos-data-access` within 30 minutes and a `dev` redeploy raises no security alert; a role assignment to a principal outside the allowed list and any role-assignment delete raise `docchat-alert-role-assignments`; deleting the Cosmos DB diagnostic setting raises `docchat-alert-diagnostic-settings`; a redirect URI added to the `dev` web registration raises `docchat-alert-app-registrations`; a just-in-time group owner adding themselves raises `docchat-alert-jit-self-grant`.

## References

- Cosmos DB data-plane role-based access control: https://learn.microsoft.com/en-us/azure/cosmos-db/how-to-connect-role-based-access-control (checked 2026-10-14)
- Contributor and privileged built-in roles: https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/privileged (checked 2026-10-14)
- Role-assignment conditions (actions and attributes): https://learn.microsoft.com/en-us/azure/role-based-access-control/conditions-authorization-actions-attributes (checked 2026-10-14)
- Activity Log alerts (API 2026-01-01): https://learn.microsoft.com/en-us/azure/templates/microsoft.insights/activitylogalerts (checked 2026-10-15)
- Scheduled query rules (API 2026-03-01): https://learn.microsoft.com/en-us/azure/templates/microsoft.insights/scheduledqueryrules (checked 2026-10-15)
