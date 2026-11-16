# ADR 0108: SQL audit scope

- **Status:** Accepted
- **Date:** 2026-10-21; amended 2026-11-07 (PSRule checks the privileged audit with the custom rule `DocChat.SQL.Auditing`, and the built-in `Azure.SQL.Auditing` is suppressed on `sql-docchat-*`, ADR 0106)
- **Decided by:** Architect
- **Specified in:** 83-infrastructure.md, 82-security.md, 85-observability.md, 84-testing.md, 90-build-order.md
- **Related ADRs:** 0014, 0024, 0025, 0027, 0029, 0107, 0115, 0125, 0139, 0141

## Context

Azure SQL audits to Log Analytics, and the product's logs never hold names, email addresses or file names. The two pull against each other: the API and ingestion identities bind exactly those values as statement parameters, while the deploy identity (migrations, grants) and the just-in-time operator groups must leave a record of what they ran that their author can't remove. An operator's `audit_events` row is written by the operator's own `docchat-ops` process, so it can be skipped or forged; the deploy identity is a privileged operator of its environment (ADR 0115), bounded by records outside its reach.

Vendor facts:

- `BATCH_COMPLETED_GROUP` "audits the entire batch or stored procedure text, as sent from the client, including the result"; when the query processor parameterises a query, the audit record can show the parameter marker in place of the column value; whether bound parameter values ever reach the audit log isn't documented. The record's statement field is `nvarchar(4000)`.
- `Microsoft.Sql/servers/extendedAuditingSettings` and `servers/databases/extendedAuditingSettings` (API 2025-01-01, name `default`) add `predicateExpression`, the condition of the audit's where clause; `queueDelayMs` is at least 1,000; `isDevopsAuditEnabled` adds the `DevOpsOperationsAudit` category. Server-level auditing applies to every database; its records reach Log Analytics only through a diagnostic setting with category `SQLSecurityAuditEvents` on the `master` database, which the Azure Verified Module doesn't create.
- "When using Microsoft Entra authentication, failed logins records don't appear in the SQL audit log"; they appear in the Entra sign-in logs.

## Decision

1. **Privileged audit (server level).** `Microsoft.Sql/servers/extendedAuditingSettings@2025-01-01` named `default` on `sql-docchat-<env>-<token>`: `{state: 'Enabled', isAzureMonitorTargetEnabled: true, isDevopsAuditEnabled: true, queueDelayMs: 1000, auditActionsAndGroups: ['BATCH_COMPLETED_GROUP', 'SUCCESSFUL_DATABASE_AUTHENTICATION_GROUP', 'FAILED_DATABASE_AUTHENTICATION_GROUP'], predicateExpression: "database_principal_name <> 'id-docchat-api-<env>' AND database_principal_name <> 'id-docchat-ingest-<env>'"}`. It records every statement of every principal except the two workload identities: the deploy identity's migrations and grants, the just-in-time groups' sessions and anyone else.
2. **Where the privileged audit goes.** Two diagnostic settings on the `master` database, each with categories `SQLSecurityAuditEvents` and `DevOpsOperationsAudit`: `sql-audit` to the environment's workspace, declared in `main.bicep`; and `sql-audit-security` to `log-docchat-security`, created by the bootstrap operator's `scripts/bootstrap/tighten_conditions.py` after the first provision of `dev` and of `prod` (ADR 0115), so no template change removes it, and deleting it raises `docchat-alert-diagnostic-settings`. `load` has `sql-audit` only.
3. **Workload audit (database level).** `Microsoft.Sql/servers/databases/extendedAuditingSettings@2025-01-01` named `default` on `docchat`: `{state: 'Enabled', isAzureMonitorTargetEnabled: true, queueDelayMs: 1000, predicateExpression: "database_principal_name = 'id-docchat-api-<env>' OR database_principal_name = 'id-docchat-ingest-<env>'", auditActionsAndGroups: <the set item 4 chooses>}`, sent through category `SQLSecurityAuditEvents` of the `docchat` database's `to-log-analytics` setting to the environment's workspace. The two predicates are complements, so each event is recorded once.
4. **The stage-0 sentinel rule.** Under the deploy identity, run `SELECT COUNT(*) FROM app.users WHERE display_name = ?` with the bound value `docchat-audit-sentinel-<uuid4>`; 20 minutes later query `SQLSecurityAuditEvents | where Statement has '<sentinel>' or AdditionalInformation has '<sentinel>'`.
   - Zero rows (bound values don't reach the log): the workload audit's set is `BATCH_COMPLETED_GROUP`, `SUCCESSFUL_DATABASE_AUTHENTICATION_GROUP` and `FAILED_DATABASE_AUTHENTICATION_GROUP`.
   - One or more rows: the workload audit's set is `SCHEMA_OBJECT_CHANGE_GROUP`, `DATABASE_OBJECT_CHANGE_GROUP`, `DATABASE_PRINCIPAL_CHANGE_GROUP`, `DATABASE_ROLE_MEMBER_CHANGE_GROUP`, `DATABASE_PERMISSION_CHANGE_GROUP`, `DATABASE_OBJECT_PERMISSION_CHANGE_GROUP` and the two authentication groups.
   - The privileged audit keeps `BATCH_COMPLETED_GROUP` in both outcomes. The result and the date go into this ADR before stage 1 starts.
5. **The principal names.** The same stage-0 run checks that `database_principal_name` carries the contained users' names: the ingestion identity's successful authentication (its `SELECT USER_NAME()` connection, ADR 0125) appears in the workload audit only, and the deploy identity's sentinel statement in the privileged audit only. When the records show another value for a workload identity, both predicates use the value the records show, and the check is repeated.
6. **Attribution.** Every SQL session states its component through the ODBC keyword `APP` (`docchat-api`, `docchat-ingestion`, `docchat-migrations`, `docchat-grants`, `docchat-ops`), so each audit record's application name says which component ran the statement.
7. **Readers and retention.** Operators read both workspaces through Log Analytics Reader; the security alerts read `log-docchat-security`. In `prod`, `SQLSecurityAuditEvents` keeps 90 days of analytics and 365 days in total (ADR 0107); `log-docchat-security` keeps 365 days.

## Alternatives considered

- **One server-level audit of every principal with `BATCH_COMPLETED_GROUP`.** Rejected: if bound values reach the log, every user's display name, email address and file name the apps bind would be logged, against the logging policy.
- **Change-groups only, for every principal.** Rejected: privileged reads (an operator querying user data or ops config) would leave no record.
- **Operators' `audit_events` rows as the only record.** Rejected: the operator's own process writes them and can skip or forge them; the privileged audit records the statements that process actually ran.
- **Predicates on the server principal name.** Rejected: the contained users' database principal names are the stable, template-defined names the predicates can state; item 5 confirms them on the platform.
- **The privileged audit only in the security workspace.** Rejected: `sql-audit-security` exists only after the post-provision step, and `load` never runs it, so the first provision and every `load` run would audit into nothing.

## Consequences

- Every statement of the deploy identity, the just-in-time groups and any other principal is recorded with up to 4,000 characters of batch text, bound values included. Names, email addresses or file names that an operator binds therefore reach both workspaces: that is the accepted price of a trustworthy record of privileged access. Operators read both; the environment's deploy identity can read the environment's workspace through Contributor; `prod` keeps the records 365 days.
- Failed Entra sign-ins to SQL appear in `log-docchat-security`'s Entra sign-in logs, not in the SQL audit.
- PSRule for Azure's `Azure.SQL.Auditing` looks only for `Microsoft.Sql/servers/auditingSettings`, which this decision doesn't declare: item 1 needs `extendedAuditingSettings` for its `predicateExpression`. On 2026-11-07 the product owner decided that a suppression of that rule on `sql-docchat-*` names this ADR, and that the custom rule `DocChat.SQL.Auditing` checks item 1 instead: exactly one server-level `extendedAuditingSettings` named `default`, with `state: 'Enabled'`, `isAzureMonitorTargetEnabled: true` and a non-empty `predicateExpression` (83-infrastructure.md sections 13.1, 13.3 and 13.4).
- In the "rows" outcome of item 4 the workload identities' reads and writes aren't audited in SQL; their actions stay visible in application telemetry and in `audit_events` (ADR 0029). Their roles can't change schema or permissions, so the change groups normally record only their logins.
- Tests (84-testing.md, deployed `dev`): a `SELECT` by a member of `sg-docchat-dev-config-operators` appears with its statement text in `log-docchat-security` within 20 minutes; a statement by the API identity never appears in the privileged audit; the grants step's rows carry application name `docchat-grants` and the migrations' rows `docchat-migrations`; a SQL login with a user name and password fails, and the deploy identity's token login appears in `SQLSecurityAuditEvents` within 20 minutes; stage 0 runs items 4 and 5.

## References

- SQL Server audit action groups (`BATCH_COMPLETED_GROUP`, parameterised queries): https://learn.microsoft.com/en-us/sql/relational-databases/security/auditing/sql-server-audit-action-groups-and-actions (checked 2026-10-14)
- `extendedAuditingSettings` (API 2025-01-01, `predicateExpression`): https://learn.microsoft.com/en-us/azure/templates/microsoft.sql/2025-01-01/servers/extendedauditingsettings (checked 2026-10-14)
- `auditingSettings` (`queueDelayMs`, `isDevopsAuditEnabled`, the `master` diagnostic setting): https://learn.microsoft.com/en-us/azure/templates/microsoft.sql/2025-01-01/servers/auditingsettings (checked 2026-10-14)
- Auditing overview (Entra failed logins, server and database levels): https://learn.microsoft.com/en-us/azure/azure-sql/database/auditing-overview?view=azuresql (checked 2026-10-14)
- Auditing setup for Log Analytics: https://learn.microsoft.com/en-us/azure/azure-sql/database/auditing-setup?view=azuresql (checked 2026-10-14)
