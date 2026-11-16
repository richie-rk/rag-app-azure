# The ingestion image (14-ingestion.md section 15.2; 01-repository.md sections 4.1 to 4.3 and
# 4.6), built from the repository root:
#   docker buildx build --platform linux/amd64 --file apps/ingestion/Dockerfile \
#     --build-arg DOCCHAT_BUILD_COMMIT=<sha> --tag <login server>/docchat/ingestion:<sha> --load .
#
# The Functions host, the locked dependencies, the two first-party wheels, the pinned models and
# encodings, the ODBC driver, the converter of section 15.11 with its fonts, the parse-user pool
# of section 15.6 with its lock files and scratch parent, and, last and offline, the ODBC smoke
# check, the readable-files check and the offline self-check of section 15.5.

FROM mcr.microsoft.com/azure-functions/python:4.1055.300-3-python3.13@sha256:a7f5fb3a2f98c9f883eaa2b6f2d2f602d9fc44f41a91c450623b6784c2d7b379 AS build

# The tools that fetch and check Microsoft's signing key exist in this stage only.
RUN apt-get update \
 && apt-get install --no-install-recommends -y ca-certificates curl gnupg \
 && rm -rf /var/lib/apt/lists/*

# Microsoft's Debian 12 repository key: the file must hold exactly one primary key, and its
# fingerprint must be the release-signing key's, so a substituted file fails the build. The base
# image ships its own copy of the keyring, which the checked key replaces (`--yes`).
ARG MICROSOFT_KEY_FINGERPRINT=BC528686B50D79E339D3721CEB3E94ADBE1229CF
RUN curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
      --output /tmp/microsoft.asc https://packages.microsoft.com/keys/microsoft.asc \
 && GNUPGHOME="$(mktemp -d)" && export GNUPGHOME \
 && gpg --batch --show-keys --with-colons /tmp/microsoft.asc > /tmp/microsoft-keys.txt \
 && test "$(grep -c '^pub:' /tmp/microsoft-keys.txt)" = 1 \
 && test "$(awk -F: '/^pub:/ { getline; if ($1 == "fpr") print $10 }' /tmp/microsoft-keys.txt)" = "$MICROSOFT_KEY_FINGERPRINT" \
 && gpg --batch --yes --dearmor --output /usr/share/keyrings/microsoft-prod.gpg /tmp/microsoft.asc \
 && rm -rf "$GNUPGHOME" /tmp/microsoft.asc /tmp/microsoft-keys.txt

# uv from its hashed wheel; required-version in pyproject.toml fails the first uv command when
# this pair goes stale. The base image's Python installs scripts outside PATH
# (/opt/python/3/bin), so the uv binary is linked into /usr/local/bin.
ARG UV_VERSION=0.13.1
ARG UV_WHEEL_SHA256=696dcf6d807d85da978f4db8472da81dccc1254b18b52cbd1367c4d923c421e0
RUN printf 'uv==%s --hash=sha256:%s\n' "$UV_VERSION" "$UV_WHEEL_SHA256" > /tmp/uv-requirements.txt \
 && python -m pip install --no-cache-dir --no-deps --require-hashes -r /tmp/uv-requirements.txt \
 && ln -sf "$(python -c 'import sysconfig; print(sysconfig.get_path("scripts"))')/uv" /usr/local/bin/uv \
 && uv --version

# The virtual environment must use the base image's Python, which the runtime stage has at the
# same path; a Python uv downloaded would exist only in this stage.
ENV UV_PYTHON_DOWNLOADS=never

# The lock and every member's manifest, so uv checks the lock against the whole workspace, and
# the source of the two wheels this image builds.
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY packages/core/pyproject.toml packages/core/pyproject.toml
COPY apps/api/pyproject.toml apps/api/pyproject.toml
COPY apps/ingestion/pyproject.toml apps/ingestion/pyproject.toml
COPY db/pyproject.toml db/pyproject.toml
COPY scripts/ops/pyproject.toml scripts/ops/pyproject.toml
COPY tests/support/pyproject.toml tests/support/pyproject.toml
COPY tests/model-stub/pyproject.toml tests/model-stub/pyproject.toml
COPY packages/core/src packages/core/src
COPY apps/ingestion/src apps/ingestion/src

# Third-party distributions only from the lock, each checked against its hash; the unhashed
# workspace members stay out of the export.
RUN uv venv /opt/venv \
 && uv export --locked --no-dev --package docchat-ingestion --no-emit-workspace --format requirements-txt -o /tmp/requirements.txt \
 && uv pip install --python /opt/venv --require-hashes --no-deps -r /tmp/requirements.txt

RUN uv build --offline --wheel --package docchat-core -o /tmp/wheels \
 && uv build --offline --wheel --package docchat-ingestion -o /tmp/wheels \
 && uv pip install --python /opt/venv --no-deps /tmp/wheels/*.whl

# OpenCV must be the headless build (the override removes opencv-python) and torch the CPU wheel,
# so the build fails on opencv-python or any nvidia-* distribution.
RUN ! uv pip list --python /opt/venv --format freeze | grep -E -i '^(opencv-python==|nvidia-)'

# The pinned models, checked file by file against the lock; the layout parameters come from the
# installed library's label map, so the step runs with the environment's Python.
COPY apps/ingestion/models.lock.json apps/ingestion/models.lock.json
COPY apps/ingestion/image/fetch_models.py apps/ingestion/image/fetch_models.py
RUN PATH=/opt/venv/bin:$PATH python -I apps/ingestion/image/fetch_models.py apps/ingestion/models.lock.json /opt/docchat/models

# The sentence model from its release wheel, by URL with its hash.
ARG SPACY_MODEL_URL=https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl
ARG SPACY_MODEL_SHA256=1932429db727d4bff3deed6b34cfc05df17794f4a52eeb26cf8928f7c1a0fb85
RUN printf 'en_core_web_sm @ %s --hash=sha256:%s\n' "$SPACY_MODEL_URL" "$SPACY_MODEL_SHA256" > /tmp/spacy-requirements.txt \
 && uv pip install --python /opt/venv --require-hashes --no-deps -r /tmp/spacy-requirements.txt

# The encodings: tiktoken downloads each file and checks it against its published SHA-256.
RUN TIKTOKEN_CACHE_DIR=/opt/docchat/tiktoken /opt/venv/bin/python -I -c \
      "import tiktoken; tiktoken.get_encoding('o200k_base'); tiktoken.get_encoding('cl100k_base')"


FROM mcr.microsoft.com/azure-functions/python:4.1055.300-3-python3.13@sha256:a7f5fb3a2f98c9f883eaa2b6f2d2f602d9fc44f41a91c450623b6784c2d7b379 AS runtime

# The run-time packages, in one layer with the apt lists removed: the parser's tools, the ODBC
# driver from Microsoft's repository (the only third-party apt source; this stage never runs curl
# or gpg), libseccomp2 for the parsing child's system-call filter, procps for the kill that ends
# a parse user's processes, and the converter of section 15.11 with its metric-compatible fonts,
# each converter package at its exact Debian 12 version so a build fails rather than install
# another release. No compilers, headers or pandoc. The same layer upgrades exactly the base
# packages whose Debian security fix the pinned digest predates (apt, apt-transport-https and
# libapt-pkg6.0 2.6.2), so no fixable finding reaches the image scan. It then purges the C
# preprocessor the base ships (cpp, cpp-12), x11-xserver-utils, the only package that depends on
# cpp (xdg-utils merely recommends it), and libisl23, libmpc3 and libmpfr6, which only cpp-12
# needs; dpkg refuses, failing the build, while any other installed package depends on one of
# them. gcc-12-base stays: it holds only GCC's documentation, and libgcc-s1 (which libc6 needs),
# libstdc++6 and libgomp1 depend on it.
COPY --from=build /usr/share/keyrings/microsoft-prod.gpg /usr/share/keyrings/microsoft-prod.gpg
ARG LIBREOFFICE_VERSION=4:7.4.7-1+deb12u14
RUN printf '%s\n' 'deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main' \
      > /etc/apt/sources.list.d/microsoft-prod.list \
 && apt-get update \
 && ACCEPT_EULA=Y apt-get install --no-install-recommends -y \
      libmagic1 poppler-utils \
      tesseract-ocr tesseract-ocr-eng tesseract-ocr-fra tesseract-ocr-deu tesseract-ocr-spa \
      tesseract-ocr-ita tesseract-ocr-nld tesseract-ocr-por \
      libglib2.0-0 msodbcsql18 unixodbc libgssapi-krb5-2 \
      libseccomp2=2.5.4-1+deb12u1 procps \
      libreoffice-core-nogui="$LIBREOFFICE_VERSION" \
      libreoffice-writer-nogui="$LIBREOFFICE_VERSION" \
      libreoffice-impress-nogui="$LIBREOFFICE_VERSION" \
      libreoffice-draw-nogui="$LIBREOFFICE_VERSION" \
      libreoffice-base-core="$LIBREOFFICE_VERSION" \
      libreoffice-common="$LIBREOFFICE_VERSION" \
      libreoffice-style-colibre="$LIBREOFFICE_VERSION" \
      ure="$LIBREOFFICE_VERSION" \
      uno-libs-private="$LIBREOFFICE_VERSION" \
      fonts-opensymbol=4:102.12+LibO7.4.7-1+deb12u14 \
      libnumbertext-data=1.0.11-1 \
      fonts-crosextra-carlito=20220224-1 \
      fonts-crosextra-caladea=20200211-1 \
      fonts-liberation2=2.1.5-1 \
 && apt-get install --no-install-recommends --only-upgrade -y apt apt-transport-https libapt-pkg6.0 \
 && dpkg --purge x11-xserver-utils cpp cpp-12 libisl23 libmpc3 libmpfr6 \
 && fc-cache -f \
 && rm -rf /var/lib/apt/lists/*

# The parse-user pool (section 15.6 item 11): users and groups docchat-parse-0 to
# docchat-parse-15 with UIDs and GIDs 10002 to 10017, one root-owned lock file per UID through
# which the parent takes a free user, and the scratch parent, whose entries no parse user can list.
RUN for n in $(seq 0 15); do \
      groupadd --gid $((10002 + n)) "docchat-parse-$n" \
      && useradd --uid $((10002 + n)) --gid $((10002 + n)) --no-create-home --home-dir /nonexistent \
           --shell /usr/sbin/nologin "docchat-parse-$n" || exit 1; \
    done \
 && install -d -o root -g root -m 0700 /run/docchat-parse \
 && for uid in $(seq 10002 10017); do \
      install -o root -g root -m 0600 /dev/null "/run/docchat-parse/$uid.lock" || exit 1; \
    done \
 && install -d -o root -g root -m 0711 /run/docchat-scratch

# The host loads the extension bundle the base image ships and downloads none, so the bundle
# must be in the range host.json names.
RUN python3 -I -c "import pathlib, sys; \
root = pathlib.Path('/FuncExtensionBundles/Microsoft.Azure.Functions.ExtensionBundle'); \
names = [entry.name.split('.') for entry in root.iterdir() if entry.is_dir()]; \
versions = [tuple(int(part) for part in name) for name in names if all(part.isdigit() for part in name)]; \
sys.exit(not any((4, 39, 1) <= version < (5, 0, 0) for version in versions))"

# The environment, the models and encodings (read-only, still readable by every user) and the
# app content; the Functions host and its Python worker keep the base image's UID 0, which the
# parsing child's user switch needs (WEBSITES_ENABLE_APP_SERVICE_STORAGE=false).
COPY --from=build /opt/venv /opt/venv
COPY --from=build /opt/docchat /opt/docchat
RUN chmod -R a-w /opt/docchat
COPY apps/ingestion/host.json apps/ingestion/function_app.py /home/site/wwwroot/
ENV HF_HOME=/opt/docchat/models/hf \
    HF_HUB_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    DO_NOT_TRACK=true \
    SCARF_NO_ANALYTICS=true \
    TIKTOKEN_CACHE_DIR=/opt/docchat/tiktoken \
    UNSTRUCTURED_DEFAULT_MODEL_INITIALIZE_PARAMS_JSON_PATH=/opt/docchat/models/layout/params.json \
    OMP_NUM_THREADS=1 \
    AzureWebJobsScriptRoot=/home/site/wwwroot \
    AzureFunctionsJobHost__Logging__Console__IsEnabled=true \
    PATH=/opt/venv/bin:$PATH
ARG DOCCHAT_BUILD_COMMIT
ENV DOCCHAT_BUILD_COMMIT=$DOCCHAT_BUILD_COMMIT

# The base image's Python ships its own test suite, whose certificate fixtures include private
# keys, and pip with a vendored CA bundle; neither runs here, and both are `.pem` files a parse
# user could read, which the readable-files check refuses, so the final stage drops them. pip's
# vendored urllib3, msgpack and pkg_resources go with it. The base also ships App Service's
# startup-command generator, a Go binary that the image's start script never runs and whose
# modules carry fixed vulnerabilities, so it goes too; the step fails if a start script names it.
RUN /usr/bin/python3 -m pip uninstall --yes --no-input pip \
 && rm -rf "$(/usr/bin/python3 -I -c 'import sysconfig; print(sysconfig.get_path("stdlib"))')/test" \
 && ! grep -rqs startupcmdgen /opt/startup \
 && rm -rf /opt/startupcmdgen

# /tmp and /var/tmp (mode 1777) stay the image's only world-writable directories and files; the
# container runtime adds /dev/shm and /dev/mqueue, which the clearing of a parse user empties.
RUN find / -xdev \( -type d -o -type f \) -perm -0002 -not -path /tmp -not -path /var/tmp \
      -exec chmod o-w {} +

# Last and offline, with the build context and the pinned gitleaks binary mounted for this step
# only: the ODBC smoke check; the readable-files check, which fails on a secret any parse user
# could read; the offline self-check, whose conversions, partitions and isolation probe run as
# parse users under the system-call filter; and the build commit, which becomes telemetry's
# AppVersion that the postdeploy check matches, is a full lower-case SHA.
RUN --network=none \
    --mount=type=bind,target=/src \
    --mount=type=bind,from=ghcr.io/gitleaks/gitleaks:v8.30.1@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f,source=/usr/bin/gitleaks,target=/opt/gitleaks/gitleaks \
    ls /opt/microsoft/msodbcsql18/lib64/libmsodbcsql-18.*.so.* > /dev/null \
 && python -I -c "import docchat_core, pyodbc; pyodbc.drivers().index('ODBC Driver 18 for SQL Server')" \
 && cd /src \
 && python -I apps/ingestion/image/check_readable_files.py --config .gitleaks.toml \
      --gitleaks /opt/gitleaks/gitleaks --exclude /src --exclude /opt/gitleaks \
 && python -I -m docchat_ingestion.selfcheck apps/ingestion/image/selfcheck \
 && python -I -c "import os, re, sys; sys.exit(re.fullmatch('[0-9a-f]{40}', os.environ.get('DOCCHAT_BUILD_COMMIT', '')) is None)"
