# ADR 0010: Python 3.13 and pinned base images

- **Status:** Accepted
- **Date:** 2026-10-21; amended 2026-11-07 (base-image pins moved)
- **Decided by:** Architect
- **Specified in:** 01-repository.md, 14-ingestion.md, 83-infrastructure.md, 84-testing.md, 86-operations.md
- **Related ADRs:** 0005, 0009, 0011, 0047, 0049, 0109, 0124, 0135, 0136, 0152

## Context

Every Python app and image in this product runs one interpreter version, and that version must be supported both by Azure Functions, which hosts the ingestion app, and by the `unstructured` library, which the page-wise method needs. The ingestion app is built from the Azure Functions Python base image with the page-wise method's operating-system packages. Every model that method uses is downloaded at image build time, pinned by revision and SHA-256, into one read-only path outside `/home`, and the app runs offline, so ingestion keeps working once egress filtering closes outbound access. Each commit's images are built once, scanned and deployed by digest; lockfiles are committed; Dependabot watches the base images.

A runtime that can change after it is scanned can't be trusted:

- Files installed into a running instance vanish on restart, scale-out or redeploy, so every operating-system package must ship in the image, and the image must not open SSH into its instances.
- A model fetched at run time is an unpinned download from a public hub, it can send vendor analytics, and it fails once outbound access is filtered.
- Compilers and headers in a final image hand an attacker who exploits a parser a toolchain beside the run's credentials.
- Pre-release SDKs and end-of-life runtimes on active paths leave the product without security fixes.

Vendor facts (checked 2026-10-14 and 2026-10-15):

- Functions 4.x runs Python 3.13 GA, supported until October 2029; 3.14 is also GA; 3.12 is the last version on Linux Consumption. `unstructured` 0.27.25 declares `Requires-Python >=3.11, <3.14`. `azure-functions` 2.3.0 and `azure-functions-runtime` 1.2.1, both Production/Stable, require Python 3.13 or later.
- The Core Tools template builds `FROM mcr.microsoft.com/azure-functions/python:4-python3.13` and sets `AzureWebJobsScriptRoot=/home/site/wwwroot` and `AzureFunctionsJobHost__Logging__Console__IsEnabled=true`; the `-appservice` image variant enables SSH.
- Microsoft Artifact Registry lists the versioned tag `4.1055.150-0-python3.13` (host 4.1055.150.0, Debian 12 bookworm, amd64); Docker Hub's official image lists `python:3.13.16-slim-bookworm` (Debian 12).
- Pulling by digest (`<registry>/<repository>@sha256:<digest>`) guarantees the bytes even if an image with the same tag is pushed later.
- Extension bundle 4.x is active and 3.x, 2.x and 1.x are deprecated; by default the host downloads bundle updates from `cdn.functions.azure.com`.

## Decision

1. **Python 3.13 everywhere.** Every Python process, image and CI job runs CPython 3.13. Every uv workspace member declares `requires-python = ">=3.13,<3.14"`, `.python-version` holds `3.13`, CI installs it through uv 0.13.1 (`astral-sh/setup-uv`), Ruff uses `target-version = "py313"` and mypy `python_version = "3.13"`.
2. **Base images, each pinned by versioned tag and digest.**

   | Image | Dockerfile | `FROM` (both stages) | OS |
   |---|---|---|---|
   | `docchat/ingestion` | `apps/ingestion/Dockerfile` | `mcr.microsoft.com/azure-functions/python:4.1055.300-3-python3.13@sha256:a7f5fb3a2f98c9f883eaa2b6f2d2f602d9fc44f41a91c450623b6784c2d7b379` | Functions host 4.1055.300.3, Debian 12, amd64 |
   | `docchat/api` | `apps/api/Dockerfile` | `python:3.13.18-slim-trixie@sha256:09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc` | Debian 13 |
   | `docchat/model-stub` | `tests/model-stub/Dockerfile` | the same as the API | Debian 13 |

   Amended 2026-11-07 (the product owner's decision): the Python images moved from `python:3.13.16-slim-bookworm` to the newest `python:3.13` slim-trixie digest and the ingestion image to the newest Functions Python 3.13 digest (Microsoft now numbers host rebuilds `4.<host>-<revision>-python3.13`), to clear the findings that had fixes; `01-repository.md` section 4 holds the current pins and ADR 0154 the exception for OS-package findings without a fix.

   The versioned host tag is used, never the floating `4-python3.13`, so every Functions host upgrade is a reviewed pull request; the digest pins the bytes. The `-appservice` variant is never used.
3. **Build invariants for all three images.** Two stages; compilers and build tools exist only in the build stage. Python dependencies come only from the lockfile: `uv export --locked --no-dev --package <member> --no-emit-workspace --format requirements-txt -o /tmp/requirements.txt` (hashes included; `--locked` fails when `uv.lock` is out of date, and nothing exports with `--frozen`), then `uv pip install --require-hashes --no-deps -r /tmp/requirements.txt` into `/opt/venv`, then `uv build --wheel --package docchat-core -o /tmp/wheels` and the same for the app, installed with `uv pip install --no-deps /tmp/wheels/*.whl`. Run-time OS packages are installed with `apt-get install --no-install-recommends` and the apt lists removed in the same layer; the API and ingestion images install `msodbcsql18` and `libgssapi-krb5-2` from Microsoft's Debian 12 repository with `ACCEPT_EULA=Y`. `ARG DOCCHAT_BUILD_COMMIT` (40 hex characters) becomes `ENV DOCCHAT_BUILD_COMMIT`. No secret, `.env`, `.azure/`, test file or source map enters an image: the root `.dockerignore` excludes `.git`, `**/tests`, `**/node_modules`, `.azure`, `.env*`, `reports`, `build`, `dist` and `load-results`.
4. **The API and model-stub images.** The runtime stage runs as user `docchat` (UID 10001, GID 10001) in `/app` with `PYTHONDONTWRITEBYTECODE=1` and `PYTHONUNBUFFERED=1`; the API image sets `TIKTOKEN_CACHE_DIR=/opt/docchat/tiktoken` and fetches the `o200k_base` and `cl100k_base` encodings into it at build time.
5. **The ingestion image carries everything it runs.**
   - Run-time packages only: `libmagic1`, `poppler-utils`, `tesseract-ocr` with `tesseract-ocr-eng`, `-fra`, `-deu`, `-spa`, `-ita`, `-nld` and `-por`, `libglib2.0-0`, `msodbcsql18` (ODBC Driver 18.7), `unixodbc`, `libgssapi-krb5-2` and `libseccomp2` (2.5.4-1+deb12u1, which the first-party binding `docchat_ingestion.isolation.syscall_filter` loads through `ctypes` for the parsing child's system-call filter), and the converter's packages, each named with its exact Debian 12 version (`libreoffice-core-nogui`, `libreoffice-writer-nogui` and `libreoffice-impress-nogui` at `4:7.4.7-1+deb12u14`, `fonts-crosextra-carlito` `20220224-1`, `fonts-crosextra-caladea` `20200211-1`, `fonts-liberation2` `2.1.5-1`; ADR 0152); no compilers, headers or pandoc. OpenCV is `opencv-python-headless` 5.0.0.93 through a uv dependency override that removes `opencv-python`; `torch` resolves from PyTorch's CPU wheel index (`https://download.pytorch.org/whl/cpu`), and the build fails if any `nvidia-*` distribution is installed.
   - Models in `/opt/docchat/models`, read-only, pinned in `apps/ingestion/models.lock.json` by repository, revision (commit hash) and the SHA-256 of every file: the layout model `unstructuredio/yolo_x_layout` (`yolox_l0.05.onnx`), the table-structure model `microsoft/table-transformer-structure-recognition`, its backbone `timm/resnet18.a1_in1k` (`model.safetensors`) in the Hugging Face cache `/opt/docchat/models/hf`, and spaCy `en_core_web_sm` 3.8.0 from its release wheel with SHA-256 `1932429db727d4bff3deed6b34cfc05df17794f4a52eeb26cf8928f7c1a0fb85`. The revisions and hashes are captured once when stage 1 starts and committed; the build fails on any mismatch. The download script is `apps/ingestion/image/fetch_models.py`; build-time scripts and self-check fixtures live under `apps/ingestion/image/`.
   - The `tiktoken` encodings `o200k_base` and `cl100k_base` in `/opt/docchat/tiktoken`, checked against their published SHA-256 values.
   - The extension bundle in `[4.39.1, 5.0.0)` from the pinned base image, under `/FuncExtensionBundles/Microsoft.Azure.Functions.ExtensionBundle/`; a build step fails when no version directory in that range exists, and the host loads that bundle at start-up and downloads none.
   - Image environment: `HF_HOME=/opt/docchat/models/hf`, `HF_HUB_OFFLINE=1`, `HF_HUB_DISABLE_TELEMETRY=1`, `DO_NOT_TRACK=true`, `SCARF_NO_ANALYTICS=true`, `TIKTOKEN_CACHE_DIR=/opt/docchat/tiktoken`, `UNSTRUCTURED_DEFAULT_MODEL_INITIALIZE_PARAMS_JSON_PATH=/opt/docchat/models/layout/params.json`, `OMP_NUM_THREADS=1`, `AzureWebJobsScriptRoot=/home/site/wwwroot`, `AzureFunctionsJobHost__Logging__Console__IsEnabled=true`.
   - The host and worker keep the base image's UID 0, which the parsing child's user switch needs. Every parse runs as one of the 16 parse users `docchat-parse-0` to `docchat-parse-15` (UIDs and GIDs 10002 to 10017, home `/nonexistent`, shell `/usr/sbin/nologin`), created in this image with one lock file each, `/run/docchat-parse/<uid>.lock`, owned by root with mode 0600, so two concurrent children never share a user (ADR 0049).
6. **The last build step proves the image.** A final `RUN` fails the build unless `/opt/microsoft/msodbcsql18/lib64/libmsodbcsql-18.*.so.*` exists (API and ingestion) and `python -c "import docchat_core, pyodbc; pyodbc.drivers().index('ODBC Driver 18 for SQL Server')"` succeeds. In the ingestion image that step is the offline self-check, `RUN --network=none` as root: it converts the fixture DOCX and PPTX files with the `convert` command and partitions the fixture PDF (with a table and a scanned page) and both converted PDFs through the parsing runner under parse users of the pool, each child with its system-call filter installed and the request flag `failOnNetwork`, whose audit hook ends the child with exit code 3 on any `socket.getaddrinfo` or `socket.connect`; asserts that the PDF's table element carries `text_as_html`; loads both encodings; checks the ODBC driver; runs the isolation probe (filter installed; `socket`, `socketpair`, `io_uring_setup`, `ptrace(PTRACE_TRACEME)` and a self `process_vm_readv` refused with `EPERM`; `clone3` refused with `ENOSYS`; a thread started after the load filtered); and runs the parsing isolation checks.
7. **Updates.** Dependabot's `docker` entry covers `/apps/api`, `/apps/ingestion` and `/tests/model-stub`, weekly on Monday at 05:00 UTC with a cooldown of 7 days by default (30 for a major, 7 for a minor, 3 for a patch), and moves the tag and the digest together. A Functions host image pull request merges only after the ingestion integration suite and the offline self-check pass. A new Python minor version is adopted only through a new ADR, once both Azure Functions and `unstructured` support it.
8. **Delivery.** Images are tagged with the commit SHA, built and scanned once per commit and deployed by digest (ADR 0124); pull requests build all three without pushing (CI job `images`).

## Alternatives considered

- **Python 3.14.** Lost: Functions supports it, but `unstructured` 0.27.25 declares `<3.14`, so the page-wise method couldn't be installed.
- **Python 3.12 or 3.11.** Lost: both are supported by Functions and `unstructured`, but `azure-functions` 2.3.0 and `azure-functions-runtime` 1.2.1 need 3.13, and their Functions support ends sooner (October 2028 and October 2027).
- **Floating tags (`4-python3.13`, `3.13-slim-bookworm`).** Lost: a host or OS upgrade would reach production in an unreviewed rebuild, and the scanned bytes and the deployed bytes could differ.
- **A tag without a digest.** Lost: a re-pushed tag changes the image under the same name. The versioned tag still tells a reviewer which host version a pull request moves to.
- **The `-appservice` variant.** Lost: it enables SSH into running instances, through which packages could be installed by hand into an instance that later loses them.
- **The Functions base image for the API too.** Lost: it carries the Functions host and its .NET runtime, which the API never runs, and every one of their packages would need scanning and patching.
- **A distroless or Alpine image for the API.** Lost: distroless has no package manager for the ODBC driver's Debian package, and Alpine adds a second OS family to install the driver for and to triage findings in; Debian 12 slim matches the ingestion image's OS.
- **Models fetched on first use or at start-up.** Lost: unpinned bytes, outbound calls to model hubs and vendor analytics, and ingestion failing once egress is filtered.
- **Installing from the package index, or from requirements without hashes.** Lost: builds wouldn't be reproducible, and a look-alike package could enter; `--require-hashes` installs exactly what `uv.lock` records.
- **One-stage images.** Lost: compilers and headers would ship to every instance.

## Consequences

- Every instance runs bytes that were scanned and tested, and nothing is fetched at run time, so ingestion keeps working when egress filtering arrives (ADR 0109).
- Every base-image change is a Dependabot pull request with the tag and digest in its diff; a Functions host upgrade waits for the ingestion integration suite and the offline self-check.
- An unfixed high or critical vulnerability in a base image blocks every merge and release until the digest moves, the package leaves the final stage or its use changes, because no waiver exists at high (ADR 0136).
- The ingestion image is large (CPU `torch`, OCR, models); longer pulls and cold starts are covered by the 900-second container start limit and one prewarmed instance (ADR 0005).
- Python 3.13's Functions support ends in October 2029; moving to 3.14 waits for `unstructured` to support it and takes a new ADR.
- The `<3.14` bound keeps uv from resolving for an interpreter the platform or the parser doesn't support.
- Obligations: 01-repository.md's version table carries both base images with their digests; the stage-1 offline-models spike runs the image with `docker run --network none` and blocks stage 1 on any network attempt; the dependency-update runbook gates host upgrades; the CI `images` job builds the three images on every pull request.
- Risk: Microsoft could withdraw a versioned host tag; the digest still pulls from the registry the build pushed to, and Dependabot proposes the next tag.

## References

- Supported languages in Azure Functions (Python 3.13 GA, end of support October 2029; 3.12 last on Linux Consumption): https://learn.microsoft.com/en-us/azure/azure-functions/supported-languages (checked 2026-10-14)
- Azure Functions Python developer reference (the 3.13 worker): https://learn.microsoft.com/en-us/azure/azure-functions/functions-reference-python (checked 2026-10-14)
- Core Tools Python 3.13 Dockerfile template: https://raw.githubusercontent.com/Azure/azure-functions-core-tools/main/src/Cli/func/StaticResources/Dockerfile.python3.13 (checked 2026-10-14)
- Azure Functions Python base image tags: https://mcr.microsoft.com/v2/azure-functions/python/tags/list (checked 2026-10-15)
- Python official image: https://hub.docker.com/_/python (checked 2026-10-15)
- Extension bundles (4.x active; download from the CDN): https://learn.microsoft.com/en-us/azure/azure-functions/extension-bundles (checked 2026-10-14)
- Container registry concepts (pull by digest): https://learn.microsoft.com/en-us/azure/container-registry/container-registry-concepts (checked 2026-10-14)
- unstructured 0.27.25 (`Requires-Python <3.14`): https://pypi.org/project/unstructured/ (checked 2026-10-14)
- azure-functions 2.3.0 and azure-functions-runtime 1.2.1 (Python 3.13 or later): https://pypi.org/project/azure-functions/ and https://pypi.org/project/azure-functions-runtime/ (checked 2026-10-14)
- Install ODBC Driver 18 on Linux (Debian 12 repository): https://learn.microsoft.com/en-us/sql/connect/odbc/linux-mac/installing-the-microsoft-odbc-driver-for-sql-server (checked 2026-10-14)
