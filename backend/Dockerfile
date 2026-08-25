# ARGUS backend — production image.
#
# **Build from the repository root**, not from `backend/`:
#
#     docker build -f backend/Dockerfile -t argus-backend .
#
# The backend imports `parser/`, a sibling package it puts on `sys.path` at
# import time, and Alembic's `script_location` points at `database/migrations`.
# Neither is reachable from a `backend/` context, and `COPY ../parser` is not a
# thing Docker allows. `docker-compose.prod.yml` sets the context accordingly.
#
# Multi-stage so the compiler toolchain needed to build wheels never reaches the
# runtime image — every package in the final image is one more thing to patch.

# --- build ------------------------------------------------------------------
FROM python:3.11-slim AS build

# Wheels are installed into a virtualenv that is copied wholesale to the runtime
# stage. Copying a venv is more predictable than replaying a pip install with
# --no-index, and it keeps pip itself out of the runtime image.
ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv "$VIRTUAL_ENV"

# Requirements alone first, so this layer stays cached until the pins change.
# Copying the source first would rebuild every dependency on every code edit.
COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install --requirement /tmp/requirements.txt

# --- runtime ----------------------------------------------------------------
FROM python:3.11-slim AS runtime

# `git` is not a convenience: the parser shells out to it to clone repositories
# and to read history, so the image is broken without it. `curl` is for the
# healthcheck below. Nothing else.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root, with a fixed uid so a mounted volume's ownership is predictable
# across hosts rather than whatever the daemon happens to assign.
RUN groupadd --gid 10001 argus \
    && useradd --uid 10001 --gid argus --create-home --shell /usr/sbin/nologin argus

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=build /opt/venv /opt/venv

WORKDIR /app
COPY --chown=argus:argus backend/alembic.ini ./
COPY --chown=argus:argus backend/app ./app
# `/parser` and `/database` sit beside `/app` deliberately. The backend's path
# bootstrap resolves the parser as `parents[3] / "parser"` from
# `app/services/parsing.py`, which is `/parser` when WORKDIR is `/app`; and
# `alembic.ini` resolves `script_location` to `/database/migrations`. Moving
# either breaks a path that is correct in the source tree.
COPY --chown=argus:argus parser /parser
COPY --chown=argus:argus database /database

# Clones land here. A named volume in compose, so a restart does not re-clone
# every repository, and owned by argus because the process cannot write as root.
RUN mkdir -p /workspace && chown argus:argus /workspace
ENV WORKSPACE_DIR=/workspace
VOLUME ["/workspace"]

USER argus
EXPOSE 8000

# Hits the app's own /health, which reports each store's reachability but always
# returns 200 — so this checks that the process is serving, not that its
# dependencies are up. A liveness probe that failed when Postgres blipped would
# restart a container that is working correctly.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl --fail --silent --show-error "http://localhost:${PORT:-8000}/health" || exit 1

# **Binds `$PORT` when the platform sets one.** Railway, Render and Fly all
# inject `PORT` and expect the process to listen on it; a hardcoded 8000 makes
# their health checks fail and the deploy roll back. Falls back to 8000 so
# compose and a plain `docker run` need no extra variable.
#
# `sh -c` so the variable expands, and `exec` so uvicorn replaces the shell as
# PID 1 — without it SIGTERM reaches `sh`, uvicorn never sees it, and every
# deploy waits out the platform's kill timeout instead of shutting down cleanly.
#
# No --reload and no --workers: scaling by processes inside one container hides
# the failure of any single one from the platform. Scale containers instead.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
