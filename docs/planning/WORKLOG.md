# ARGUS — Daily Work Log

Running record of work completed on the project. A new entry is appended every
time `#workdone` is used. Newest entries are at the bottom, so the file reads
top-to-bottom as the project progresses.

Timeline this log tracks: [`TIMELINE.md`](TIMELINE.md)

---

## Monday, 03 August 2026 at 17:38 (UTC-04:00)

**Week 1, Days 1–2 — Foundation and data stack**

Set up the development toolchain, project scaffolding, and the three-database
stack ARGUS runs on.

- Installed Node.js 24.19.0 LTS, npm 11.17.0, uv 0.11.32, and GitHub CLI 2.97.0;
  started Docker Desktop 28.3.0
- Created the project structure (`backend/`, `frontend/`, `parser/`, `database/`,
  `docs/`, plus `infra/`, `scripts/`, and `.github/workflows/`)
- Wrote the README, the 6-week day-by-day delivery plan, and `docs/SETUP.md`
- Added `.gitignore`, `.gitattributes` for LF line endings, and `.env.example`
  covering every service and both LLM provider slots
- Initialized the repo, merged with the existing remote `surya_branch`, and
  untracked the local Claude settings file
- Built `docker-compose.yml` for PostgreSQL 16.14, Neo4j 5.26 (with APOC), and
  Qdrant 1.12.5, and verified all three with real reads and writes

Two fixes worth remembering: the Neo4j page-cache environment variable needs a
single underscore (`NEO4J_server_memory_pagecache_size`), and the Neo4j
healthcheck now uses `cypher-shell` so it tests Bolt and authentication rather
than just the HTTP port.

**Next:** Week 1 Day 3 — FastAPI skeleton with `/health`, pydantic-settings
configuration, the SQLAlchemy engine, and the first Alembic migration.

---
