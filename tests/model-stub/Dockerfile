# The model-stub image (01-repository.md sections 4.1, 4.2 and 4.5), built from the repository
# root: docker buildx build --platform linux/amd64 --file tests/model-stub/Dockerfile \
#   --build-arg DOCCHAT_BUILD_COMMIT=<sha> --tag <login server>/docchat/model-stub:<sha> --load .

FROM python:3.13.18-slim-trixie@sha256:09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc AS build

# uv from its hashed wheel; required-version in pyproject.toml fails the first uv command when
# this pair goes stale.
ARG UV_VERSION=0.13.1
ARG UV_WHEEL_SHA256=696dcf6d807d85da978f4db8472da81dccc1254b18b52cbd1367c4d923c421e0
RUN printf 'uv==%s --hash=sha256:%s\n' "$UV_VERSION" "$UV_WHEEL_SHA256" > /tmp/uv-requirements.txt \
 && python -m pip install --no-cache-dir --no-deps --require-hashes -r /tmp/uv-requirements.txt

# The lock and every member's manifest, so uv checks the lock against the whole workspace, and
# the source of the one wheel this image builds.
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY packages/core/pyproject.toml packages/core/pyproject.toml
COPY apps/api/pyproject.toml apps/api/pyproject.toml
COPY apps/ingestion/pyproject.toml apps/ingestion/pyproject.toml
COPY db/pyproject.toml db/pyproject.toml
COPY scripts/ops/pyproject.toml scripts/ops/pyproject.toml
COPY tests/support/pyproject.toml tests/support/pyproject.toml
COPY tests/model-stub/pyproject.toml tests/model-stub/pyproject.toml
COPY tests/model-stub/src tests/model-stub/src

# Third-party distributions only from the lock, each checked against its hash.
RUN uv venv /opt/venv \
 && uv export --locked --no-dev --package docchat-model-stub --no-emit-workspace --format requirements-txt -o /tmp/requirements.txt \
 && uv pip install --python /opt/venv --require-hashes --no-deps -r /tmp/requirements.txt

# The stub depends on no first-party package, so only its own wheel is built.
RUN uv build --offline --wheel --package docchat-model-stub -o /tmp/wheels \
 && uv pip install --python /opt/venv --no-deps /tmp/wheels/*.whl


FROM python:3.13.18-slim-trixie@sha256:09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc AS runtime

# Exactly the base packages whose Debian security fix the pinned digest predates (zlib1g, DSA of
# 2026-11-01), so no fixable finding reaches the image scan; the apt lists go in the same layer.
RUN apt-get update \
 && apt-get install --no-install-recommends --only-upgrade -y zlib1g \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 docchat \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin docchat

# The base image's pip, with the certifi, urllib3 and msgpack it vendors, has no use at run time,
# so the final stage drops it and the image scan sees only the locked packages (84-testing.md
# section 9: a package leaves the final stage).
RUN python -m pip uninstall --yes --no-input pip

COPY --from=build /opt/venv /opt/venv
WORKDIR /app
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ARG DOCCHAT_BUILD_COMMIT
ENV DOCCHAT_BUILD_COMMIT=$DOCCHAT_BUILD_COMMIT

# Smoke step, last and offline: the application imports with its default settings, and the
# build commit is a full lower-case SHA.
RUN --network=none python -I -c "import docchat_model_stub.app" \
 && python -I -c "import os, re, sys; sys.exit(re.fullmatch('[0-9a-f]{40}', os.environ.get('DOCCHAT_BUILD_COMMIT', '')) is None)"

USER 10001:10001
EXPOSE 8000
# exec makes Uvicorn process 1, so it receives the stop signal; the shell only expands the
# worker count.
CMD ["sh", "-c", "exec uvicorn docchat_model_stub.app:app --host 0.0.0.0 --port 8000 --workers \"${DOCCHAT_STUB_WORKERS:-1}\""]
