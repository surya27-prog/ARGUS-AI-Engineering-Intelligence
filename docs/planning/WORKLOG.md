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

## Thursday, 13 August 2026 at 12:02 (UTC-04:00)

Week 1, Day 4 — branch `deepu_branch` (working copy has no `.git`, so the branch
could not be read from the system; recorded from the previous entry)

- Built the parser package: `parser/parser/{models,ingest,walker,cli}.py`
- Froze the parser output schema in `models.py` — `FileInfo`, `SkippedPath`,
  `RepoInventory` plus `Language`/`SourceKind`/`SkipReason` enums
- `FileInfo` carries a path-derived dotted `module` (`app.core.config`) for
  Week 2 import resolution, and a sha256 for change detection
- Ingestion handles git URLs (shallow clone + commit/branch metadata), zips
  (zip-slip rejected, GitHub wrapper dir stripped) and local dirs (read in place)
- Walker prunes 27 vendor/cache dirs, filters to `.py`/`.pyi`, skips symlinks
  and oversized files, and records every skip with a reason
- `python -m parser.cli <source>` prints the inventory; `--json`, `--limit`,
  `--show-skipped`, `--force` supported
- Added `parser/pyproject.toml` — stdlib-only deps, ruff/pytest config matching
  the backend
- 34 tests green, ruff clean
- Verified end to end against `backend/` (12 files) and a live clone of
  `psf/requests` (37 files, 12,032 lines)

---

## Thursday, 13 August 2026 at 12:32 (UTC-04:00)

Week 1, Day 5 — branch `deepu_branch` (working copy still has no `.git`)

- Added `parser/parser/extractor.py` — Python AST extraction to dataclasses
- Extended the schema with `Symbol`, `Parameter`, `ImportRef`, `CallRef`,
  `ParsedFile`, `ParsedRepo` and the `SymbolKind`/`ParameterKind` enums
- Extracts classes, functions, methods, nested defs, decorators (with args),
  base classes, docstrings, full signatures, return annotations, async flags
- Qualnames flatten nested scopes to `outer.inner` / `Record.Meta`; call sites
  are attributed to the enclosing function
- Imports and calls are recorded as written, unresolved — relative-import depth
  and aliases preserved for Week 2's resolution pass
- Syntax errors return a `ParsedFile` with `error` set instead of raising, so
  one bad file doesn't sink the parse
- `analyze_repo()` + `python -m parser.cli <src> --symbols` wired up
- 3 fixture files (plus a deliberately broken one) and 28 extractor tests;
  62 tests green overall, ruff clean
- Ran against `psf/requests`: 807 symbols, 591 imports, 2,687 call sites,
  0 files failed to parse

---

## Thursday, 13 August 2026 at 15:25 (UTC-04:00)

Week 1, Days 6–7 — branch `deepu_branch` (working copy still has no `.git`)

Day 6 — upload page and the first vertical slice:

- Added `source_files` + `symbols` tables (`app/models/source.py`) and migration
  `7b66ffa8c249`, applied to the local database
- `app/services/parsing.py` — the parser↔API seam; parse failures are recorded
  on the repository row instead of raised
- `app/api/repos.py` — `POST /repos`, `/repos/upload`, `GET /repos`, `/{id}`,
  `/{id}/files`, `/{id}/symbols`, `POST /{id}/reparse`, `DELETE /{id}`
- Parses run as background tasks; the request returns 202 and the UI polls
- Scaffolded the Next.js 15 / React 19 frontend by hand (no CSS framework):
  ingestion form, repository list, file browser, symbol list
- Bumped Next from 15.1.6 to 15.5.23 — 15.1.6 carries CVE-2025-66478
- Fixed a real Windows bug found by running the UI: re-staging an existing clone
  died with `PermissionError [WinError 5]` because git marks `.git/objects`
  read-only and `shutil.rmtree` refuses them; added a chmod-and-retry handler
- Symbol list now fetches per selected file rather than filtering a loaded page,
  and reports "showing N of M" instead of truncating silently
- 24 backend tests green, 64 parser tests green, ruff clean, frontend builds and
  typechecks
- Verified in the browser end to end: pasted a GitHub URL, watched
  pending → parsing → complete, saw 37 files and 807 symbols listed
- Created `backend/.venv` and installed from requirements — uv was not on PATH
  on this machine despite the Day 1–3 entry

Day 7 — documentation:

- Wrote `docs/architecture/overview.md` — system diagram, component
  responsibilities, the frozen parser schema and why it is shaped that way,
  ingestion flow, API surface, design decisions, deferred scope

Not done:

- Tag `v0.1-parser` — this working copy has no `.git`, so nothing can be
  committed or tagged from here
- Week 1 demo recording

---

## Thursday, 13 August 2026 at 15:39 (UTC-04:00)

Week 1, Days 4–7 committed — branch `deepu_branch`

- Switched to `C:\dev\ARGUS-AI-Engineering-Intelligence` as the working copy;
  the `Downloads` copy has the same files but no `.git`, which is why the three
  entries above could not record a real branch
- Committed Week 1 in three commits on `deepu_branch`:
  - `954447e` Days 4–5 — parser package (17 files)
  - `33cd402` Day 6 — ingestion API and Next.js upload page (24 files)
  - `2e019cf` Day 7 — architecture overview and work log
- Days 4 and 5 are one commit: `models.py`, `walker.py` and `cli.py` were
  extended on Day 5, so the two days cannot be split cleanly after the fact
- Working tree clean; nothing pushed yet

Still outstanding from Week 1:

- Tag `v0.1-parser`
- Week 1 demo recording

---

## Thursday, 13 August 2026 at 15:53 (UTC-04:00)

Week 2, Day 1 — branch `deepu_branch`

- Wrote `docs/architecture/graph-schema.md`, committed as `5001f69`
- Node labels `Repo`, `File`, `Class`, `Function`, `Module` with properties,
  keys and the Postgres bridge; relationships `CONTAINS`, `IMPORTS`, `CALLS`,
  `INHERITS` with direction and edge properties
- Single deterministic `key` string per node instead of a composite key —
  `NODE KEY` is Neo4j Enterprise only and this project runs 5.26 Community
- Re-parse strategy is stamp-and-sweep: `MERGE` alone never removes a deleted
  file, so writes carry a `run_id` and the run deletes what it did not touch
- Resolution and confidence model for imports, calls and inheritance; ambiguous
  and unresolved call sites are counted, not guessed at
- `CALLS` edges are aggregated per caller/callee pair with a `count`, not one
  edge per call site
- Only external modules get `:Module` nodes; internal module names stay on
  `File.module`, since in Python an internal module is a file
- Included the Cypher for Friday's dependency/dependent endpoints so the schema
  is checked against its real queries now rather than on Friday

---
