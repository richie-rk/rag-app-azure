# ADR 0125: SQL contained users and SID source

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Architect
- **Specified in:** 83-infrastructure.md, 80-data-model.md, 82-security.md, 84-testing.md, 90-build-order.md
- **Related ADRs:** 0024, 0025, 0026, 0027, 0104, 0105, 0108, 0113, 0116, 0141

## Context

No SQL password may exist anywhere: not in an image, a configuration file, the repository or an app setting. The server therefore accepts only Microsoft Entra authentication, each app connects with an access token of its own user-assigned identity (ADR 0026), and each caller is its own database principal with rights through a role (ADR 0025). The managed identities' users are created with `WITH SID = …, TYPE = E`, which needs no directory lookup and so no Microsoft Graph permission on the server. Microsoft's pages disagree on whether that SID is the identity's client ID or its object ID, and a wrong SID creates a user that the identity's token never maps to, so every connection fails at login. A stage-0 test settles the question, and this ADR records the rule that turns its result into the decision.

Vendor facts (checked 2026-10-14):

- `CREATE USER [name] WITH SID = <binary(16)>, TYPE = E` (a user, service principal or managed identity) or `TYPE = X` (a group) works in Azure SQL Database, and the ID isn't validated against Entra ID. The `CREATE USER` page (updated 2026-09-21) says to use the client ID for service principals and user- or system-assigned managed identities and the object ID for users and groups, with the pattern `CONVERT(VARCHAR(MAX), CONVERT(VARBINARY(16), @guid), 1)` inside dynamic SQL.
- The user-assigned managed identity page says the same syntax creates users "without the needed Microsoft Graph permission" but that "you must supply the Object Id of the Microsoft Entra principal and use it as an SID"; the Entra overview describes a contained user's SID as the "Binary representation of the Microsoft Entra identity's object ID".
- The server's Entra administrator is one security object, a group allowed, whose members inherit it; Entra authentication for Azure SQL doesn't use Azure RBAC, so every identity needs a database principal.
- ODBC Driver 18 takes an access token through the pre-connect attribute `SQL_COPT_SS_ACCESS_TOKEN` (1256): a 4-byte length followed by the token in UTF-16-LE with no terminator, never combined with `UID`, `PWD` or `Authentication`.

## Decision

1. **No SQL login.** The server runs with `azureADOnlyAuthentication: true` (a Deny policy stops anyone re-enabling SQL authentication, ADR 0116). Its Entra administrator is the group `sg-docchat-<env>-sql-admins`, whose only member is the environment's deploy identity; that identity runs the migrations and the grants step as `dbo` and is no contained user.
2. **Five contained users, created by the grants step.** `scripts/deploy/sql_principals.py --env <env>`, run by the azd `postprovision` hook right after the migrations (ADR 0027), connects as the deploy identity with `docchat_core.sql.engine.connection_string("grants")` (which ends in `APP=docchat-grants`, so the privileged audit names its session, ADR 0108) and, idempotently:

   | Contained user | Created as | Role |
   |---|---|---|
   | `id-docchat-api-<env>` | `CREATE USER [id-docchat-api-<env>] WITH SID = 0x<32 hex>, TYPE = E` | `docchat_api` |
   | `id-docchat-ingest-<env>` | the same form | `docchat_ingestion` |
   | `sg-docchat-<env>-config-operators` | `CREATE USER [sg-docchat-<env>-config-operators] WITH SID = 0x<32 hex>, TYPE = X` | `docchat_config_operator` |
   | `sg-docchat-<env>-conversation-readers` | the same form | `docchat_conversation_reader` |
   | `sg-docchat-<env>-user-data-operators` | the same form | `docchat_user_data_operator` |

   It adds each user to its role with `ALTER ROLE <role> ADD MEMBER [<user>]`, removes every member of those five roles that isn't in this set, and drops and re-creates a user whose SID doesn't match its identity (an identity re-created under the same name).
3. **SID source.** `TYPE = E` users take the managed identity's **client ID**, the value the `CREATE USER` page prescribes, from the azd outputs `DOCCHAT_API_IDENTITY_CLIENT_ID` and `DOCCHAT_INGESTION_IDENTITY_CLIENT_ID`; `TYPE = X` users take the group's **object ID**. The script's option `--sid-source client-id|object-id` selects the identities' source (default `client-id`; `object-id` reads `DOCCHAT_API_IDENTITY_PRINCIPAL_ID` and `DOCCHAT_INGESTION_IDENTITY_PRINCIPAL_ID`), and it applies to `TYPE = E` users only.
4. **The SID literal.** DDL can't bind parameters, so the script validates every ID as a UUID before any SQL runs, builds the literal as `uuid.UUID(<id>).bytes_le.hex()` (equal to SQL Server's `CONVERT(VARBINARY(16), <uniqueidentifier>)` byte order) and takes its statement text from `docchat_core.sql.statements`. A failure stops the hook with `sql_principal_failed` before any app is deployed.
5. **Spike SP-2 settles the SID source at stage 0.** In `dev`, the script creates the ingestion identity's user from its client ID, and the stage-0 spike app, running as the ingestion identity, connects through the data-access layer with its token and runs `SELECT USER_NAME()`. It returns `id-docchat-ingest-dev` → the client ID is confirmed. It fails with login error 18456 → the script re-creates the user from the object ID and the spike repeats the query; success → `--sid-source` defaults to `object-id` from then on. The result, with the date and the error text if any, is written into this ADR before stage 1 starts; if neither form logs in, stage 0 doesn't pass and stage 1 doesn't start.

## Alternatives considered

- **`CREATE USER … FROM EXTERNAL PROVIDER`.** Rejected: the server would need a directory lookup, and with it a Microsoft Graph permission on its identity; the SID form needs neither.
- **The object ID as the default.** Not chosen: the `CREATE USER` page, the most specific and most recently updated source, prescribes the client ID for managed identities; the spike switches to the object ID if the platform disagrees.
- **SQL logins with passwords in Key Vault.** Rejected: a password is a stored secret that grants the login's rights to whoever reads it, and Entra-only authentication forbids SQL logins.
- **Adding the app identities to the admin group.** Rejected: members of the Entra administrator group hold every permission in the database, including schema changes, which app identities must never hold.
- **Granting rights to users rather than roles.** Rejected: rights belong to the five roles (ADR 0025), so re-creating a user never loses a grant.

## Consequences

- No SQL password exists anywhere: images, settings and the repository hold only server and database names, and every connection carries an Entra token.
- The server needs no Graph permission and makes no directory call; a wrong ID can only create a user that never logs in, which SP-2 detects at stage 0 and the deployed smoke tests detect in every environment.
- Re-creating a workload identity changes its client and object IDs; the next provision re-creates the user with the new SID, and the role-assignment conditions are re-tightened by runbook R23.
- Tests (84-testing.md): a unit test proves that the SID literal equals `CONVERT(VARBINARY(16), <uniqueidentifier>)` for five known pairs and that a non-UUID is rejected before any SQL runs; a deployed `dev` test proves that the compiled grant matrix matches `sys.database_permissions`, that a SQL user-name-and-password login fails while the deploy identity's token login succeeds, and that the privileged audit's rows for the grants step carry `ApplicationName` `docchat-grants`.

## References

- `CREATE USER` (Azure SQL Database): https://learn.microsoft.com/en-us/sql/t-sql/statements/create-user-transact-sql?view=azuresqldb-current (checked 2026-10-14)
- Managed identities with Azure SQL (SID guidance): https://learn.microsoft.com/en-us/azure/azure-sql/database/authentication-azure-ad-user-assigned-managed-identity?view=azuresql (checked 2026-10-14)
- Microsoft Entra authentication for Azure SQL: https://learn.microsoft.com/en-us/azure/azure-sql/database/authentication-aad-overview?view=azuresql (checked 2026-10-14)
- Service principals in Azure SQL (tutorial): https://learn.microsoft.com/en-us/azure/azure-sql/database/authentication-aad-service-principal-tutorial?view=azuresql (checked 2026-10-14)
- ODBC Driver and Microsoft Entra ID (access token attribute): https://learn.microsoft.com/en-us/sql/connect/odbc/using-azure-active-directory (checked 2026-10-14)
