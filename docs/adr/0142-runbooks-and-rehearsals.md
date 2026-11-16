# ADR 0142: Runbooks and rehearsals

- **Status:** Accepted
- **Date:** 2026-10-21; amended 2026-11-06 (numbering and catalogue brought to the 37 runbooks of the runbook index, R37 from ADR 0152)
- **Decided by:** Architect
- **Specified in:** 86-operations.md, 01-repository.md, 85-observability.md, 90-build-order.md
- **Related ADRs:** 0103, 0127, 0134, 0136, 0138, 0139, 0141, 0152

## Context

Operators deploy and roll back, change runtime settings, rotate secrets, rebuild indexes, answer alerts, serve data-subject requests and recover from incidents; `docs/runbooks/` holds those procedures, among them deploy, rollback, ops-config changes, per-user overrides, re-index, user data export and deletion, and incident response. A procedure that exists only in someone's head, or in a document nobody has run, fails when it is needed: under pressure, with production at stake. Documentation that is never executed drifts from the system it describes. Alerts are only useful when each names what to do next (ADR 0138), and audited operations are only audited when the procedure uses the audited tool (ADR 0139).

## Decision

1. **One file per runbook.** `docs/runbooks/RNN-<slug>.md`, numbered R01 to R37. Every runbook states its purpose; its performer (a role); its prerequisites (roles, network position, tools and versions); numbered steps, each with one exact command and its expected result; verification; how to undo it; and the audit record it produces. Common prerequisites unless a runbook says otherwise: Azure CLI 2.91.0 or later with no extension installed (`az config set extension.use_dynamic_install=no`), azd 1.35.1, uv 0.13.1, GitHub CLI, `az login --tenant <tenant ID>`, the environment's values in `.azure/<env>/.env` (`azd env refresh -e <env>`), and a machine in the operator allowlist for anything that reaches SQL, Cosmos DB or a Key Vault. KQL in a runbook runs through the Logs query API with `az rest --method post --url "https://api.loganalytics.io/v1/workspaces/<workspace ID>/query" --resource "https://api.loganalytics.io"`; data changes run through `docchat-ops` (ADR 0139).
2. **The catalogue.** R01 deploy; R02 rollback; R03 change an ops-config value; R04 per-user index override; R05 re-index an index; R06 user data export and deletion; R07 incident response; R08 test-user password rotation; R09 model version upgrade; R10 zone-redundancy conversions; R11 run the load test; R12 Defender acceptance check; R13 first admins and test identities; R14 Static Web Apps deployment token reset; R15 find the trace behind a user's reference ID; R16 repository and GitHub settings; R17 dependency, base-image and tool updates; R18 ingestion failures and stuck runs; R19 read a user's conversation, and the access review; R20 alert response index and runbook maps; R21 bootstrap a tenant and subscription; R22 first provision of an environment; R23 re-tighten role-assignment conditions; R24 change the operator allowlist; R25 rotate the host-account key (content-share fallback 1 only); R26 remove a stray runner rule; R27 content-share fallback activation; R28 prune the registry; R29 budget alert at 80% or 100%; R30 decommission an environment; R31 suspected pipeline compromise; R32 remove a withdrawn document's text from conversation data; R33 reset the Durable task hub; R34 repair the CI runner group; R35 change the API origin or add a custom domain; R36 change conversation or memory retention; R37 retrieve an uploaded file for an audit or legal request.
3. **One folder for every component.** The procedures of identity and access, ingestion, the chat pipeline, conversations and memory, and infrastructure are written into the same folder under the R-number the catalogue gives them; R20 maps every alert ID to its runbook and first action, and maps every infrastructure procedure and every other component's procedure to its runbook. Every alert rule's description names its runbook, and the alert-rule test fails when that file doesn't exist (ADR 0138).
4. **Rehearsal.** Every runbook is rehearsed in `dev` at stage 6 and again after any change to the commands it uses; R32 is rehearsed on a test document cited by a test user's turns, and R33 with no ingestion run open. Two groups are handled differently: R21, R30, R31 and R34 are rehearsed as walk-throughs that run every read-only command and stop before each step that changes anything; R25 and R27 apply only under content-share fallback 1 and are rehearsed when it is adopted.
5. **The log.** `docs/runbooks/rehearsals.md` holds one table row per rehearsal: date (UTC), runbook ID, environment, operator GitHub handle, result (`passed` or `failed: <reason code>`), ticket. The release sign-in check (R01 step 9), the Defender acceptance (R12), the bootstrap (R21) and first provisions (R22) are logged there too.

## Alternatives considered

- **A wiki or an external runbook tool.** Rejected: it drifts from the code it describes, isn't reviewed with the change that alters a command, and isn't versioned with the release it applies to.
- **Prose procedures without exact commands.** Rejected: an operator under pressure must not translate prose into commands; each step's command and expected result make the runbook checkable.
- **Automating every procedure as a workflow.** Rejected: deploy, rollback, the load test and the nightly checks are workflows already; the rest need a person's judgement or a second person's approval (just-in-time access, data-subject requests, incident containment), so they stay runbooks that call audited tools.
- **Rehearsing only after an incident.** Rejected: the first run of a procedure would be the real one.
- **Rehearsing destructive procedures for real.** Rejected for R21, R30, R31 and R34: bootstrapping, decommissioning and compromise response act on the tenant, the subscription or shared CI resources; a walk-through runs every read and stops before every change.

## Consequences

- A change to a command used by a runbook carries the runbook's edit and a new rehearsal row; a runbook that hasn't been rehearsed since its commands changed is visible in the log.
- Stage 6 acceptance requires every rehearsal to be logged before `prod` serves users.
- Every alert leads to a numbered procedure, so the on-call operator starts from R20 and never from a blank page.
- The cost is the stage-6 rehearsal effort and the rehearsal after each command change; the walk-throughs keep the risky procedures cheap to rehearse.

## References

- Azure CLI dynamic extension installation setting: https://learn.microsoft.com/en-us/cli/azure/azure-cli-extensions-overview (checked 2026-10-21)
- Logs query API request format: https://learn.microsoft.com/en-us/azure/azure-monitor/logs/api/request-format (checked 2026-10-21)
