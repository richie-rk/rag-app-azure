# ADR 0124: Build once and deploy by digest

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Architect
- **Specified in:** 83-infrastructure.md, 01-repository.md, 82-security.md, 84-testing.md, 86-operations.md
- **Related ADRs:** 0010, 0027, 0103, 0118, 0119, 0120, 0121, 0123, 0127, 0135, 0136

## Context

Every environment must run exactly the bytes that the pipeline built, scanned and tested: each commit's images are built once into the shared registry, scanned, tagged with the commit SHA and deployed by digest, with lockfiles committed, base images pinned by digest and the build failing on any high or critical finding. Nothing is installed at deploy time, and no OS package is ever added to a running instance.

Three further forces shape the jobs. Building runs third-party code (npm install scripts, Vite plugins, Dockerfile `RUN` steps), so it must run where no token for a deploy identity can be minted (ADR 0120). A tag is mutable: anyone holding AcrPush could point `docchat/api:<sha>` at other bytes, so a deploy that finds its digest by tag, a rollback for instance (ADR 0127), needs proof that the digest is the one `main` built for that commit. And a provision sets each site's whole configuration, so it must never start new code before the migrations of the same release have finished (ADR 0027).

Vendor facts (checked 2026-10-14 unless stated):

- A digest reference `[loginServerUrl]/[repository]@[sha256:digest]` always pulls the same image, even after an identically tagged image is pushed.
- `actions/attest-build-provenance` v4.2.2 (published 2026-08-06, checked 2026-10-15) writes SLSA build provenance signed through the job's GitHub OIDC token. `gh attestation verify oci://<image-uri>` checks it with `--repo`, `--signer-workflow`, `--source-ref` and `--source-digest`, and an `oci://` URI needs the caller signed in to the registry (checked 2026-10-15).
- azd's `docker.imagePassthrough` deploys an image reference exactly as given, digest included, without building, pulling or pushing (ADR 0123).

## Decision

1. **Three images per commit.** `docchat/api`, `docchat/ingestion` and `docchat/model-stub`, from Dockerfiles that meet the image invariants of ADR 0010: two stages with every `FROM` pinned by digest, Python dependencies only from `uv.lock` with hashes, run-time OS packages baked in, a final smoke `RUN` step, `ARG DOCCHAT_BUILD_COMMIT` (40 hex) carried into `ENV`. The load generator has no image of its own: it runs on the ingestion image's pinned Microsoft base image and downloads k6 by checksum (ADR 0135).
2. **`build`, with no credential** (push to `main` only; no GitHub environment; `permissions {contents: read}`; no OIDC token and no Azure sign-in; 60 minutes): builds the web client (`npm ci`, `npm run build`, artifact `web-dist`, 90 days) and the pinned Static Web Apps CLI archive (artifact `swa-cli`, 90 days, ADR 0121); builds each image with `docker buildx build --platform linux/amd64 --build-arg DOCCHAT_BUILD_COMMIT=<sha> --label org.opencontainers.image.revision=<sha> --label org.opencontainers.image.source=<repository URL> --tag <login server>/docchat/<image>:<sha> --load <repository root>`; scans each local image with Trivy 0.76.0 (`trivy image --scanners vuln,secret --format json`) and passes the result to `scripts/ci/security_gate.py`, where any high or critical finding fails with no waiver (ADR 0136), so no image with such a finding is ever pushed; writes a CycloneDX SBOM per image as an artifact; and saves the three images with `docker save` into the artifact `images-<sha>` (1 day) beside `image-ids.json` (`{"api", "ingestion", "model-stub"}`, each the local image ID `sha256:<64 hex>`).
3. **`publish`, with no build tools** (push only; needs `build`; `environment: dev`; concurrency `docchat-registry`; `permissions {contents: read, id-token: write, actions: read, attestations: write}`; 30 minutes; it runs no `npm`, `docker build` or `buildx`): downloads `images-<sha>`, runs `docker load`, and fails with `image_id_mismatch` unless each loaded image's ID equals `image-ids.json`; signs in as the `dev` deploy identity, opens the registry for its runner (ADR 0119) and runs `az acr login`; pushes the three images; records each digest reference `<login server>/docchat/<image>@sha256:<64 hex>` in `release-manifest.json` (`{commit, apiImage, ingestionImage, modelStubImage, builtAt}`, artifact `release-manifest`, 90 days); attests each pushed digest with `actions/attest-build-provenance` (`subject-name: <login server>/docchat/<image>`, `subject-digest: sha256:<64 hex>`, `push-to-registry: false`, so the provenance lives in the repository's attestations and nothing extra is written to the registry); verifies each with `gh attestation verify oci://<digest reference> --repo <owner>/<repo> --signer-workflow <owner>/<repo>/.github/workflows/deploy.yaml --source-ref refs/heads/main --source-digest <commit>` (`GH_TOKEN: ${{ github.token }}`; failure `image_attestation_invalid`); closes the registry rule with `if: always()`; outputs `api_image`, `ingestion_image`, `model_stub_image` and `commit`.
4. **`resolve`, for dispatched deploys** (`workflow_dispatch` only; `environment: dev`; concurrency `docchat-registry`; `permissions {contents: read, id-token: write, attestations: read}`; 30 minutes): checks out with `fetch-depth: 0` and fails with `commit_not_on_main` unless `git merge-base --is-ancestor <image_commit> origin/main`; signs in as the `dev` deploy identity, opens the registry and runs `az acr login`; reads each image's digest for the tag `<image_commit>` (`az acr manifest show-metadata --registry <registry> --name docchat/<image>:<image_commit> --query digest -o tsv`; a missing tag fails with `image_tag_missing`); verifies each digest reference with the same `gh attestation verify` call and `--source-digest <image_commit>` (failure `image_attestation_invalid`), so a tag re-pointed by anyone holding AcrPush can't reach a deploy; closes the rule; uploads `release-manifest` for the resolved references and gives the same outputs as `publish`.
5. **Every environment deploys by digest.** Deploy jobs receive only references that `publish` or `resolve` of a `deploy.yaml` run on `main` produced and verified (in `load`, read from that run's `release-manifest`); `prod` and `load` jobs never contact the registry, and the apps pull through their private endpoints with their own identities. The `preprovision` hook rejects any image reference that doesn't match `^[a-z0-9.-]+\.azurecr\.io/docchat/[a-z-]+@sha256:[0-9a-f]{64}$` with `image_reference_invalid`. The API and ingestion images deploy with `azd deploy <service> --from-package <reference>` (ADR 0123); the `load` model stub takes its image from provisioning.
6. **Provisioning keeps the running image.** The `preprovision` hook reads each site's live `linuxFxVersion` (site found by its `azd-service-name` tag), strips `DOCKER|`, and writes `DOCCHAT_API_IMAGE_CURRENT`, `DOCCHAT_INGESTION_IMAGE_CURRENT` and `DOCCHAT_MODEL_STUB_IMAGE_CURRENT`; a site that doesn't exist yet (an environment's first provision) takes the new release's reference. `main.bicep` writes those values into `linuxFxVersion`, so `azd provision` and its migrations finish before any new code starts.
7. **Pull requests build without pushing.** `ci.yaml`'s `images` job (45 minutes) builds the three images, runs their smoke steps, Trivy and the gate, and pushes nothing.
8. **The registry.** Repositories `docchat/api`, `docchat/ingestion` and `docchat/model-stub`; tags are full 40-character commit SHAs and serve only as lookup keys. `nightly.yaml`'s `trivy-rescan` job re-scans the digests running in `dev` (the sites' `linuxFxVersion`) and in `prod` (the images tagged with the commit of the newest `deploy-prod-*` tag) and opens or updates an issue for a finding. No image is deleted automatically; runbook R28 prunes, at most once a quarter, images older than 400 days that no `deploy-prod-*` tag references.

## Alternatives considered

- **Build and push in one job under the deploy identity.** Rejected: every install script, build plugin and Dockerfile step would run where a token for the `dev` deploy identity can be minted.
- **Build per environment.** Rejected: `prod` would run bytes that `dev` never ran and that were never scanned as deployed.
- **Deploy by tag.** Rejected: a tag can be re-pointed, and an app restart would then pull different bytes.
- **Signing images with a project-held key** (Notation or Cosign key pairs). Rejected: the key would be a stored secret to protect and rotate; GitHub's attestation is signed through the job's OIDC token, verified by `gh attestation verify`, and needs no registry write.
- **Building in Azure with registry tasks.** Rejected: the build would run outside the CI scanning gate and need the registry's network bypass for tasks, which stays off.
- **An image for the load generator.** Rejected: a fourth image to build, scan and keep; the generator runs on the pinned ingestion base image (ADR 0135).
- **Letting provisioning set the new image.** Rejected: new code could start before the release's migrations finish.

## Consequences

- Every environment runs bytes that `main`'s pipeline built and scanned once, and every digest found by tag is checked against its attestation before it is deployed.
- An image with a high or critical finding never reaches the registry; a finding published later appears in the nightly re-scan as an issue.
- A load test or a rollback can use only a release whose artifacts and images still exist: `release-manifest` lives 90 days, and R28 keeps every image a `deploy-prod-*` tag references (Premium includes 500 GiB).
- Saving and loading images costs a few minutes per release; the image ID check makes that hand-over safe.
- Tests (84-testing.md): CI checks prove that no job holding `id-token: write` runs `npm`, `docker build` or `docker buildx build`, and that every Dockerfile's `FROM` lines are digest-pinned; unit tests prove that the `preprovision` hook rejects tag-only references and accepts digest references; deployed `dev` tests prove that the API and ingestion images run from digests equal to the release manifest, each with a valid attestation for `deploy.yaml` on `main`, and that a dispatch for a commit whose tag was re-pointed to an unattested image fails in `resolve` with `image_attestation_invalid` and deploys nothing.

## References

- Container registry concepts (digest references): https://learn.microsoft.com/en-us/azure/container-registry/container-registry-concepts (checked 2026-10-14)
- `gh attestation verify`: https://cli.github.com/manual/gh_attestation_verify (checked 2026-10-15)
- `actions/attest-build-provenance` releases: https://github.com/actions/attest-build-provenance/releases (checked 2026-10-15)
- azure.yaml schema (`docker.imagePassthrough`): https://github.com/Azure/azure-dev/blob/main/schemas/v1.0/azure.yaml.json (checked 2026-10-14)
