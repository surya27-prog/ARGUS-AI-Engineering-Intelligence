# Local Development Setup

## Prerequisites

| Tool | Version | Check |
|---|---|---|
| Docker Desktop | 28.x+ | `docker info` |
| Python | 3.11+ | `py --version` |
| Node.js | 22 LTS+ | `node --version` |
| uv | 0.11+ | `uv --version` |
| Git | 2.40+ | `git --version` |

On Windows, installable via winget (one `--id` per package):

```bash
winget install --id Docker.DockerDesktop -e; winget install --id OpenJS.NodeJS.LTS -e; winget install --id astral-sh.uv -e; winget install --id GitHub.cli -e
```

Open a new terminal afterwards — winget updates `PATH`, but existing shells keep
the old value, which shows up as `docker`/`uv` "not recognized" or as
`docker-credential-desktop ... not found in %PATH%` during an image pull.

Docker Desktop must be **launched** before any `docker` command works; installing
the CLI does not start the engine.

## Start the data stack

```bash
git clone https://github.com/DeepuChandru/ARGUS-AI-Engineering-Intelligence.git
cd ARGUS
cp .env.example .env
docker compose up -d
```

First run pulls ~1.5 GB of images and takes a few minutes. Then:

```bash
docker compose ps
```

Expected: `postgres` healthy, `neo4j` healthy, `qdrant` up (no healthcheck — see below).

## Set up the backend

Dependencies are declared in `backend/pyproject.toml` and pinned in
`backend/uv.lock`. Two ways to install them — both produce the same versions.

**With uv (preferred).** Creates `backend/.venv` from the lockfile:

```bash
cd backend && uv sync --frozen
```

**With plain pip**, using the exported requirement files:

```bash
cd backend && py -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt -r requirements-dev.txt
```

`requirements.txt` (runtime) and `requirements-dev.txt` (pytest, ruff) are
generated from `uv.lock` — never hand-edit them. Regenerate after changing
dependencies in `pyproject.toml`:

```bash
cd backend && uv export --frozen --no-hashes --no-emit-project --no-dev -o requirements.txt && uv export --frozen --no-hashes --no-emit-project --only-dev -o requirements-dev.txt
```

## Apply migrations

Requires the Postgres container to be up. Run from `backend/` — both Alembic's
`prepend_sys_path` and the `../.env` lookup in `Settings` are relative to it:

```bash
cd backend && .venv/Scripts/python -m alembic upgrade head
```

## Run the API

```bash
cd backend && .venv/Scripts/python -m uvicorn app.main:app --reload
```

`GET http://localhost:8000/health` returns 200 with `"postgres": "up"` when the
stack is running. Interactive docs at http://localhost:8000/docs.

Tests:

```bash
cd backend && .venv/Scripts/python -m pytest -q
```

`test_health_reports_postgres_up` needs the compose stack running; the other
three pass standalone.

## Services

| Service | Host port | Credentials | UI |
|---|---|---|---|
| PostgreSQL | 5432 | `argus` / `argus_dev_password` | — |
| Neo4j | 7687 (Bolt), 7474 (HTTP) | `neo4j` / `argus_dev_password` | http://localhost:7474 |
| Qdrant | 6333 (REST), 6334 (gRPC) | none | http://localhost:6333/dashboard |

## Verify

```bash
docker exec argus-postgres psql -U argus -d argus -c "select version();"
```

```bash
docker exec argus-neo4j cypher-shell -u neo4j -p argus_dev_password "RETURN 1;"
```

```bash
curl http://localhost:6333/readyz
```

## Common operations

Stop, keeping data:

```bash
docker compose down
```

Wipe everything and start clean — destroys all parsed repos, graphs, and vectors:

```bash
docker compose down -v
```

Follow one service's logs:

```bash
docker compose logs -f neo4j
```

## Troubleshooting

**Docker daemon not reachable** (`open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified`) — Docker Desktop isn't running. Launch it from the Start menu and wait; the CLI is installed independently of the daemon, so `docker` resolving on `PATH` does not mean the engine is up.

**Neo4j restarting with `Unrecognized setting`** — a `NEO4J_*` environment variable name doesn't map to a real config key. The mapping rule is that a single `_` becomes a dot and a double `__` becomes a literal underscore, so `server.memory.pagecache.size` is `NEO4J_server_memory_pagecache_size` while `server.memory.heap.initial_size` is `NEO4J_server_memory_heap_initial__size`. Neo4j validates config strictly and refuses to boot on an unknown key.

**Neo4j marked unhealthy but the browser loads** — the healthcheck runs `cypher-shell`, which tests Bolt and authentication rather than just the HTTP port. An unhealthy container with a working UI usually means a password mismatch between `.env` and the existing `neo4j_data` volume. The `NEO4J_AUTH` variable only applies on first initialization, so changing the password later requires `docker compose down -v` to reset the volume.

**Qdrant has no healthcheck** — this is deliberate. The image ships without a shell or `curl`, so any in-container probe fails and would mark a working service unhealthy. Check it from the host with `curl http://localhost:6333/readyz` instead.

**Port already in use** — something else holds 5432, 7474, 7687, or 6333. Find it with `netstat -ano | findstr :5432` on Windows, then either stop it or change the host-side port in `docker-compose.yml` and the matching value in `.env`.
