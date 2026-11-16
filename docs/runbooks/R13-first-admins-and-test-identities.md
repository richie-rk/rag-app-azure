# R13 — First admins and test identities

**Performer:** per part, as stated: a User Administrator, a Privileged Role Administrator, the tenant administrator, a Conditional Access Administrator, a rotating operator and a group owner, each for its part (`86-operations.md` section 1.1).

**Trigger:** a new tenant (parts A and C), the first provision of an environment or a re-created registration (part B), `role-not-assigned` and access requests (part D). On a new tenant the parts run in the order A, then B for `dev`, then C, then B for `prod`, because part B in `dev` names the test groups and part C's policy names `dev`'s applications.

**Prerequisites:** the operator workstation of `86-operations.md` section 1.5; the test-user aliases of `testUserAliases` (`81-config.md` section 4.7) and the group members of `84-testing.md` section 6.

Placeholders in angle brackets are filled in at run time. A `403` from Microsoft Graph means the step's role is missing: hand the step to a holder; earlier steps stay in place.

## Part A — Test identities, once per tenant

A User Administrator for users and groups, a Privileged Role Administrator for the administrative unit and the role assignment (`86-operations.md` section 1.1). Microsoft Learn names Privileged Role Administrator for creating an administrative unit and adding members to it, confirmed 2026-10-25.

1. The Privileged Role Administrator creates the administrative unit:

   ```bash
   az rest --method POST --url https://graph.microsoft.com/v1.0/directory/administrativeUnits --body '{"displayName": "DocChat test users", "description": "Automated test accounts for DocChat non-production environments"}'
   ```

   Expected: the new unit; note the unit ID.

2. For each alias of `testUserAliases`, the User Administrator creates the user, where `<name>` is `docchat-test-<alias>` for the `dev` set and `docchat-<alias>` for the `load` set (`docchat-load-01` to `docchat-load-20`, `docchat-load-admin`):

   ```bash
   az ad user create --display-name "DocChat test <alias>" --user-principal-name <name>@<initial domain> --password <random, discarded> --force-change-password-next-sign-in false
   ```

   Then the Privileged Role Administrator adds the user to the unit:

   ```bash
   az rest --method POST --url "https://graph.microsoft.com/v1.0/directory/administrativeUnits/<unit ID>/members/\$ref" --body '{"@odata.id": "https://graph.microsoft.com/v1.0/users/<user ID>"}'
   ```

3. The User Administrator creates the groups `sg-docchat-dev-test-users`, `sg-docchat-dev-test-admins`, `sg-docchat-dev-test-norole`, `sg-docchat-load-test-users` and `sg-docchat-load-test-admins`, and adds the members `84-testing.md` section 6 assigns to each:

   ```bash
   az ad group create --display-name <name> --mail-nickname <name>
   az ad group member add --group <name> --member-id <user ID>
   ```

   Part B reads their object IDs with:

   ```bash
   az ad group show --group <name> --query id -o tsv
   ```

4. The Privileged Role Administrator gives each rotating operator Password Administrator on the unit only:

   ```bash
   az rest --method POST --url https://graph.microsoft.com/v1.0/roleManagement/directory/roleAssignments --body '{"principalId": "<operator object ID>", "roleDefinitionId": "966707d0-3269-4727-9be2-8c3a10f19b9d", "directoryScopeId": "/administrativeUnits/<unit ID>"}'
   ```

## Part B — Every environment, after its first provision

The tenant administrator: Cloud Application Administrator or Application Administrator (`86-operations.md` section 1.1), in `sg-docchat-operators` for step 1's refresh (`83-infrastructure.md` section 7.5).

1. Sign in and refresh the azd environment:

   ```bash
   az login --tenant <tenant ID>
   azd env refresh -e <env> --no-prompt
   ```

2. Run the post-provision identity script:

   ```bash
   uv run python scripts/identity_post_provision.py --environment <env> --resource-token <DOCCHAT_RESOURCE_TOKEN> \
     --users-group <sg-docchat-<env>-users object ID> --admins-group <sg-docchat-<env>-admins object ID> \
     [--admin <object ID> …]
   ```

   In `dev` add the three test groups:

   ```bash
   uv run python scripts/identity_post_provision.py --environment dev --resource-token <DOCCHAT_RESOURCE_TOKEN> \
     --users-group <sg-docchat-dev-users object ID> --admins-group <sg-docchat-dev-admins object ID> \
     [--admin <object ID> …] \
     --test-users-group <sg-docchat-dev-test-users object ID> --test-admins-group <sg-docchat-dev-test-admins object ID> \
     --test-norole-group <sg-docchat-dev-test-norole object ID>
   ```

   The script refuses the test groups in `prod` and `--test-norole-group` outside `dev` with exit 1 before any Graph call; a `load` run's test groups are assigned at its gate (R11 step 4). Expected: exit 0 and a summary with the run's correlation ID.

3. Exit 2 naming the no-role step means Microsoft Graph refused the default-access assignment of `sg-docchat-dev-test-norole` on the API after every other step was done: a repository administrator records the skip:

   ```bash
   gh variable set DOCCHAT_TEST_NOROLE_SKIP_REASON --env dev-tests --body graph_default_access_refused
   ```

   Any other non-zero exit: fix the cause it names and run the script once more.

4. Verify: a first admin signs in and `GET /v1/me` returns `isAdmin: true`; a second run of the script changes nothing.

## Part C — Conditional Access and first passwords, once per tenant after part B in `dev`

A Conditional Access Administrator, then a rotating operator.

1. Add the five test groups to `conditions.users.excludeGroups` of every tenant policy that requires multi-factor authentication for all users:

   ```bash
   az rest --method PATCH --url https://graph.microsoft.com/v1.0/identity/conditionalAccess/policies/<policy ID> --body @<file>
   ```

2. Create the policy `DocChat test users: non-production APIs only`:

   ```bash
   az rest --method POST --url https://graph.microsoft.com/v1.0/identity/conditionalAccess/policies --body @<file>
   ```

   The body sets `displayName`, `state: "enabled"`, `conditions.users.includeGroups` = the five groups, `conditions.applications.includeApplications: ["All"]`, `conditions.applications.excludeApplications` = the `dev` API and `dev` test-client application IDs, `conditions.clientAppTypes: ["all"]` (Microsoft Graph refuses the request with `400` without it) and `grantControls: {"operator": "OR", "builtInControls": ["block"]}`.

3. Set both sets' first passwords with R08 (`--set all`), then run the `dev` smoke suite:

   ```bash
   gh workflow run deployed-tests.yaml -f environment=dev -f suite=smoke
   ```

   Expected: the run passes (its `norole` check reports skipped when part B step 3 recorded a reason).

## Part D — Day-to-day access

1. People get access by joining `sg-docchat-<env>-users` or `sg-docchat-<env>-admins`, whose owners (the people in `groupOwners`, `86-operations.md` section 1.1) add them:

   ```bash
   az ad group member add --group <group> --member-id <object ID>
   ```

   Project access is granted in the admin Projects screen (`adminAddProjectMember`), and new users join every default project at their first sign-in.

2. `role-not-assigned`: the `access` tile "Access denials by kind and operation" names the operations; a person who should have access is added to `sg-docchat-<env>-users`; a sudden rise after a registration change means part B runs once more for that environment.

## Verification

Part B step 4 and part C step 3.

## Undo

A User Administrator deletes part A's users and groups with `az ad user delete` and `az ad group delete`, and a Privileged Role Administrator the unit and the role assignments; part B and C changes are undone by the inverse Graph calls, by the role each part names.

## Audit record

Entra's audit log for every user, group, role and policy change; the script's printed summary and correlation ID in the ticket.
