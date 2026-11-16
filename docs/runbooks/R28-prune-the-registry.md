# R28 — Prune the registry

**Performer:** the bootstrap operator (Owner on `rg-docchat-shared`), from a machine in the shared allowlist (R24 step 4).

**Trigger:** quarterly.

**Prerequisites:** the operator workstation of `86-operations.md` section 1.5, with a checkout of the repository for the `deploy-prod-*` tags.

Placeholders in angle brackets are filled in at run time; `<registry>` is `crdocchat<sharedToken>`.

## Steps

1. The commits `prod` ran (the set P):

   ```bash
   git fetch --tags
   git for-each-ref --format "%(*objectname)" "refs/tags/deploy-prod-*"
   ```

2. For each repository `docchat/api`, `docchat/ingestion` and `docchat/model-stub`, list the manifests older than 400 days:

   ```bash
   az acr manifest list-metadata --registry <registry> --name docchat/<image> --query "[?lastUpdateTime < '<UTC date 400 days ago>'].{digest: digest, tags: tags}" -o json
   ```

3. For each listed manifest none of whose tags is in P:

   ```bash
   az acr repository delete --name <registry> --image docchat/<image>@<digest> --yes
   ```

4. Verify:

   ```bash
   az acr manifest show-metadata --registry <registry> --name docchat/<image>:<commit> --query digest -o tsv
   az acr show-usage --name <registry> -o table
   ```

   Expected: each commit in P still resolves to a digest, and the usage shows storage below the Premium tier's 500 GiB.

5. Record the number of images deleted in the ticket, and remove the address added for this run.

## Verification

Step 4.

## Undo

None; an image of a commit outside P comes back only by building that commit once more.

## Audit record

The registry's `ContainerRegistryRepositoryEvents` in `log-docchat-security`.
