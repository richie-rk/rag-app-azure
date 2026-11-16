# R19 — Read a user's conversation; access review

**Performer:** an operator at level `read` (`13-conversations-and-memory.md` section 10.12, procedures 1, 2 and 8 to 10); a group owner of `sg-docchat-<env>-conversation-readers` (or PIM) for the grant; any operator for the monthly review's reads and the alert steps, which need no level.

**Trigger:** a ticket that needs a turn's content; the monthly access review; the alerts `summary-failures`, `extraction-failures`, `abandoned-turns`, `client-id-conflicts`, `human-conversation-access`, `docchat-alert-jit-grant`, `docchat-alert-jit-self-grant` and `docchat-alert-jit-overdue`; a user's report that an answer is still being written.

**Prerequisites:** the operator workstation of `86-operations.md` section 1.5, with `.azure/<env>/.env` refreshed (`azd env refresh -e <env> --no-prompt`), which holds `DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID` and `DOCCHAT_API_IDENTITY_PRINCIPAL_ID`; for steps 2 to 4, a machine whose public address is in the environment's `DOCCHAT_OPERATOR_IP_ALLOWLIST` (SQL and Cosmos DB) and an encrypted volume for the `--out` directory.

Placeholders in angle brackets are filled in at run time. `docchat-ops` outcomes (`86-operations.md` section 3.1): exit 3, check the membership, then `az logout` and `az login --tenant <tenant ID>`; exit 5, run the same command once more; exit 7, check that the machine's address is in the allowlist and that the store is healthy. Every run prints its correlation ID first; the ticket records it. Findings go into the ticket by reference only (identifiers, counts, codes and times), never conversation text. Workbook tiles are named as `85-observability.md` section 5.9 names them.

KQL steps run through the Logs query API (`86-operations.md` section 1.5) against the environment's workspace unless the step names another, with the timespan the step names:

```bash
az rest --method post \
  --url "https://api.loganalytics.io/v1/workspaces/<DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID>/query" \
  --resource "https://api.loganalytics.io" \
  --body "{\"query\": \"<KQL with single-quoted strings>\", \"timespan\": \"<ISO 8601 duration>\"}" \
  --query "tables[0].rows"
```

## Reading a conversation

1. From R15, take the user's object ID (`enduser.id` on the request span) and the session and turn IDs (the `docchat.conversations.begin_turn` span); open a ticket with them, the reason and a duration of at most 4 hours.

2. A group owner grants the level, or the operator activates it in PIM; then the operator signs in again so the token carries the membership:

   ```bash
   uv run docchat-ops --env <env> jit grant --level read --member <operator object ID> --hours <1 to 4> --ticket <id> --reason "<reason>"
   az logout
   az login --tenant <tenant ID>
   ```

   Expected: exit 0 and the grant's receipt (exit 6: the operator is already a member); `docchat-alert-jit-grant` fires and is acknowledged with the ticket.

3. Read the turns into an empty directory on the encrypted volume (`--turn` repeats up to 50 times; without it the newest 50 turns are read):

   ```bash
   uv run docchat-ops --env <env> user read-conversation --user <oid> --session <sessionId> --turn <turnId> --out <directory on an encrypted volume> --ticket <id> --reason "<reason>"
   ```

   Expected: exit 0 and the counts, and `<sessionId>.json` in the directory with mode 0600; `human-conversation-access` fires within 30 minutes, and its acknowledgement names the ticket. Exit 4: the session doesn't exist for that user, so check the IDs; exit 2: more than 50 `--turn`, or the directory already holds files.

4. Record findings in the ticket by reference only and delete the output directory. The owner revokes, or PIM expires the grant; then sign out:

   ```bash
   uv run docchat-ops --env <env> jit revoke --level read --member <operator object ID> --ticket <id> --reason "work finished"
   az logout
   ```

## Monthly access review

5. Run the human-access query over the last 31 days (timespan `P31D`):

   ```text
   az rest … CDBDataPlaneRequests | where DatabaseName == 'docchat' and AadPrincipalId != '<DOCCHAT_API_IDENTITY_PRINCIPAL_ID>' | summarize requests = count(), firstSeen = min(TimeGenerated), lastSeen = max(TimeGenerated) by AadPrincipalId
   ```

   Expected: one row per principal other than the API identity that touched the `docchat` database, with its first and last request.

6. Match every principal and window to a ticket and to a grant in PIM history or in the Entra `AuditLogs` of `log-docchat-security` (that workspace's customer ID, `86-operations.md` section 1.5; timespan `P31D`), allowing the 90 minutes a token stays valid after a revocation:

   ```text
   az rest … AuditLogs | where OperationName in ('Add member to group', 'Remove member from group') | project TimeGenerated, OperationName, group = tostring(TargetResources[1].displayName), member = tostring(TargetResources[0].id)
   ```

   Expected: each window of step 5 lies inside a grant that a ticket names.

7. Confirm the three level groups are empty:

   ```bash
   uv run docchat-ops --env <env> jit list --ticket <id> --reason "monthly access review"
   ```

   Expected: exit 0 and no member in any of the three groups, and in the fallback mode no per-principal data-plane assignment.

8. Any unmatched access, any `docchat-alert-jit-self-grant`, or a `docchat-alert-jit-overdue` without a ticket starts R07 (security, step 6), and R31 when it can't be explained. A `docchat-alert-jit-grant` is acknowledged with its ticket.

The review's ticket is the monthly access review record (T-556). It holds, by reference only:

```text
Monthly access review: <env>, <month>; operator <GitHub handle>
Step 5: <n> principals other than the API identity in CDBDataPlaneRequests over P31D
  <AadPrincipalId>  <firstSeen>..<lastSeen>  matched to ticket <ID> and grant <PIM or AuditLogs time>
  ... one line per principal; any line without a ticket names the R07 ticket it started
Step 7: jit list correlation ID <ID>; members of read, config and user-data: none
Alerts of the month: jit-self-grant <none | tickets>; jit-overdue <none | tickets>
Result: passed | R07 <ticket>
```

## Conversation alerts and reports

9. `summary-failures`, `extraction-failures`: read the `conversations` tiles "Summary rebuilds by outcome" and "Memory extractions by outcome and rejections by reason", then filter the `docchat.summary.rebuild` or `docchat.memory.extract` spans by `docchat.error_code`:

   ```text
   az rest … AppDependencies | where Name in ('docchat.summary.rebuild', 'docchat.memory.extract') | summarize calls = sum(ItemCount) by Name, code = tostring(Properties['docchat.error_code'])
   ```

   - `model_unavailable`: the Azure OpenAI checks of R09's throttling step (throttling, quota, deployment health);
   - `content_filtered`: expected for some sessions; no action unless it exceeds 5% of calls;
   - `invalid_output` dominated by finish reason `length`: raise `summary.max_completion_tokens` or `memory.max_completion_tokens` with R03 (its capacity check in `prod`), or lower the reasoning effort;
   - `store_conflict`: check Cosmos DB throttling and `docchat.conversations.store_conflicts`.

10. "An answer is still being written" reports and `abandoned-turns`: the block ends at the turn's `deadlineAt` (receipt + `chat.max_turn_seconds` + 30 s, 210 s by default). Past that, check Cosmos DB availability, `docchat.conversations.store_conflicts` and the `conversations` tile "Abandoned, superseded and duplicate turns".

11. `client-id-conflicts`: the same tile; a single client repeating IDs is a client fault (R02 when a release caused it).

12. `human-conversation-access`: read the `conversations` tile "Human access to conversation data" and match each principal, collection and window to a ticket's grant as in step 6; an access without one follows step 8.

13. Feedback review: read the trends from the `conversations` tiles "Thumbs up and down per project per day, with the down rate" and "Negative feedback by category" (metadata only); the content of a turn behind a negative rating is read with steps 1 to 4 and a ticket.

## Verification

Reading: the ticket holds the command's correlation ID and its findings by reference, the output directory is deleted, and `jit list` shows the operator no longer in the `read` group. Review: every principal of step 5 is matched in the review's ticket and step 7 lists no member. Alerts: the alert resolves (the state alerts on their own; `human-conversation-access` after its 15-minute mute).

## Undo

None.

## Audit record

One `conversation.read` row per read (`turnIds`, `ticket`, `level` `read`); the Entra membership changes in the `AuditLogs` of `log-docchat-security`; the `human-conversation-access` acknowledgement naming the ticket; the review's results in its ticket.
