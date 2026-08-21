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

## Monday, 17 August 2026 at 10:33 (UTC-04:00)

Week 2, Days 2-3 — branch `deepu_branch`

- Day 2 `ef91904`: Neo4j driver + graph writer — `:Repo`/`:File`/`:Class`/
  `:Function` nodes and the `CONTAINS` tree, batched `UNWIND ... MERGE` on the
  node key, stamp-and-sweep per `run_id`
- Driver is built lazily so importing it cannot stop the API starting when
  Neo4j is down; `/health` now reports Neo4j and the pool closes on shutdown
- Symbol keys fall back to the file path outside a package — the schema doc
  allowed an empty module, which would have fused two unrelated `helper`
  functions into one node. Amendment written into the doc
- Parser path bootstrap moved to `app/services/__init__.py`, now two modules
  need it
- Day 3 `d4ad76c`: import resolution — `IMPORTS` edges and the first `:Module`
  nodes, resolution as a pure function in `import_resolver.py`
- Relative imports resolve against the importing file's package; a package's
  `__init__.py` is the package, so one fewer component comes off it
- Over-deep relative imports recorded as external with their literal text
- Second schema-doc amendment: `internal_symbol` does not verify the trailing
  name is an extracted symbol, since module-level variables are not extracted
- Verified against `psf/requests`: 37 files, 591 import refs → 90 relative,
  20 internal_symbol, 8 internal_module, 207 external; `sessions.py`
  spot-checked correct. Re-parsed twice, nothing swept
- 65 backend + 64 parser tests green
- Found and fixed a pre-existing problem: the Postgres volume was one migration
  behind head, so `source_files` and `symbols` did not exist and 7 Week 1 tests
  were failing. `alembic upgrade head` applied
- Docker Desktop crashes on startup on its Inference manager (unix socket at a
  Windows path). Worked around by restarting, not fixed — `EnableDockerAI` is
  still true
- Not done: `parser/uv.lock` left untracked; nothing pushed

---

## Monday, 17 August 2026 at 11:50 (UTC-04:00)

Week 2, Days 4-7 — branch `deepu_branch`

- Day 4 `01fe2fa`: call graph and inheritance — four confidence tiers, `CALLS`
  aggregated per pair, `INHERITS` resolved first because `attribute_self` needs
  the hierarchy
- Running Day 4 against `psf/requests` found three things: `CALLS` must be able
  to target a `:Class` (constructor calls were being dropped silently),
  re-exports need following (`requests/__init__.py` does `from .api import get`
  — worth 275 call sites), and ambiguity is 7 sites out of 2510, so the
  deferred fan-out idea was closed rather than built
- Day 5 `0998c0e`: graph query API — `/graph`, `/graph/search`,
  `/dependencies`, `/dependents`
- Day 1's draft Cypher does not compile: `*1..$depth` is a syntax error, Cypher
  cannot parameterise a variable-length bound. Depth is now an interpolated
  literal validated as a bounded int. Writing that Cypher a week early is what
  made this cheap
- Day 6 `d83917c`: `parse_jobs` table, migration `a1b08af925a7`. The job's
  `run_id` is the graph's stamp, so a Postgres row names the subgraph it wrote
- Fixed a bug this week introduced: `DELETE /repos/{id}` left the whole Neo4j
  subgraph orphaned
- Day 7 `6608d4e`: ran the week's demo against a real uvicorn server, which
  found a bug TestClient cannot — it runs background tasks before returning, so
  the pipeline looked atomic. The repository was marked `complete` before the
  graph write, so a poller could reach an empty `/dependents`. `complete` is now
  published in the same commit as the finished job
- Updated `docs/architecture/overview.md` to end of Week 2
- Tagged `v0.1-parser` (Week 1, was outstanding) and `v0.2-graph`
- Week 2 demo verified end to end on `psf/requests`: 202 in ~210ms, parsing →
  complete, 937 nodes and 1929 relationships, re-parse sweeps nothing,
  dependents of `Session.request` is exactly its seven HTTP verb methods,
  DELETE clears both stores
- 132 backend + 64 parser tests green, ruff clean, migrations at head
- Not done: nothing pushed — all of Week 2 is local; `parser/uv.lock` still
  untracked; Week 1 demo recording still outstanding

---

## Monday, 17 August 2026 at 12:04 (UTC-04:00)

Week 3, Day 1 — branch `deepu_branch`

- `0fe0ced`: two provider interfaces, `ChatProvider` and `EmbeddingProvider`,
  selected by `LLM_PROVIDER` and `EMBEDDING_PROVIDER` independently
- Anthropic chat provider on the official SDK; OpenAI embedding provider;
  `stub` and `hash` providers that run offline with no key
- No `temperature` on the chat interface — it was removed on current Claude
  models and returns a 400. `effort` is the supported control, and a test
  asserts the parameter cannot creep back in
- A refusal is an HTTP 200 with empty content, so `ChatResponse` carries
  `stop_reason` and a `refused` property
- `EmbeddingBatch` carries its model and width; the OpenAI provider refuses to
  construct when the model's real width disagrees with `EMBEDDING_DIMENSIONS`
- Token counting is on the interface, since tokenizers are provider-specific
- `/health` reports the selected providers and whether they have credentials,
  read from config rather than by calling them
- `.env.example` was advertising `ollama` and a local embedding provider that
  were never implemented — removed, `LLM_EFFORT` added
- 156 backend tests green, all offline; swapped both providers by env var and
  exercised complete, stream, count_tokens, embed and check
- Not done: no API keys set yet, so neither real provider has been called.
  Day 2 needs an `OPENAI_API_KEY` (or a local embedding provider) to fill
  Qdrant. Nothing pushed; `parser/uv.lock` still untracked

---

## Monday, 17 August 2026 at 17:19 (UTC-04:00)

Week 4, Days 1-3 — branch `deepu_branch`

- Day 1: impact analysis — reverse traversal of `CALLS`/`IMPORTS` scored as
  `confidence * decay^(hops-1)`; score picks each node's representative route,
  not hop count. `GET /repos/{id}/impact`
- Day 2: git history ingestion — `git log --name-status` to co-change pairs
  with Jaccard, renames followed, mass commits dropped. `CO_CHANGED` edges plus
  `change_count` on `:File`. `GET /repos/{id}/cochange`
- Day 2: clone depth raised from 1 to 500; two new `parse_jobs` columns and a
  migration; co-change failures are logged, not fatal
- Day 3: risk score — blast/coverage/centrality/coupling/churn weighted into
  0-100, saturating normalisation, missing history renormalises the weights.
  `GET /repos/{id}/risk`, formula in `docs/architecture/risk-model.md`
- 288 backend + 92 parser tests green; ruff clean
- Verified end-to-end against this repo's own graph and history: 1459
  functions scored, `get_settings` at 72.9 (high)
- Not done: nothing pushed to the remote; Days 4-7 (Cytoscape graph,
  interactivity, LLM-explained impact) not started

---

## Thursday, 20 August 2026 at 15:48 (UTC-04:00)

Week 4, Day 5 — branch `deepu_branch`

- Blast radius on selection: clicking a node loads `/impact` and lights it up —
  root white, dependents amber fading with hop distance, rest at 10% opacity,
  and the actual routes highlighted, not just the endpoints
- Side panel ranks affected nodes with hops and score; rows outside the fetched
  slice are shown greyed and non-clickable rather than as dead links
- Colour by risk as well as by type, using the API's own bands; nodes outside
  the top 200 render grey and say "unscored, not safe"
- Type filter per node label, last label unremovable
- Collapse by module into group nodes sized by member count, edges remapped and
  merged between groups, expand one from the selection panel
- Added `frontend/src/lib/graphView.ts` — view transforms as pure functions;
  cap-hidden and filter-hidden node counts reported separately
- Risk fetched lazily on first switch; colour changes repaint via style mappers
  instead of rebuilding elements, so the layout, pan and zoom survive
- Installed `cytoscape` — Day 4 declared it in `package.json` but it was never
  in `node_modules`, so the graph page could not have typechecked
- Frontend typechecks and builds; committed as `9a8c5de`

Not done:

- **Day 5 not verified in a browser** — Docker Desktop is paused on this
  machine, so there was no API to click against. The done-when ("click a
  function, see it light up its dependents") is unconfirmed
- Day 4 has no work log entry; not written here since it was a different session
- Nothing pushed to the remote

---

## Friday, 21 August 2026 at 09:50 (UTC-04:00)

Week 4, Day 6 — branch `deepu_branch`

- `services/impact_explain.py` + `GET /repos/{id}/impact/explain` — the blast
  radius in prose, committed as `1b9a923`
- Context carries signatures and first docstring lines joined from Postgres,
  not just node names; the graph stores none of that, and the "because Y" has
  to come from somewhere
- An empty radius never calls the model — deterministic text instead, keeping
  the caveat about unresolved and dynamic calls
- Prompt requires confidence to be honoured; a guessed edge stated as fact is
  the failure mode being designed against
- Not streamed, unlike `/chat` — one cacheable JSON response beside a graph
- Cached per `(repo, key, depth, commit_sha)`, per process; not shared between
  workers and lost on restart
- Provider failure returns 503 saying the radius was computed but not explained,
  so the UI keeps the ranked list
- Chat wiring is an explicit `focus_key` on the request, not intent detection;
  a stale key degrades to an unfocused answer. Chat system prompt gained a
  paragraph on what a `Blast radius` block is
- Frontend: explicit "Explain this in English" button (automatic would bill a
  call per node clicked), a footer saying how many affected nodes the prose was
  read from, and a chat link carrying the focus key
- 20 new tests; lint clean; frontend typechecks and builds

Not done:

- **Backend tests not run** — Docker Desktop is paused, so Neo4j and Postgres
  are unreachable. Only the 4 tests needing neither were executed; the other 16
  collect but are unverified. Same for Day 5's browser check
- Nothing pushed to the remote

---

## Friday, 21 August 2026 at 11:32 (UTC-04:00)

Week 4, Day 7 — branch `deepu_branch`

Buffer day spent clearing the verification debt from Days 5 and 6.

- Full suites green against the live stack: **308 backend + 92 parser = 400
  tests**, ruff clean on both
- Applied 4 pending migrations that had never run on this machine
  (`parse_jobs`, embedding columns, conversations/chat_messages, cochange columns)
- Re-parsed the `requests` fixture: it was parsed in Week 1, before the graph
  writer existed, so its Neo4j graph was empty. Now 322 nodes / 400 edges in the
  calls view, 728 nodes scored for risk
- Verified `/impact` on real data: `is_prepared` → 17 affected, 9 direct, max 2
  hops, routes correct
- Verified `/impact/explain` end to end. The stub provider echoes its prompt
  back, which confirms the Day 6 design on real data: the context carried
  `src/requests/_types.py:47`, the full annotated signature and the docstring's
  first line — all joined from Postgres, which is where the "because Y" comes from
- Verified the explanation cache: second call returned `cached: true`, 0 output
  tokens
- Both new frontend routes compile and serve 200
- Tagged `v0.4-impact`

Notes for Week 5:

- This machine has **no `.env`** — every setting falls back to its default, so
  `LLM_PROVIDER=anthropic` with an empty key. The verification above ran with
  `LLM_PROVIDER=stub` passed to the process, leaving no trace on disk. Real
  answers need `.env` created from `.env.example`
- The running Qdrant container reports **1.12.5** while `docker-compose.yml`
  pins `v1.19.0` and the client is 1.19.0 — a stale container from before the
  pin was bumped. Left alone rather than recreated on a buffer day, since a
  1.12→1.19 storage migration could force a re-embed

Not done:

- **Day 5's browser check** — the Chrome extension disconnected mid-session, so
  "click a function, see it light up its dependents" is still unconfirmed in a
  real browser. The data behind it is verified; only the rendering is not
- Nothing pushed to the remote

---
