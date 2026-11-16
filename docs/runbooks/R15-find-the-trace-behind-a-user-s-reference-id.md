# R15 — Find the trace behind a user's reference ID

**Performer:** an operator (Log Analytics Reader on the environment's workspace through `sg-docchat-operators`; no just-in-time level, `86-operations.md` section 2.8.9).

**Trigger:** a user's error report quoting a reference ID.

**Prerequisites:** the operator workstation of `86-operations.md` section 1.5, with `.azure/<env>/.env` refreshed (`azd env refresh -e <env> --no-prompt`), which holds `DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID`.

Placeholders in angle brackets are filled in at run time. `docchat-ops` outcomes (`86-operations.md` section 3.1): exit 3, check the membership, then `az logout` and `az login --tenant <tenant ID>`; exit 5, run the same command once more. Every run prints its correlation ID first; the ticket records it.

## Steps

1. Take the 32-character reference ID from the user's message: the problem's `correlationId`, which is the trace ID the browser sent (`85-observability.md` section 2.11).

2. Look up the trace:

   ```bash
   uv run docchat-ops --env <env> trace show <reference ID> --ticket <id> --reason "user error report"
   ```

   Expected: the trace's records in time order, with outcomes, error codes, session and turn IDs and the user's object ID. Exit 2: the ID isn't 32 lower-case hexadecimal characters, so check the copy; exit 4: no record within 30 days.

3. Read the session and turn IDs and the error code; the code's meaning is in the error catalogue of the file that owns the operation (`11-api.md` section 2).

4. When the answer needs the content of the turn: R19.

5. A sign-in failure's reference ID is Entra ID's correlation ID; look it up in `log-docchat-security` (timespan `P30D`), whose customer ID is `az monitor log-analytics workspace show --resource-group rg-docchat-identity --workspace-name log-docchat-security --query customerId -o tsv`:

   ```bash
   az rest --method post \
     --url "https://api.loganalytics.io/v1/workspaces/<log-docchat-security customer ID>/query" \
     --resource "https://api.loganalytics.io" \
     --body "{\"query\": \"SigninLogs | where CorrelationId == '<id>'\", \"timespan\": \"P30D\"}" \
     --query "tables[0].rows"
   ```

   Expected: the sign-in record with its result and failure reason.

## Verification

Step 2 lists at least one record for the reference ID, and the ticket names the session, the turn and the error code found.

## Undo

None.

## Audit record

None: the command reads metadata only. The ticket records the reference ID and the command's correlation ID.
