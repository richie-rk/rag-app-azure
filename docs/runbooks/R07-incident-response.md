# R07 — Incident response

**Performer:** the on-call operator; a security responder joins every security incident; for step 6, a User Administrator, a group owner and the tenant administrator (`86-operations.md` section 1.1); for a restart, the bootstrap operator.

**Trigger:** any Sev1 alert; an alert whose runbook is "Incident response" (R20); a security event; a failed `prod_checks.py`, or a failed `deploy_checks.py` step (`app_settings_mismatch`, `grants_mismatch`; `83-infrastructure.md` section 10.7 steps 9 and 10).

**Prerequisites:** the operator workstation of `86-operations.md` section 1.5; for step 6.4, an address in the environment's `DOCCHAT_OPERATOR_IP_ALLOWLIST` (SQL) and level `user_data`.

Placeholders in angle brackets are filled in at run time. `docchat-ops` outcomes (`86-operations.md` section 3.1): exit 3, check the membership, then `az logout` and `az login --tenant <tenant ID>`; exit 5, run the same command once more; exit 6, read the state and rerun; exit 7, check that the machine's address is in the allowlist and that the store is healthy. Every run prints its correlation ID first; the ticket records it.

KQL steps run through the Logs query API (`86-operations.md` section 1.5) against the environment's workspace unless the step names another, with the timespan the step names (`PT1H` when none is named):

```bash
az rest --method post \
  --url "https://api.loganalytics.io/v1/workspaces/<DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID>/query" \
  --resource "https://api.loganalytics.io" \
  --body "{\"query\": \"<KQL with single-quoted strings>\", \"timespan\": \"<ISO 8601 duration>\"}" \
  --query "tables[0].rows"
```

## Steps

1. Acknowledge the alert (Sev1 within 30 minutes during the organisation's cover hours) and open an incident ticket with the alert name, the environment and the time it fired.

2. Classify by the alert and go to its step:

   | Class | Alerts and events | First look | Step |
   |---|---|---|---|
   | Availability and capacity | `api-down`, `api-instance-unhealthy`, `api-5xx-high`, `api-5xx-elevated`, `event-loop-lag`, `sql-failures`, `api-capacity-exhausted`, `audit-write-failures`, `telemetry-silent`, `telemetry-scrub-errors`, `telemetry-dropped-attributes` | `availability`, then `capacity` | 3 |
   | Chat | `chat-failed-turns`, `screening-failures`, `usage-recording-failures`, `content-filter-volume`; user reports about answers, sources or downloads | `ttft`, `openai`, `quota` | 4 |
   | Access | `user-provisioning-failures`, `invalid-token-share`, `signing-key-fetch-failures`, `signing-key-age`, `request-limit-rejections` | `access`, `quota` | 5 |
   | Security | `malware-detected` after R18; every alert of `log-docchat-security` that a pipeline run or ticket explains (R20); a gitleaks finding; a suspected credential exposure; an urgent access removal | the alert's operation and caller | 6 |

3. **Availability.** Read the `availability` tiles "Availability %", "5xx by operation and code", "Healthy-instance percentage" and "Problems by code", and the `capacity` tiles "API plan CPU", "Autoscale capacity and actions", "Instances per role" and "SQL retries, failures and pool"; list instances:

   ```bash
   az webapp list-instances --name <DOCCHAT_API_APP_NAME> --resource-group rg-docchat-<env> -o table
   ```

   - A release started it (this query, timespan `PT2H`): R02.

     ```kusto
     AppRequests | where TimeGenerated > ago(2h) | summarize sum(ItemCount) by AppVersion, bin(TimeGenerated, 10m)
     ```

   - One unhealthy instance: the bootstrap operator restarts the app (operators hold only Reader):

     ```bash
     az webapp restart --name <DOCCHAT_API_APP_NAME> --resource-group rg-docchat-<env>
     ```

   - `api-capacity-exhausted` with autoscale at its maximum: raise `api.maxInstances` in `infra/env/<env>.json` by pull request and R01.
   - `sql-failures` or `audit-write-failures`: the SQL server's health and the `capacity` tile "SQL pool wait"; in `dev`, a paused serverless database resumes on the first login.
   - `telemetry-silent`: in `dev` the workspace's daily cap (`monitoring.dailyCapGb`), with this query (timespan `P1D`); otherwise the apps' health.

     ```kusto
     _LogOperation | where Category =~ 'Ingestion' and Detail contains 'OverQuota'
     ```

   - `telemetry-scrub-errors` or `telemetry-dropped-attributes`: open an issue naming the attribute key and fix forward (`85-observability.md` section 3.6).
   - A regional problem: Azure Service Health for the subscription and region in the Azure portal.

4. **Chat.**
   - `chat-failed-turns`: the `ttft` tile "Turns by outcome" and the `openai` tile "429s by deployment, chat path and ingestion"; throttling follows R09 step 7.
   - `screening-failures`: chat and memory fail closed while Prompt Shields is unavailable, by design, and no bypass exists; check Content Safety's service health in the environment's region and post a service notice.
   - `content-filter-volume`: the `openai` tile "Screening and filters" by project; read affected turns only under R19; a threshold change is a reviewed Bicep change to the content filter policy (`83-infrastructure.md` section 5.6).
   - Flagged sources, with this query (timespan `P7D`); an admin reviews each document and deletes it or uploads a corrected version:

     ```kusto
     AppEvents | where Name == 'source_screening_flagged' | summarize n = count() by documentId = tostring(Properties['documentId'])
     ```

   - `usage-recording-failures`: the `quota` tile "Usage-recording failures" and Cosmos DB throttling on the `capacity` tile "Cosmos DB request charge per operation".
   - "Too many answers in progress" reports: stream leases expire within `chat.max_turn_seconds` + 30 s (at most 250 s), so no manual step exists; reports after that point to `docchat.ratelimit.rejections` and instance health.
   - "Too many sources loading" reports and download aborts: download leases expire within 300 s; a rise of `slow_client` aborts from one user is a slow reader or a scripted client, and the limits apply per user.
   - Turns ended with `service_restarting` outside deploy windows (the `availability` tile "Citation outcomes, download aborts, stream disconnects and drained turns"): instance health and the platform's restart events.

5. **Access.**
   - `signing-key-fetch-failures` or `signing-key-age`: confirm the API reaches `login.microsoftonline.com` over its default outbound path (the `access` tile "Signing-key age and fetches"); a restart (step 3) forces a fetch at start-up; after an emergency key roll by Microsoft, tokens signed with the new key work within 300 seconds per instance without action.
   - `invalid-token-share`: the `access` tile "Token validations by outcome and reason": one reason dominating after a release is a client fault (R02); many reasons from few callers is probing (step 6).
   - `user-provisioning-failures`: the `availability` tile "Problems by code" and SQL health (step 3).
   - `request-limit-rejections`: find the users behind the `429`s with this query; one account at the line rate is a script or a compromised account (step 6), many accounts at once a misbehaving client release (step 3).

     ```kusto
     AppRequests | where AppRoleName == 'docchat-api' and ResultCode == '429' | summarize n = sum(ItemCount) by UserAuthenticatedId, Name | top 10 by n
     ```

6. **Security containment and urgent access removal**, in this order (`11-api.md` section 1.4.6):
   1. A User Administrator (Privileged Authentication Administrator when the account holds a directory role, `86-operations.md` section 1.1) disables the account and revokes its sessions:

      ```bash
      az ad user update --id <upn> --account-enabled false
      az rest --method POST --url https://graph.microsoft.com/v1.0/users/<oid>/revokeSignInSessions
      ```

   2. A group owner removes the user from `sg-docchat-<env>-users` and `sg-docchat-<env>-admins`, and the tenant administrator (Cloud Application Administrator) deletes any direct DocChat assignment, both as R06 step B1's first option:

      ```bash
      az ad group member remove --group sg-docchat-<env>-users --member-id <oid>
      az ad group member remove --group sg-docchat-<env>-admins --member-id <oid>
      az rest --method GET --url https://graph.microsoft.com/v1.0/users/<oid>/appRoleAssignments
      az rest --method DELETE --url https://graph.microsoft.com/v1.0/users/<oid>/appRoleAssignments/<assignment ID>
      ```

      The `DELETE` runs for each assignment whose `resourceDisplayName` starts with `DocChat`.
   3. An admin removes the user's project memberships in the Projects screen; they take effect on the next request. A token issued earlier stays valid for at most 90 minutes after issue for anything else its roles allow.
   4. Review the person's recent admin changes at level `user_data`; admins undo each unwanted change in the admin screens:

      ```bash
      uv run docchat-ops --env <env> user audit-events --actor <oid> --since <UTC time of the suspected start> --ticket <id> --reason "incident review of admin changes"
      ```

   5. A leaked test-user password → R08 at once; a leaked Static Web Apps token → R14; a pipeline credential, or an alert of `log-docchat-security` that nothing explains → R31; any other credential → revoke it at its issuer first, then remove it from history with this command and a force-push coordinated by a `docchat-maintainers` administrator (gitleaks findings are never waived):

      ```bash
      git filter-repo --replace-text <expressions file>
      ```

   6. `malware-detected`: the `document.malware_detected` row names the uploader as its subject; treat the uploader's account as in sub-steps 1 to 4 when the upload wasn't expected.

7. **Evidence.** Save the KQL results that show the event (identifiers and times only) to the ticket; never paste content.

8. **A user-reported error** with a reference ID: R15.

9. **Sev1 updates** go every 60 minutes to the stakeholders named in the ticket.

10. **Close** with a post-incident review within 5 business days, recording any runbook or alert change it decides.

## Verification

Each class step (3 to 6) names its own check; the post-incident review of step 10 records the outcome in the ticket.

## Undo

Each containment step is undone by its inverse once the review clears the account: a User Administrator re-enables the account, and a group owner restores the group membership:

```bash
az ad user update --id <upn> --account-enabled true
az ad group member add --group <group> --member-id <oid>
```

## Audit record

The incident ticket; Entra audit entries for account and group changes.
