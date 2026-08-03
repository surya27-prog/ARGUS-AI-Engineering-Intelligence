# Local Development Setup

## Prerequisites

| Tool | Version | Check |
|---|---|---|
| Docker Desktop | 28.x | `docker info` |
| Python | 3.11+ | `py --version` |
| Node.js | 22 LTS+ | `node --version` |
| uv | 0.11+ | `uv --version` |
| Git | 2.40+ | `git --version` |

On Windows, all four installable via winget:

```bash
winget install --id Docker.DockerDesktop OpenJS.NodeJS.LTS astral-sh.uv GitHub.cli -e
```

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
