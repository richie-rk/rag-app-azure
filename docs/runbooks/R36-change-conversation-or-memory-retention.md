# R36 — Change conversation or memory retention

**Performer:** a maintainer, with the product owner's approval in the ticket; a release approver approves the `prod` deployment (R01).

**Trigger:** a retention decision (`13-conversations-and-memory.md` section 9.3).

**Prerequisites:** the operator workstation of `86-operations.md` section 1.5, with `.azure/<env>/.env` refreshed (`azd env refresh -e <env> --no-prompt`), which holds `DOCCHAT_COSMOS_ACCOUNT_NAME`.

Placeholders in angle brackets are filled in at run time. `conversationRetentionDays` sets the `conversations` container's `defaultTtl` and `memoryRetentionDays` the `memories` container's (`13-conversations-and-memory.md` section 9.1): 0 sets `-1`, so nothing expires; `N` sets `N × 86,400` seconds, and each item then expires `N` days after its own last change.

## Steps

1. Open a ticket with the environment, the new `conversationRetentionDays` and `memoryRetentionDays` (each 0 to 3,650; 0 keeps items) and the product owner's approval.

2. `prod`: announce the change to users 14 days before step 4, because turning retention on deletes, in the background, every item whose last change is older than the period, and only a point-in-time restore inside the 30-day continuous-backup window brings such items back (7 days in `dev` and `load`). Record the announcement's date in the ticket.

3. Set the values in `infra/env/<env>.json` and open the pull request:

   ```bash
   gh pr create --title "Retention: <env> conversations <days>, memory <days>" --body "<ticket and the product owner's approval>"
   ```

   Expected: the pull-request checks pass (`infra-validate` refuses a value the environment file's type doesn't allow), and the what-if of the `preflight-whatif` job (`83-infrastructure.md` section 10.5), which runs against `dev`, shows for a `dev` change the containers' `defaultTtl`: `-1` for 0, otherwise days × 86,400.

4. Merge, and deploy with R01 (automatic in `dev`; approved by a release approver in `prod`, no sooner than 14 days after step 2's announcement). Expected: the deploy run succeeds.

5. Verify each container whose value changed:

   ```bash
   az cosmosdb sql container show --resource-group rg-docchat-<env> --account-name <DOCCHAT_COSMOS_ACCOUNT_NAME> --database-name docchat --name <conversations|memories> --query resource.defaultTtl -o tsv
   ```

   Expected: `-1` for 0, otherwise the days × 86,400.

6. Items the TTL deletes inside a live container return only through a restore of the whole account into a new account, which R06 part C step 6 sends to the product owner as a new ADR; step 2's announcement exists for that reason.

## Verification

Step 5 returns the expected `defaultTtl` for each changed container, and the ticket holds the pull request, the deploy run and, in `prod`, the announcement's date.

## Undo

A revert pull request deployed with R01 stops further deletions; items already deleted are covered by step 6.

## Audit record

The pull request, the deploy run and the Activity Log's container updates; the ticket with the product owner's approval.
