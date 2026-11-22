# syntax=docker/dockerfile:1
#
# Custom MCP gateway image.
#
# Multi-stage build: dependencies are resolved deterministically from the
# committed ``uv.lock`` in a builder stage, and only the resulting virtualenv
# and the application source are copied into a slim runtime stage. The runtime
# never contains a compiler toolchain, and no secret is ever baked in.

# ---------------------------------------------------------------------------
# Builder: create /app/.venv from uv.lock (no dev dependencies).
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

# Pinned uv distribution, copied from the official image (no pip bootstrap).
COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Resolve dependencies first so a source-only change does not invalidate the
# expensive dependency layer. ``README.md`` is required because pyproject.toml
# declares it as the project readme.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Now install the project itself (editable: ``custom_mcp`` resolves from
# /app/src, which is where the runtime stage copies the source).
COPY src ./src
RUN uv sync --frozen --no-dev

# ---------------------------------------------------------------------------
# Runtime: slim image, non-root user, persistent /data volume.
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    DATA_DIR=/data

WORKDIR /app

# Create an unprivileged system user and the persistent data directory it owns.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --home-dir /app app \
    && mkdir -p /data \
    && chown -R app:app /data

# The venv and source tree, owned by the runtime user.
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/src /app/src

# Plugin packages and state live here; declared as a volume for persistence.
VOLUME /data

EXPOSE 8000

# Drop privileges; every instruction and the container process run as ``app``.
# There is intentionally no ``USER root`` after this line.
USER app

# Slim images have no curl; probe the health endpoint with the stdlib instead.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]

CMD ["custom-mcp"]
