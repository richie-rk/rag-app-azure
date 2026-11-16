# The API image (01-repository.md sections 4.1 to 4.4 and 4.7; 11-api.md section 1.15), built
# from the repository root:
#   docker buildx build --platform linux/amd64 --file apps/api/Dockerfile \
#     --build-arg DOCCHAT_BUILD_COMMIT=<sha> --tag <login server>/docchat/api:<sha> --load .
#
# The locked dependencies and the two first-party wheels, the tokenizer encodings, the ODBC driver
# from Microsoft's Debian 13 repository, Gunicorn's configuration, and, last and offline, the smoke
# step. Gunicorn is process 1 in exec form, so it receives App Service's stop signal itself.

FROM python:3.13.18-slim-trixie@sha256:09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc AS build

# The tools that fetch and check Microsoft's signing key exist in this stage only.
RUN apt-get update \
 && apt-get install --no-install-recommends -y ca-certificates curl gnupg \
 && rm -rf /var/lib/apt/lists/*

# The key that signs Microsoft's Debian 13 repository: the file must hold exactly one primary key,
# and its fingerprint must be that signing key's, so a substituted file fails the build.
ARG MICROSOFT_KEY_FINGERPRINT=AA86F75E427A19DD33346403EE4D7792F748182B
RUN curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
      --output /tmp/microsoft.asc https://packages.microsoft.com/keys/microsoft-2025.asc \
 && GNUPGHOME="$(mktemp -d)" && export GNUPGHOME \
 && gpg --batch --show-keys --with-colons /tmp/microsoft.asc > /tmp/microsoft-keys.txt \
 && test "$(grep -c '^pub:' /tmp/microsoft-keys.txt)" = 1 \
 && test "$(awk -F: '/^pub:/ { getline; if ($1 == "fpr") print $10 }' /tmp/microsoft-keys.txt)" = "$MICROSOFT_KEY_FINGERPRINT" \
 && gpg --batch --dearmor --output /usr/share/keyrings/microsoft-prod.gpg /tmp/microsoft.asc \
 && rm -rf "$GNUPGHOME" /tmp/microsoft.asc /tmp/microsoft-keys.txt

# uv from its hashed wheel; required-version in pyproject.toml fails the first uv command when
# this pair goes stale.
ARG UV_VERSION=0.13.1
ARG UV_WHEEL_SHA256=696dcf6d807d85da978f4db8472da81dccc1254b18b52cbd1367c4d923c421e0
RUN printf 'uv==%s --hash=sha256:%s\n' "$UV_VERSION" "$UV_WHEEL_SHA256" > /tmp/uv-requirements.txt \
 && python -m pip install --no-cache-dir --no-deps --require-hashes -r /tmp/uv-requirements.txt

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
COPY apps/api/src apps/api/src

# Third-party distributions only from the lock, each checked against its hash; the unhashed
# workspace members stay out of the export.
RUN uv venv /opt/venv \
 && uv export --locked --no-dev --package docchat-api --no-emit-workspace --format requirements-txt -o /tmp/requirements.txt \
 && uv pip install --python /opt/venv --require-hashes --no-deps -r /tmp/requirements.txt

RUN uv build --offline --wheel --package docchat-core -o /tmp/wheels \
 && uv build --offline --wheel --package docchat-api -o /tmp/wheels \
 && uv pip install --python /opt/venv --no-deps /tmp/wheels/*.whl

# The encodings: tiktoken downloads each file and checks it against its published SHA-256.
RUN TIKTOKEN_CACHE_DIR=/opt/docchat/tiktoken /opt/venv/bin/python -I -c \
      "import tiktoken; tiktoken.get_encoding('o200k_base'); tiktoken.get_encoding('cl100k_base')"


FROM python:3.13.18-slim-trixie@sha256:09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc AS runtime

# The ODBC driver from Microsoft's repository, the only third-party apt source; this stage never
# runs curl or gpg. The same layer upgrades exactly the base packages whose Debian security fix the
# pinned digest predates (zlib1g, DSA of 2026-11-01), so no fixable finding reaches the image scan.
COPY --from=build /usr/share/keyrings/microsoft-prod.gpg /usr/share/keyrings/microsoft-prod.gpg
RUN printf '%s\n' 'deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/13/prod trixie main' \
      > /etc/apt/sources.list.d/microsoft-prod.list \
 && apt-get update \
 && ACCEPT_EULA=Y apt-get install --no-install-recommends -y msodbcsql18 unixodbc libgssapi-krb5-2 \
 && apt-get install --no-install-recommends --only-upgrade -y zlib1g \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 docchat \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin docchat

# The base image's pip, with the certifi, urllib3 and msgpack it vendors, has no use at run time,
# so the final stage drops it and the image scan sees only the locked packages (84-testing.md
# section 9: a package leaves the final stage).
RUN python -m pip uninstall --yes --no-input pip

COPY --from=build /opt/venv /opt/venv
COPY --from=build /opt/docchat /opt/docchat
RUN chmod -R a-w /opt/docchat
COPY apps/api/gunicorn.conf.py /app/gunicorn.conf.py
WORKDIR /app

ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TIKTOKEN_CACHE_DIR=/opt/docchat/tiktoken
ARG DOCCHAT_BUILD_COMMIT
ENV DOCCHAT_BUILD_COMMIT=$DOCCHAT_BUILD_COMMIT

# Smoke step, last and offline: the ODBC driver is installed and registered, both encodings load
# from the cache, the application factory builds with no settings and no network, and the build
# commit is a full lower-case SHA.
RUN --network=none ls /opt/microsoft/msodbcsql18/lib64/libmsodbcsql-18.*.so.* \
 && python -I -c "import docchat_core, pyodbc; pyodbc.drivers().index('ODBC Driver 18 for SQL Server')" \
 && python -I -c "import tiktoken; tiktoken.get_encoding('o200k_base'); tiktoken.get_encoding('cl100k_base')" \
 && python -I -c "from docchat_api.app import create_app; create_app()" \
 && python -I -c "import os, re, sys; sys.exit(re.fullmatch('[0-9a-f]{40}', os.environ.get('DOCCHAT_BUILD_COMMIT', '')) is None)"

USER 10001:10001
EXPOSE 8000
CMD ["gunicorn", "--config", "/app/gunicorn.conf.py", "docchat_api.app:create_app()"]
