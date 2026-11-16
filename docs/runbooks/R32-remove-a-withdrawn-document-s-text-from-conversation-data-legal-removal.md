# R32 — Remove a withdrawn document's text from conversation data (legal removal)

**Performer:** an operator at level `user_data`, after an admin deleted the document (`13-conversations-and-memory.md` section 10.12, procedure 11); an admin of the document's index for step 2; a group owner of `sg-docchat-<env>-user-data-operators` (or PIM) for the grant; the ticket's approver for the decision of step 6.

**Trigger:** a legal removal request for a document's text.

**Prerequisites:** the operator workstation of `86-operations.md` section 1.5, on a machine whose public address is in the environment's `DOCCHAT_OPERATOR_IP_ALLOWLIST` (SQL and Cosmos DB), with `.azure/<env>/.env` refreshed (`azd env refresh -e <env> --no-prompt`).

Placeholders in angle brackets are filled in at run time. `docchat-ops` outcomes (`86-operations.md` section 3.1): exit 3, check the membership, then `az logout` and `az login --tenant <tenant ID>`; exit 5, run the same command once more; exit 6, read the state and rerun; exit 7, check that the machine's address is in the allowlist and that the store is healthy. Every run prints its correlation ID first; the ticket records it.

## Steps

1. Open a ticket naming the document ID and the legal basis.

2. An admin deletes the document in the Documents screen (`adminDeleteDocument`), which hides it from readers at once and starts the clean-up of its chunks, renditions and blobs. Step 4 doesn't need the document's SQL row, so it also works after that clean-up. Expected: the document no longer appears in the index's document list.

3. A group owner grants the level, or the operator activates it in PIM; then the operator signs in again so the token carries the membership:

   ```bash
   uv run docchat-ops --env <env> jit grant --level user_data --member <operator object ID> --hours 4 --ticket <id> --reason "<legal basis>"
   az logout
   az login --tenant <tenant ID>
   ```

   Expected: exit 0 and the grant's receipt (exit 6: the operator is already a member).

4. Redact the document's text from the stored turn traces:

   ```bash
   uv run docchat-ops --env <env> user redact-document --document-id <documentId> --ticket <id> --reason "<legal basis>"
   ```

   It prints the number of matching turn traces and asks for confirmation (in `prod` type `prod`), replaces every source block of that document with `[removed]`, then prints the (`userId`, `sessionId`) pairs of the traces it found. Expected: exit 0 and `traceCount`; copy the pairs into the ticket. Exit 5 or 6: run it once more until it exits 0; exit 7: check the allowlist and the network.

5. Verify with one more run:

   ```bash
   uv run docchat-ops --env <env> user redact-document --document-id <documentId> --ticket <id> --reason "<legal basis>"
   ```

   Expected: exit 0 and `traceCount` 0.

6. Record in the ticket that the stored answers, session summaries and follow-ups of the listed sessions may still quote the document: the command changes only source blocks (`13-conversations-and-memory.md` section 10.10), and no procedure deletes one session or edits one answer. When the ticket's approver decides that the legal basis requires removing a listed user's quotes, run R06 part B for that user, which also removes the user's DocChat app-role assignments, memberships, memory and every session; otherwise close with the quotes kept, as `82-security.md` section 1.6 accepts.

7. Revoke, or let PIM expire; then sign out:

   ```bash
   uv run docchat-ops --env <env> jit revoke --level user_data --member <operator object ID> --ticket <id> --reason "legal removal finished"
   az logout
   ```

8. Delete every `internal/` export package kept with an open ticket that holds the document's text (R06 part A step 4), and record each deletion in this ticket.

## Verification

Step 5's run exits 0 with `traceCount` 0; the ticket lists the (`userId`, `sessionId`) pairs and the approver's decision of step 6; `jit list` shows the operator no longer in the `user_data` group.

## Undo

None: the replaced text can't be brought back.

## Audit record

One `document.traces_redacted` row per run (`documentId`, `traceCount`, `ticket`), part of the deletion register that R06 part C re-applies after a restore; the admin's `document.deleted` row of step 2; the just-in-time grant and revocation in Entra ID's audit log (and `docchat-alert-jit-grant`); the `human-conversation-access` alert raised by the command's Cosmos DB requests; the ticket.
