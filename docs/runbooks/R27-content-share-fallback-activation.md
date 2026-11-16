# R27 — Content-share fallback activation

**Performer:** a maintainer (pull request) and the bootstrap operator (policy).

**Trigger:** the content-share spike fails any of its criteria 1 to 6 (`83-infrastructure.md` section 2.7) and ADR 0103 records fallback 1.

**Prerequisites:** the operator workstation of `86-operations.md` section 1.5.

Placeholders in angle brackets are filled in at run time.

## Steps

1. Before any deploy that carries the fallback, the bootstrap operator excludes the host account from the shared-key assignment:

   ```bash
   az policy assignment update --name docchat-deny-storage-shared-key --scope /subscriptions/<subscription ID>/resourceGroups/rg-docchat-<env> --not-scopes <host account resource ID>
   ```

   `docchat-alert-governance` fires; acknowledge it with the ticket.

2. A pull request with the fallback-1 changes of `83-infrastructure.md` section 2.7 and `81-config.md` section 2.8: the host account's shared-key exception, its `file` private endpoint and the zone `privatelink.file.core.windows.net`, the share `func-docchat-ingest-<env>`, the secret `ingestion-content-share-connection` written from `listKeys()`, the three app settings, `keyVaultReferenceIdentity`, both outbound-routing switches, the NAT gateway `ng-docchat-<env>` with `pip-docchat-<env>-nat` on `snet-ingest-integration`, and the ingestion identity's Key Vault Secrets User on that secret. Expected: PSRule and the what-if pass.

3. Deploy it (R01), then run the spike's criteria 1 to 6 against the app. When criterion 4, the scale-out, still fails with fallback 1 deployed, stop: fallback 2 goes to the product owner as a new ADR.

4. Verify:

   ```bash
   az network vnet subnet show --resource-group rg-docchat-<env> --vnet-name <virtual network> --name snet-ingest-integration --query natGateway.id -o tsv
   ```

   Expected: the ID names `ng-docchat-<env>`; the deployed `smoke` suite (`dev`) or `prod_smoke` (`prod`) passes and an ingestion run completes.

5. From then on R25 rotates the host key every 90 days, every provision of the environment raises `docchat-alert-key-access` (R20), and every rerun of the bootstrap's `policies` step is followed by step 1 (R21 step 7).

## Verification

Step 4.

## Undo

A revert pull request deployed, then:

```bash
az policy assignment update --name docchat-deny-storage-shared-key --scope /subscriptions/<subscription ID>/resourceGroups/rg-docchat-<env> --not-scopes ""
```

## Audit record

The policy assignment change and the deployment in the Activity Log.
