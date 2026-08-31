# T096 — the driftzero-api Cloud Run image.
#
# One service holding the API, the Truth Engine and the ADK agents, per plan.md's
# deployed topology. Runs as a non-root user, carries no credential of any kind, and
# obtains its identity from the attached driftzero-run-sa at runtime.

FROM python:3.13-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Dependency metadata first so the wheel layer is reused when only sources change.
COPY pyproject.toml ./
COPY src/ ./src/

# .[api,cloud,live]: the HTTP surface, the Firestore and Cloud Storage adapters, and
# the live model clients (google-adk for Gemini, google-auth/httpx for Vertex AI MaaS).
#
# `live` is present but not thereby active: DRIFTZERO_SEMANTIC_PROVIDER and
# DRIFTZERO_FIELD_PROVIDER still select the provider, and both default to "disabled".
# Shipping the dependency is what makes the choice available to a deployment -- without
# it the composition root can only report UNAVAILABLE, and the service answers "no
# analysis was performed" while looking correctly configured.
#
# The demo console extra stays absent: this image serves the production API.
RUN python -m pip install --upgrade pip \
 && python -m pip wheel --wheel-dir /wheels ".[api,cloud,live]"


FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# A dedicated unprivileged account. Cloud Run does not require root, and running as it
# would mean a container compromise starts with more than it needs.
RUN groupadd --system driftzero \
 && useradd --system --gid driftzero --home-dir /app --no-create-home driftzero

WORKDIR /app

COPY --from=build /wheels /wheels

# Install the wheel the build stage produced, by NAME. Installing from the source
# path would make pip try to build the project again, offline, with no build
# backend available. Only wheels are copied here, so no source tree ships in the
# final image.
RUN python -m pip install --no-index --find-links=/wheels "driftzero[api,cloud,live]" \
 && rm -rf /wheels

# The controlled pilot source-procedure corpus and artifact catalog. The runtime globs
# only the top-level *.json here; fixtures/multimodal/ is excluded by .dockerignore
# because it is test input, never server-read. This is an M2 pilot limitation and the
# /ready endpoint reports it as CLOUD_PILOT rather than claiming production readiness.
COPY fixtures/*.json ./fixtures/

ENV DRIFTZERO_FIXTURES_DIR=/app/fixtures \
    PORT=8080

USER driftzero
EXPOSE 8080

# Shell form on purpose: ${PORT} is supplied by Cloud Run at start time and must be
# expanded by a shell. Exec form would pass the literal string "${PORT}" to uvicorn.
CMD exec uvicorn driftzero_api.app:app --host 0.0.0.0 --port ${PORT}
