# One Dockerfile for every Python service.
#
# Nine near-identical Dockerfiles is nine places to forget `USER 10001`. The
# service is chosen with --build-arg SERVICE.
#
#   docker build -f docker/python.Dockerfile --build-arg SERVICE=cairn-gateway .

ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

ARG SERVICE
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY --from=ghcr.io/astral-sh/uv:0.5.14 /uv /usr/local/bin/uv

WORKDIR /build

# Dependency layer first: source changes should not re-resolve the world.
COPY pyproject.toml uv.lock* ./
COPY packages/ packages/
COPY services/ services/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /opt/venv && \
    VIRTUAL_ENV=/opt/venv uv pip install \
        ./packages/cairn-core \
        $([ -d packages/cairn-mcp-kit ] && echo ./packages/cairn-mcp-kit) \
        ./services/${SERVICE}

# ---------------------------------------------------------------- runtime
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ARG SERVICE
ARG VERSION=0.1.0
ARG REVISION=unknown

LABEL org.opencontainers.image.title="${SERVICE}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.source="https://github.com/Nouman-Amjad/cairn" \
      org.opencontainers.image.vendor="Cairn"

# curl is needed by the vLLM warm-up probe and by nothing else here; keep the
# runtime surface to what actually runs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 cairn \
    && useradd -u 10001 -g cairn -s /usr/sbin/nologin -M cairn

COPY --from=builder /opt/venv /opt/venv
COPY --chown=10001:10001 packages/cairn-core/alembic.ini /app/alembic.ini
COPY --chown=10001:10001 packages/cairn-core/src/cairn_core/migrations /app/migrations

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SERVICE=${SERVICE}

WORKDIR /app
USER 10001:10001
EXPOSE 8000

# The console script installed by the service's pyproject.
CMD ["/bin/sh", "-c", "exec ${SERVICE}"]
