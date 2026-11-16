# ADR 0021: First sign-in provisioning

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decided by:** Product owner and architect
- **Specified in:** 10-web-client.md, 11-api.md, 80-data-model.md, 82-security.md, 84-testing.md, 85-observability.md
- **Related ADRs:** 0015, 0017, 0020, 0022, 0025, 0026, 0029, 0030, 0033, 0091, 0143

## Context

Microsoft Entra ID is the only way into DocChat, and admin status comes only from the `Admin` app role in the validated token (ADR 0017). The product still needs its own record of each person: project memberships reference a user, admins search for people to add to projects, and the API holds no Microsoft Graph permission, so it can't look anyone up in the directory. The product owner set the rule for newcomers: they join every project flagged default when they first sign in, and admins grant the rest.

The record has to meet these needs:

- Identity is the Entra object ID, never an e-mail address or part of one, because addresses change, get reissued to other people, and can share a local part across domains.
- Nothing a caller sends can choose their role or their projects: the role comes from the token, and the first memberships come from the default flags.
- No credential of any kind is stored, because Entra ID is the only sign-in method.
- Which projects a newcomer joins is data (the `is_default` flag), never code.
- A person with no project gets a plain next step, never an error.

Facts that bound the mechanism:

- The `oid` claim is an immutable GUID; `name` and `preferred_username` are mutable and not for authorisation.
- Azure SQL Database runs with `READ_COMMITTED_SNAPSHOT` on by default, so a plain read takes no lock and two concurrent read-then-insert transactions can both see no row.
- `HOLDLOCK` is equivalent to `SERIALIZABLE` for the table it names, for the duration of the transaction, and `UPDLOCK` takes update locks held until the transaction completes.
- A page load sends about 10 requests at once, often to different workers and instances, so a person's first requests arrive concurrently.
- Every authenticated request passes this step, so a known user must cost at most one indexed `UPDATE`, and usually nothing.

## Decision

1. **Where it runs.** `docchat_core.users.ensure_user(principal)` runs on every authenticated `/v1` request at step 5 of the check order: after token validation, the role check and, where they apply, the burst limit, the admin check and the write limit, and before the body is read. Its only input is the validated `Principal`; no path, query, body or header value identifies the user.
2. **Throttle.** Each process keeps a map keyed by `oid` (at most 50,000 entries, oldest evicted first). When it shows that the caller's row was written or confirmed less than 15 minutes ago, the step is skipped. The map holds no access decision; losing it costs one more write.
3. **One transaction** (`SET XACT_ABORT ON`), statements in this order:
   - `UPDATE app.users WITH (UPDLOCK, HOLDLOCK)` for `object_id = :oid`, setting `tenant_id` from `tid`, `display_name` and `email` only when the token's value is non-empty, `last_known_role`, and `last_seen_at = SYSUTCDATETIME()`, never earlier than `first_seen_at` (amended 2026-11-08: the statement's time is read before it waits for a concurrent first sign-in's insert, so it is raised to `first_seen_at` when earlier).
   - When the update touched no row: insert the `app.users` row; insert one `app.project_members` row (`grant_source = N'default'`, `granted_by = NULL`) for every project whose `is_default = 1` at that moment, read through the filtered index `IX_projects_default (id) WHERE is_default = 1` with the literal `1` in the statement text so SQL Server can match the filtered index (a default project is always active, by `CK_projects_default_active`); insert the audit row `user.provisioned` (`actor_type` `user`, actor and subject the caller's `oid`, `target_type` `user`, `target_id` the `oid` text, `metadata_json` `{"defaultProjectCount": <rows inserted>}`, the request's `correlation_id`).
   - Commit, then record the `oid` in the throttle map.
   The key-range lock that `HOLDLOCK` takes for the absent key serialises two first requests of one person: the second waits for the first to commit, then updates.
4. **Failures.** Deadlock `1205` or duplicate key `2627` or `2601` rolls back and retries the whole transaction once. A second failure, or SQL unreachable after the data-access layer's retries (ADR 0026), answers `503 dependency_unavailable` with `retryAfterSeconds` 5 and leaves the throttle map unchanged.
5. **Claims are display data.** `display_name` comes from `name`: Unicode NFC, code points of categories Cc, Cf, Cs, Co and Cn removed, trimmed, cut to 256 characters, `''` when absent. `email` comes from the `email` claim, else from `preferred_username` when it holds exactly one `@`, no whitespace and no `#`; trimmed, at most 320 characters, else `''`. `last_known_role` is `Admin` when `roles` contains `Admin`, else `User`. No decision reads any of the three, `getCurrentUser` returns identity values from the token and never from the row, and no log record or span carries a name or e-mail address.
6. **`app.users`:** `object_id UNIQUEIDENTIFIER` primary key `PK_users` (clustered); `tenant_id UNIQUEIDENTIFIER NOT NULL`; `display_name NVARCHAR(256) NOT NULL DEFAULT N''`; `email NVARCHAR(320) NOT NULL DEFAULT N''`; `last_known_role NVARCHAR(32) NOT NULL` with `CK_users_last_known_role` (`IN (N'Admin', N'User')`); `first_seen_at` and `last_seen_at` `DATETIME2(3) NOT NULL DEFAULT SYSUTCDATETIME()` with `CK_users_seen_order` (`last_seen_at >= first_seen_at`); indexes `IX_users_display_name (display_name, object_id)` and `IX_users_email (email, object_id)`. No credential column of any kind and no `row_version`, because admins never edit these rows. Writers: this flow, and the user-data deletion command (ADR 0033).
7. **Default flags set later.** Flagging a project default affects only people who sign in for the first time afterwards. An existing user joins it only when an admin adds them (`adminAddProjectMember`).
8. **Who can be found.** `users` rows exist only for people who have signed in at least once, so `adminSearchUsers` and `adminAddProjectMember` (`404 user_not_found` otherwise) reach only them. A newcomer whose first sign-in finds no default project sees the no-projects state, a plain message asking them to contact an admin, and the admin then finds and adds them.
9. **Telemetry.** Span `docchat.users.ensure` with `docchat.users.outcome` (`skipped`, `touched`, `provisioned`, `failed`) and, when provisioned, `docchat.users.default_project_count`; counter `docchat.users.provisioned`; custom event `user_provisioned` (`oid`, `defaultProjectCount`). Alert `user-provisioning-failures` (severity 2, 5-minute window evaluated every 5 minutes, incident-response runbook): failed `docchat.users.ensure` spans above 1% of API requests other than `GET /healthz`, with at least 50 requests in the window.

## Alternatives considered

- **A registration call that the client makes once.** A separate write route can be skipped, repeated or raced by the client, leaves a signed-in person without a row until it succeeds, and adds a write surface that must itself be protected from role or project tampering. Provisioning inside the authenticated request path needs no client step.
- **Directory lookups or a scheduled sync through Microsoft Graph.** The API would need Microsoft Graph application permissions, which its least-privilege identity doesn't hold, and a sync stores people who never use the product.
- **Keying users by e-mail address or user principal name.** Both are mutable, Microsoft says not to authorise on them, and a shared local part would merge two people. The object ID is immutable and unique in the tenant.
- **Insert first and treat a duplicate key as "already provisioned".** It is correct only if the membership and audit inserts share the transaction and the whole unit retries, and it turns every concurrent first request into an error path. The update-first form makes the common case one `UPDATE` and the rare race a short wait; the duplicate-key retry stays as a safety net.
- **One `MERGE` statement.** It still needs `HOLDLOCK` to be safe under concurrency, and the membership and audit inserts would hang off its `OUTPUT` clause. The explicit update-then-insert is easier to read, test and keep correct.
- **Re-applying default projects at every sign-in.** It would re-add people an admin removed, and the product owner's rule is that flagging a project default later doesn't change existing users.
- **Writing `last_seen_at` on every request.** One SQL write per request for a display value; the throttle bounds it to one write per user per process per 15 minutes.

## Consequences

- Every person who reaches the API with a valid role has exactly one `users` row, keyed by the object ID that also keys the Cosmos DB partitions (ADR 0030) and the audit columns (ADR 0029), and holds the default memberships of their first moment.
- Admin status can't be set through the app at all; the stored role is a label in admin lists.
- The `users` table is the admins' directory: a person appears only after their first sign-in, so a newcomer is assigned in Entra ID, signs in once, and is then added to projects by an admin.
- A person who signs in while no project is flagged default reaches the no-projects state (ADR 0091), never an error.
- SQL is a dependency of the first request in each 15-minute window per process: while SQL is unreachable those requests answer `503 dependency_unavailable`, and requests whose `oid` is in the throttle map skip the step. The access checks of ADR 0020 read SQL on the same requests anyway.
- Display names and e-mail addresses refresh at most every 15 minutes per process and only from tokens that carry them; an empty claim never erases a stored value.
- A user whose data was deleted (ADR 0033) would be provisioned again, with that moment's default projects, if they signed in again; the deletion runbook removes the person's app assignments first, so no token can be issued.
- Obligations: integration tests against the SQL Server container (32 concurrent first requests for one `oid` create one `users` row, one membership per default project and one `user.provisioned` row; first requests of 50 different users at once all succeed, deadlocks resolved by the single retry; a project flagged default after a user's first request doesn't add that user, while the next new user gets it; the `CHECK` constraints reject invalid rows); the plan test that every provisioning statement uses only index seeks; the `user-provisioning-failures` alert and its runbook.
- Risk: anyone the tenant administrator assigns to the API joins every default project at first sign-in. Admins control that exposure through the default flag (ADR 0022), and tenant administrators through app assignment.

## References

- Access token claims reference (`oid` immutable; `name` and `preferred_username` mutable and not for authorisation): https://learn.microsoft.com/en-us/entra/identity-platform/access-token-claims-reference (checked 2026-10-14)
- SET TRANSACTION ISOLATION LEVEL (`READ_COMMITTED_SNAPSHOT` on by default in Azure SQL Database): https://learn.microsoft.com/en-us/sql/t-sql/statements/set-transaction-isolation-level-transact-sql (checked 2026-10-14)
- Table hints (`HOLDLOCK` equivalent to `SERIALIZABLE` for the table it names and the duration of the transaction; `UPDLOCK` held until the transaction completes): https://learn.microsoft.com/en-us/sql/t-sql/queries/hints-transact-sql-table?view=azuresqldb-current (checked 2026-10-18)
- CREATE INDEX (filtered indexes and their predicate rules): https://learn.microsoft.com/en-us/sql/t-sql/statements/create-index-transact-sql?view=azuresqldb-current (checked 2026-10-14)
