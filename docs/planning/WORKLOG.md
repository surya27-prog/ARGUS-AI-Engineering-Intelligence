# ARGUS — Daily Work Log

Appended every time `#workdone` is used. Newest entries at the bottom.

---

## Monday, 03 August 2026 at 17:38 (UTC-04:00)

Week 1, Days 1–2

- Installed Node 24 LTS, uv, GitHub CLI, Docker Desktop
- Created project folder structure
- Wrote README, 6-week timeline, and SETUP docs
- Added `.gitignore`, `.gitattributes`, `.env.example`
- Set up Git repo and merged with remote `surya_branch`
- Built `docker-compose.yml` — Postgres, Neo4j, Qdrant all running and tested

---

## Monday, 03 August 2026 at 17:40 (UTC-04:00)

Week 1

- Created this work log
- Added `CLAUDE.md` with the `#workdone` rule and project conventions

---

## Monday, 03 August 2026 at 19:15 (UTC-04:00)

Week 1, Days 1–3 verification — branch `deepu_branch`

- Verified Days 1–3 on this machine after merging `surya_branch`; Docker, uv and
  GitHub CLI were absent locally despite the Day 1 log
- Installed uv 0.11.32, GitHub CLI 2.97.0, Docker Desktop 4.85.0 (engine 29.6.2)
- Created `.env` from `.env.example` (was missing, so compose could not start)
- Brought up the stack: postgres healthy, neo4j healthy, qdrant ready; verified
  each with psql, cypher-shell and `/readyz`
- Fixed `/health` hanging forever when Postgres is down — psycopg has no default
  connect timeout, so the `try/except` meant to report "down" never fired. Added
  `DB_CONNECT_TIMEOUT` (default 5s) and wired it into the engine
- Ran `alembic upgrade head`; `repositories` + `alembic_version` tables created
- `pytest` 4/4 green; live uvicorn `/health` returns 200 with `postgres: "up"`
- Added `backend/requirements.txt` and `requirements-dev.txt`, exported from
  `uv.lock` so pip users get the same pinned versions as uv users
- Expanded `docs/SETUP.md` with backend install, migration and run steps
- Fixed 3 pre-existing ruff failures (ignored B008 for FastAPI `Depends`,
  moved `ParseStatus` to `StrEnum`); lint now clean
- Node is v20.15.1 here, not the Node 24 the Day 1 entry claims — left as is,
  nothing needs it until the Week 1 Saturday frontend task

---
