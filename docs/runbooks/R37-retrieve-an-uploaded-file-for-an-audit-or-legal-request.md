# R37 — Retrieve an uploaded file for an audit or legal request

**Performer:** an operator at level `user_data`; an administrator of the index for step 2; a group owner of `sg-docchat-<env>-user-data-operators` (or PIM) for the grant.

**Trigger:** an audit or legal request for a file an administrator uploaded, as uploaded. Readers and administrators get only PDF renditions (`82-security.md` SEC-54), so this runbook is the supported way to hand over an uploaded file.

**Prerequisites:** the operator workstation of `86-operations.md` section 1.5, on a machine whose public address is in the environment's `DOCCHAT_OPERATOR_IP_ALLOWLIST` (SQL and the data account), with `.azure/<env>/.env` refreshed (`azd env refresh -e <env> --no-prompt`); a directory that only the operator can read.

Placeholders in angle brackets are filled in at run time. Every `docchat-ops` run prints its correlation ID first; the ticket records it.

## Steps

1. Open a ticket naming the requester, the legal or audit basis, the document ID and the version number, and the approved recipient and delivery channel.

2. An administrator reads the version ID from `adminGetDocument` (`versions[].versionId`, the versions the web client's document panel lists) and records it in the ticket. A version that is `failed` with `source_missing`, or a deleted document whose blobs were purged, has no file: the command of step 4 exits 2 or 4, so record that in the ticket and stop.

3. A group owner grants the level, or the operator activates it in PIM; then the operator signs in again so the token carries the membership:

   ```bash
   uv run docchat-ops --env <env> jit grant --level user_data --member <operator object ID> --hours 1 --ticket <id> --reason "<basis>"
   az logout
   az login --tenant <tenant ID>
   ```

   Expected: exit 0 and the grant's receipt.

4. In the operator-only directory, fetch the source:

   ```bash
   uv run docchat-ops --env <env> document fetch-source --version <versionId> --out <path> --ticket <id> --reason "<basis>"
   ```

   It prints that the file is untrusted before the download. Expected: exit 0 and the version ID, media type, size and SHA-256; the file at `<path>` has mode 0600.
   - Exit 2: `<path>` exists, or the version is `failed` with `source_missing`; choose a new `<path>`, or record the missing source and stop.
   - Exit 4: check the version ID; an unknown version or a purged blob has no file.
   - Exit 5: the stored bytes don't match the recorded SHA-256 (the partial file is removed); record that in the ticket and escalate to the security responder without handing anything over.
   - Exit 7: check the allowlist and the network.

5. Don't open the file on the operator's machine. Hand it over unopened through the channel the ticket names, with its SHA-256 and the warning that it is an untrusted upload, to be opened only in an isolated environment; then delete the local copy and record the deletion in the ticket.

6. Revoke, or let PIM expire; then sign out:

   ```bash
   uv run docchat-ops --env <env> jit revoke --level user_data --member <operator object ID> --ticket <id> --reason "source fetch finished"
   az logout
   ```

## Verification

The recipient confirms, through the ticket, that the SHA-256 of the file received equals the one step 4 printed; `jit list` shows the operator no longer in the `user_data` group.

## Undo

None needed: the command changes no stored data.

## Audit record

One `document.source_fetched` row per fetch (`indexId`, `documentId`, `versionNumber`, `sizeBytes`, `ticket`); the just-in-time grant and revocation in Entra ID's audit log (and `docchat-alert-jit-grant`); the ticket with the hand-over and the deletion of the local copy.
