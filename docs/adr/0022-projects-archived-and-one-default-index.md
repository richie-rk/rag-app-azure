# ADR 0022: Projects archived and one default index

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decided by:** Product owner and architect
- **Specified in:** 10-web-client.md, 11-api.md, 14-ingestion.md, 80-data-model.md, 84-testing.md
- **Related ADRs:** 0012, 0020, 0021, 0029, 0052, 0094

## Context

The product owner made projects and indexes many-to-many: normally one index per project, but one index can serve several projects. Callers never name an index: the API resolves the caller's unexpired override, else the project's default mapping (ADR 0020). A project with mappings but no default would leave chat unable to choose, and a project whose default index has failed would break chat for all its members. Sessions store their `projectId` and audit rows carry `project_id` without a foreign key, so a deleted project row would leave history pointing at nothing. Several admins can work on one project at the same time, and cross-origin scripts can't read `ETag` headers, so versions travel in response bodies.

The product needs:

- Admins can create and edit projects, and take one out of use at once without losing its history.
- A project exists as its own row whatever its membership, and admins see every project.
- Chat always finds exactly one default index for a project that has any mapping.
- Two admins on stale screens can't silently overwrite each other's membership or default-index change.

Facts that bound the design:

- A filtered unique index is allowed on a nonclustered index, and only the rows that match its filter must be unique; the filter allows simple comparisons with constants only, so a statement must carry the literal for SQL Server to match it.
- `rowversion` is 8 bytes and changes on every insert or update of its row; the documented optimistic-concurrency pattern is `UPDATE … WHERE key = @k AND rv = @rv`, then check the affected row count.
- Two admins can hold the same screen: admin A makes index X the default while admin B, on the earlier screen, makes index Y the default. B's request touches Y and whichever row is the default now, so no per-row version on the mapping rows can show B that A acted.

## Decision

1. **Archive and restore, never delete.** No operation deletes a project. `adminUpdateProject` with `{"status": "archived"}` sets `status = N'archived'`, `archived_at`, `archived_by` (the admin's `oid`) and `is_default = 0` in one statement; `{"status": "active"}` on an archived project restores it, clearing `archived_at` and `archived_by` while `is_default` stays `0`. A body that changes `status` holds no other field (`422 validation_failed` with `{path: "body.status", reason: "not_allowed"}`). Constraints: `CK_projects_status` (`IN (N'active', N'archived')`), `CK_projects_default_active` (`is_default = 0 OR status = N'active'`) and `CK_projects_archive_fields` (archive columns both set exactly when archived).
2. **An archived project is read-only.** On an archived project only the restore body is accepted; any other change to it, its members or its mappings gets `409 project_archived`. Memberships and mappings survive archiving, so a restore brings everything back. `UQ_projects_name` (case-insensitive under `SQL_Latin1_General_CP1_CI_AS`) includes archived projects.
3. **Archived means unreachable on every user path.** Chat, citations, session lists and reads, feedback and the project picker treat an archived project as absent, for admins too; its sessions disappear from the next request and come back on restore. Admin routes list (`status` filter `active`, `archived` or `all`), read and restore it. Projects are their own rows, independent of memberships, and admins reach every project without a membership row.
4. **One default index per mapped project.** A project with at least one mapping has exactly one default mapping; a project with none has none. `UX_project_indexes_default (project_id) WHERE is_default = 1` allows at most one; the operations guarantee at least one:
   - `adminMapProjectIndex` (`PUT /v1/admin/projects/{projectId}/indexes/{indexId}`, body `{"isDefault": boolean}`) creates or updates one mapping. A project's first mapping must have `isDefault: true`, else `409 default_index_required`. `isDefault: true` on a non-default mapping clears the current default in the same transaction, old row first, so the unique index never sees two. `isDefault: false` on the current default gets `409 default_index_required`.
   - `adminUnmapProjectIndex` (`DELETE`) gets `409 default_index_in_use` when the mapping is the default and other mappings exist; the only mapping can always be removed. Removing a mapping deletes the overrides that point at it, through the cascading composite foreign key `FK_user_index_overrides_mapping`.
5. **Only an index that can serve is mappable.** The logical index must exist and not be `deleted` (`404 index_not_found`, the same answer as `adminGetIndex`), and its status must be `provisioning`, `ready`, `reingest_required`, `reingesting` or `rebuilding`; `failed` and `deleting` get `409 index_not_mappable`. `project_indexes.search_index_id` references `app.search_indexes (id)` with no cascade, and ingestion refuses to delete an index that any project maps (`409 index_mapped`, ADR 0052).
6. **The project's version guards its members and mappings.** `adminUpdateProject`, `adminAddProjectMember`, `adminRemoveProjectMember`, `adminMapProjectIndex` and `adminUnmapProjectIndex` require `If-Match: "<rowVersion>"` with the project's `rowVersion`: the 8-byte `ROWVERSION` as standard base64 with padding, 12 characters, `^[A-Za-z0-9+/]{11}=$`. A missing header, `*`, a weak tag, a list or any other form gets `428 precondition_required`; a well-formed tag that isn't current gets `412 precondition_failed`. Each operation reads the project row under `UPDLOCK, ROWLOCK`, makes its other lookups, compares the version, applies its rules and, when it changes anything, runs `UPDATE app.projects SET … updated_at = SYSUTCDATETIME(), updated_by = @actor OUTPUT inserted.row_version WHERE id = @projectId AND row_version = @rowVersion` in the same transaction (membership and mapping changes set only those two columns); zero rows updated triggers one re-read that decides between `404` and `412`. A call that changes nothing (an existing membership, an unchanged mapping, an absent row to remove, a `PATCH` equal to the stored values) moves nothing and returns the current version. Check order: `404 project_not_found`, then `404 user_not_found` or `404 index_not_found`, then `428` and `412`, then `409 project_archived`, then the state rules. Only creates (`adminCreateProject`) carry no `If-Match`.
7. **Responses.** `adminAddProjectMember` answers `201` (created) or `200` (already a member) with `ProjectMemberChange`; `adminMapProjectIndex` answers `201` (created) or `200` (updated or unchanged) with `ProjectIndexMappingChange`; both removals answer `200 ProjectPartRemoval` with `removed` true or false. Each carries `projectRowVersion`, the project's `rowVersion` after the call.
8. **Client contract.** The admin screens read `adminGetProject` before the member and mapping lists, so the version sent is never newer than what is shown; keep each returned `projectRowVersion`, so one admin's consecutive changes chain; offer only mappable indexes in the map dialog; and answer `412` with the reload flow (ADR 0094).
9. **Audit rows** in the change's own transaction, only when a row changed: `project.archived` (`wasDefault`), `project.restored`, `project_member.added`, `project_member.removed` (`grantSource`), `project_index.mapped` (`isDefault`), `project_index.default_set` (`previousDefaultIndexId`), `project_index.unmapped` (`wasDefault`, `overridesRemoved`).

## Alternatives considered

- **Deleting projects with cascades.** Sessions and audit rows would point at nothing, and a removal by mistake couldn't be undone. Archiving removes a project from use at once and keeps everything a restore needs.
- **A terminal `deleted` status.** It adds a state that does less than `archived`; archive and restore already cover retiring a project.
- **Editable archived projects.** Changes to a project nobody can use are invisible until the restore, which would then bring back something other than what was archived. Read-only keeps the archived project as it was.
- **The default as a column on `projects` (`default_search_index_id` with a composite foreign key to the mapping).** It splits one fact across two tables and needs its own clearing rule when the last mapping goes; the filtered unique index keeps the default on the mapping it belongs to.
- **Per-row versions on membership and mapping rows.** They can't catch admin B's stale default change (B's request touches rows that A's change didn't version for B), and an add creates a row that has no version to compare. The project's version covers both.
- **No precondition on membership and mapping changes, since `PUT` and `DELETE` are idempotent.** The last writer would win silently, which is exactly the overwrite this decision exists to stop.
- **Allowing `failed` or `deleting` indexes to be mapped.** A project could gain a default that can't serve, and its members' chat would fail with `409 project_index_unavailable`.

## Consequences

- Admins can take a project out of use at once and bring it back intact; history keeps resolving to a real project row, and a project exists whatever its membership.
- An archived project keeps its name: to reuse the name, restore that project, rename it, and archive it again.
- An index that an archived project maps can't be deleted (`409 index_mapped`) until the project is restored and the mapping removed.
- Every membership or mapping change makes another admin's pending edit of the same project stale, which costs that admin one reload; this cost is accepted. Different projects never contend.
- The last mapping can be removed: the project then has no default, chat answers `409 project_index_unavailable`, and `listMyProjects` reports `hasDefaultIndex: false`, which is true only while the default mapping's index has an active physical index.
- Removing a mapping silently removes the overrides on it; the audit row counts them in `overridesRemoved`.
- Obligations: integration tests (two concurrent `adminMapProjectIndex` calls with the same `If-Match` that make different indexes default: exactly one succeeds, the other gets `412`, one default remains, and a direct insert of a second default row fails; for each of the four membership and mapping operations, no `If-Match` gives `428`, a stale version `412` with nothing changed, and the current one a new `projectRowVersion` equal to what `adminGetProject` returns next; the admin A and admin B scenario; an archived project refused on every chat-path check for admins and members, restored with its memberships; the `409` codes of rules 2, 4 and 5; the `CHECK` constraints); the web client's tests of the reload flow and the mappable filter.

## References

- CREATE INDEX (filtered unique indexes, their predicate rules and required SET options): https://learn.microsoft.com/en-us/sql/t-sql/statements/create-index-transact-sql?view=azuresqldb-current (checked 2026-10-14)
- rowversion (8 bytes, changes on every insert or update; the optimistic-concurrency pattern): https://learn.microsoft.com/en-us/sql/t-sql/data-types/rowversion-transact-sql (checked 2026-10-14)
- Error 1785 (one table may not appear twice in a cascade chain): https://learn.microsoft.com/en-us/sql/relational-databases/errors-events/mssqlserver-1785-database-engine-error (checked 2026-10-14)
