# ADR 0024: Just-in-time operator access

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decided by:** Product owner and architect
- **Specified in:** 13-conversations-and-memory.md, 82-security.md, 83-infrastructure.md, 84-testing.md, 85-observability.md, 86-operations.md
- **Related ADRs:** 0014, 0025, 0028, 0029, 0033, 0054, 0098, 0108, 0114, 0115, 0138, 0139, 0142

## Context

The product owner decided that conversations are kept indefinitely and are open to debugging only through privileged, audited access. No app operation shows one person's conversations or memory to anyone else, admins included. Operators therefore have no role in the app: they change ops config and per-user index overrides through audited scripts, run ingestion recovery and user-data procedures, and read conversation data only through just-in-time access granted outside the app. Their standing group holds read-only rights; every data-plane right on SQL and Cosmos DB comes from time-bound group membership, through PIM for Groups where the tenant has Microsoft Entra ID P2 or Microsoft Entra ID Governance, and through an audited script everywhere else.

The design has to meet these needs:

- Logs and traces carry metadata only (ADR 0014), so investigating a turn means reading the conversation store, and that read must be the privileged path.
- Every human access to user data is attributable to one person, tied to a ticket, approved by a second person and logged per request.
- Changes to identities, roles, projects and ingestion leave a trail, and data-access grants are recorded where the people who approve them, who hold no data right, can't alter the record.
- The deploy identity's Contributor role includes `Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments/write`, which no Azure RBAC condition can restrict, so it could create a Cosmos DB data-plane assignment outside this route. Alerts outside the pipeline's reach must report every such assignment; ADR 0115 bounds the deploy identity, and this ADR covers people.

Facts that bound the design:

- Cosmos DB data-plane roles (Built-in Data Reader `00000000-0000-0000-0000-000000000001`, Built-in Data Contributor `00000000-0000-0000-0000-000000000002`) are distinct from Azure RBAC roles, so PIM for Azure resources can't time-bound them; an assignment takes a database or container scope.
- No Microsoft page says whether an Entra group object ID works as a data-plane `principalId`.
- `CDBDataPlaneRequests` records `AadPrincipalId` for every request when the per-request `DataPlaneRequests` category is on.
- A contained database user for an Entra group is created `WITH SID = <group object ID>, TYPE = X`, so SQL rights follow group membership.
- PIM for Groups makes membership of any security group eligible, role-assignable or not, with approval, MFA, justification, ticket information (an information-only field) and a maximum activation of 1 to 24 hours; it has no default approvers and Microsoft recommends at least two; administrators and group owners can still change membership outside PIM.
- A group that isn't role-assignable can also be managed by Microsoft Entra roles such as Groups Administrator and User Administrator, and Authentication, Helpdesk and User Administrators can change the credentials of its members. A role-assignable group can be managed only by Global Administrators, Privileged Role Administrators and its owners, no other administrator can change its members' or owners' credentials, and a tenant holds at most 500 of them.
- Access tokens last 60 to 90 minutes, and a new group membership reaches SQL and Cosmos DB only in a new token.

## Decision

1. **No standing data access for people.** A person reaches SQL, Cosmos DB or the poison queue only as a member of one of three Entra security groups per environment (`dev`, `prod`, `load`), which the bootstrap creates with no members (`securityEnabled: true`, `mailEnabled: false`, `isAssignableToRole: false`), owned by that environment's approvers:

   | Level | Group | Rights |
   |---|---|---|
   | `read` | `sg-docchat-<env>-conversation-readers` | Cosmos DB Built-in Data Reader at `/dbs/docchat/colls/conversations`; SQL role `docchat_conversation_reader` (`INSERT` on `app.audit_events`, for the `conversation.read` row) |
   | `user_data` | `sg-docchat-<env>-user-data-operators` | Cosmos DB Built-in Data Contributor at `/dbs/docchat`; SQL role `docchat_user_data_operator` (export, deletion, trace redaction) |
   | `config` | `sg-docchat-<env>-config-operators` | SQL role `docchat_config_operator` (ops config, index overrides, ingestion recovery); Storage Queue Data Message Processor on `ingestion-requests-poison`; no Cosmos DB right |

   The standing group `sg-docchat-operators` holds only Reader on each environment's resource group and on `rg-docchat-shared`, Log Analytics Reader on each environment's workspace and on `log-docchat-security`, and DocChat Test Secret Writer on the shared test-credential vault. Every person who runs a `docchat-ops` command, `azd env refresh` or an `az` read in a runbook is a member: the people listed in `operators` (the operators, and every maintainer, release approver or directory administrator who performs such a step) and the approvers. The SQL roles and their table rights are ADR 0025.
2. **Approvers.** Each environment has at least 2 named approvers (`approverObjectIds` in `scripts/bootstrap/bootstrap.config.json`), who own its three groups and hold no SQL or Cosmos DB right. No approver is an operator: the bootstrap stops with `approver_is_operator` (exit 2) when an approver is in the configured `operators` list, checked in its `prerequisites` step before any change. The bootstrap's `groups` step adds the approvers to `sg-docchat-operators` for its reads, which give no data-plane right, and PIM eligibility goes only to the people in `operators`, so an approver is never one of the people a grant is made for.
3. **Grants last at most 4 hours and need a second person.**
   - With Entra ID P2 or Microsoft Entra ID Governance: PIM for Groups, eligible membership for the people in `operators`; each group's policy requires approval by a group owner, a justification, a ticket number and MFA on activation, with a maximum activation of 4 hours. The post-provision checklist configures it on the three groups of each environment.
   - Otherwise a group owner runs `uv run docchat-ops --env <env> jit grant --level read|user_data|config --member <operator oid> --hours <1–4> --ticket <id> --reason "<10 to 500 characters>"`. It refuses with exit 2 when `--member` is the signed-in user, exits 6 when the member already belongs to the group, runs `az ad group member add`, and prints a receipt `{group, member, level, grantedAt, expiresAt, ticket, mode, assignmentId}`. `jit revoke --level … --member <oid>` removes the membership (exit 4 when there is none); `jit list` shows the three groups' members, which are empty outside active tickets.
   - The operator then signs in again (`az logout`, `az login`) so the membership is in the token, and works from a machine whose public IP is in the environment's operator allowlist (ADR 0098).
4. **Group principals in Cosmos DB, settled by a test.** While `cosmosGroupDataAccess` is `true` (the default), Bicep assigns the two Cosmos DB roles of rule 1 to the groups. The deployed group-principal test adds a `dev` test operator to `sg-docchat-dev-conversation-readers` and, after a fresh token, retries a point read every 60 s for up to 15 minutes: `200` keeps the group design; `403` sets `cosmosGroupDataAccess` to `false`, Bicep then creates no group assignment, the azd outputs carry `DOCCHAT_COSMOS_GROUP_DATA_ACCESS=false`, and `jit grant` also creates a per-principal assignment (same role and scope, a new UUID v4 assignment ID) for `read` and `user_data`, which `jit revoke` deletes; the bootstrap operator then assigns each approver the custom role DocChat Cosmos Data Access Granter (`f6b96867-d30e-4380-9a16-fbac2370fcd8`, actions `Microsoft.DocumentDB/databaseAccounts/read`, `Microsoft.DocumentDB/databaseAccounts/sqlRoleDefinitions/read`, `Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments/read`, `Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments/write` and `Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments/delete`) at the Cosmos DB account. The `config` level is membership alone in both modes. The test's result is recorded in this ADR.
5. **Every human request is logged.** Cosmos DB sends `DataPlaneRequests` (resource-specific) to `CDBDataPlaneRequests` with `AadPrincipalId`. SQL's privileged audit records every statement of every principal except the two workload identities in the environment's workspace and, in `dev` and `prod`, in `log-docchat-security` as well (ADR 0108). Every changing `docchat-ops` command requires `--ticket` (1 to 64 letters, digits, `-`, `_` or `#`) and `--reason` (10 to 500 characters) and writes its audit row with the ticket. Grants and revocations are not `audit_events` rows: Entra ID's audit log and the Activity Log record them, because approvers hold no SQL right.
6. **Alerts.** In `log-docchat-security` (365-day workspace in `rg-docchat-identity`, where no pipeline identity has rights; action group `ag-docchat-security`; evaluated every 15 minutes over 30 minutes): over `AuditLogs`, `docchat-alert-jit-grant` (severity 3, every `Add member to group` for a group in `jitGroupIds`), `docchat-alert-jit-self-grant` (severity 1, an `Add member to group` whose `InitiatedBy.user.id` equals the added member's object ID) and `docchat-alert-jit-overdue` (severity 2, no matching `Remove member from group` within 4 hours 15 minutes); over `AzureActivity`, `docchat-alert-cosmos-data-access` (severity 1, every Cosmos DB `sqlRoleAssignments` write or delete except re-writes of the template-declared assignments), which fires on every fallback grant and on any assignment the deploy identity makes outside the templates. In each environment's workspace, the alert `human-conversation-access` (severity 3, evaluated every 15 minutes over 15 minutes, conversation-access runbook): any `CDBDataPlaneRequests` row for database `docchat` whose `AadPrincipalId` isn't the API identity's principal ID.
7. **Monthly review.** An operator runs the human-access query over 31 days grouped by principal, matches every principal and time window to a ticket and to a grant in PIM history or the Entra `AuditLogs` of `log-docchat-security`, and confirms with `jit list` that the three groups are empty; any unmatched access starts the incident-response runbook.

## Alternatives considered

- **Standing data roles for the operators' group.** Standing access is unaudited at the moment of use and survives every ticket, and data-plane access for people is allowed only through just-in-time membership.
- **A conversation viewer for admins in the app.** No app operation may expose another user's conversations, admins included.
- **PIM for Azure resources (time-bound RBAC assignments per person).** Cosmos DB data-plane roles aren't Azure RBAC roles, and SQL needs a database principal; group membership drives both (a contained group user in SQL, a group principal in Cosmos DB).
- **PIM only, requiring Entra ID P2 or ID Governance in every tenant.** The template must work in tenants without them; the script mode keeps the same 4-hour limit, second person, ticket and alerts.
- **Activation without approval.** It removes the second person, the control that makes a single compromised operator account insufficient.
- **Role-assignable groups.** Only Global Administrators, Privileged Role Administrators and the owners could change their membership, and no other administrator could change their members' or owners' credentials. Not adopted: the three groups grant no Microsoft Entra role, so they stay ordinary security groups and the design relies on detection (an alert on every membership added and a severity-1 alert on a self-add) against changes made by directory administrators; the residual risk is stated under Consequences.
- **A shared break-glass account.** `AadPrincipalId` and the SQL audit would name the account, never the person.

## Consequences

- Every human access to user data is time-bound, approved by a second person, tied to a ticket, attributed to one principal and logged per request; conversations stay invisible to admins in the app.
- An operator can act only while an approver is reachable: on-call cover needs an approver as well as an operator.
- Script-mode memberships don't expire by themselves: the group owner runs `revoke`, and `docchat-alert-jit-overdue` reports any membership still present after 4 hours 15 minutes. PIM activations expire on their own.
- A token issued before a revocation stays usable until it expires, 60 to 90 minutes after issue; the review allows for that window.
- In the per-principal fallback every grant raises the severity-1 `docchat-alert-cosmos-data-access`, which the review matches to its ticket. A per-principal Cosmos DB assignment has no expiry of its own: `jit revoke` deletes it, and `jit list` shows any that remain, so the monthly review catches one left behind by a membership removed outside `jit revoke`.
- The groups aren't role-assignable, so Entra roles such as Groups Administrator and User Administrator can change their membership outside PIM and `docchat-ops`, and Authentication, Helpdesk and User Administrators can change their members' credentials; every membership change raises `docchat-alert-jit-grant`, and a self-add raises the severity-1 alert within 30 minutes.
- Obligations: the bootstrap unit test for `approver_is_operator`; the deployed group-principal test, the deployed test that the operator's read appears in `CDBDataPlaneRequests` with the operator's `AadPrincipalId` within 30 minutes and fires `human-conversation-access`, and the deployed test that a group owner adding themselves to `sg-docchat-dev-config-operators` raises `docchat-alert-jit-self-grant`; the runbooks for reading a conversation and the access review, user data export and deletion, ops-config changes and index overrides, and ingestion recovery.

## References

- What is PIM for Groups (any security group, role-assignable or not; approval, MFA, justification and maximum activation; who can manage non-role-assignable groups; at most 500 role-assignable groups): https://learn.microsoft.com/en-us/entra/id-governance/privileged-identity-management/concept-pim-for-groups (checked 2026-10-18)
- Configure PIM for Groups settings (activation 1 to 24 hours, ticket information, at least one approver and two for redundancy, no default approvers, changes outside PIM): https://learn.microsoft.com/en-us/entra/id-governance/privileged-identity-management/groups-role-settings (checked 2026-10-18)
- Cosmos DB data plane security reference (built-in data roles, distinct from Azure RBAC): https://learn.microsoft.com/en-us/azure/cosmos-db/reference-data-plane-security (checked 2026-10-14)
- Connect with role-based access control (scopes and the permissions to create an assignment): https://learn.microsoft.com/en-us/azure/cosmos-db/how-to-connect-role-based-access-control (checked 2026-10-14)
- CDBDataPlaneRequests table (`AadPrincipalId`): https://learn.microsoft.com/en-us/azure/azure-monitor/reference/tables/cdbdataplanerequests (checked 2026-10-14)
- CREATE USER (`TYPE = X` for Entra groups): https://learn.microsoft.com/en-us/sql/t-sql/statements/create-user-transact-sql?view=azuresqldb-current (checked 2026-10-14)
- Configurable token lifetimes (access tokens 60 to 90 minutes): https://learn.microsoft.com/en-us/entra/identity-platform/configurable-token-lifetimes (checked 2026-10-14)
- Integrate Microsoft Entra logs with Azure Monitor logs (the diagnostic setting, at least Security Administrator): https://learn.microsoft.com/en-us/entra/identity/monitoring-health/howto-integrate-activity-logs-with-azure-monitor-logs (checked 2026-10-18)
