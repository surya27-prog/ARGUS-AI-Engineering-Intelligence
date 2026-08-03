# ARGUS — 6-Week Delivery Timeline

**Project:** ARGUS – AI Engineering Intelligence Platform
**Repo:** https://github.com/DeepuChandru/ARGUS-AI-Engineering-Intelligence
**Window:** Mon 3 Aug 2026 → Sun 13 Sep 2026
**Working model:** one shared track (no person-split). Mon–Sat are work days, Sunday is review + buffer.

---

## Ground rules

1. **Every day ends with a commit.** Even a broken-but-committed day beats a perfect uncommitted one.
2. **Demoable by end of every week.** Week N must run end-to-end, even if thin.
3. **Vertical slices, not horizontal layers.** Build one feature all the way through (parser → DB → API → UI) before starting the next.
4. **Sunday = catch-up.** If you slip, Sunday absorbs it. Do not push slippage into the next week.
5. **One target repo for testing** — pick a mid-sized real Python repo (e.g. `fastapi` or `requests`) and use it as the fixture all 6 weeks. Consistent test data makes progress visible.

---

## Week-level view

| Week | Dates | Theme | Ships by Sunday |
|---|---|---|---|
| 1 | 3–9 Aug | Foundation + parser skeleton | Upload a repo, see parsed file/symbol list in a web page |
| 2 | 10–16 Aug | Knowledge graph | Dependency graph stored in Neo4j, queryable via API |
| 3 | 17–23 Aug | RAG + AI chat | Ask a question about the codebase, get a cited answer |
| 4 | 24–30 Aug | Impact analysis + visualization | "If I change X, what breaks?" with an interactive graph |
| 5 | 31 Aug–6 Sep | Tech debt, risk score, hardening | Risk dashboard + debt report, tests green in CI |
| 6 | 7–13 Sep | Deploy, docs, demo | Live URL, README, demo video, portfolio-ready |

**Hard scope cuts if behind** (drop in this order): cloud deployment → tech debt detection → auth → multi-language parsing (Python only).

---

## Week 1 — Foundation & Parser Skeleton (3–9 Aug)

Goal: a repo goes in, structured facts come out, and you can see them in a browser.

| Day | Date | Work | Done when |
|---|---|---|---|
| Mon | 3 Aug | Install toolchain (Node LTS, Docker Desktop, Poetry/uv). Push folder structure + README + .gitignore. Branch protection on `main`. | `git clone` on a fresh machine works |
| Tue | 4 Aug | `docker-compose.yml`: Postgres + Neo4j + Qdrant. Verify all three come up and you can connect to each. | `docker compose up` → 3 healthy containers |
| Wed | 5 Aug | FastAPI skeleton: `/health`, config via pydantic-settings, `.env.example`, SQLAlchemy engine + first migration (Alembic). | `GET /health` returns 200, `alembic upgrade head` works |
| Thu | 6 Aug | Parser package: clone/unzip a repo to a workspace dir, walk files, filter by extension, ignore `node_modules`/`.git`/`venv`. | `python -m parser.cli <path>` prints file inventory |
| Fri | 7 Aug | Python AST extraction: functions, classes, imports, call sites → dataclasses. Unit tests on 3 fixture files. | `pytest parser/tests` green |
| Sat | 8 Aug | Next.js app + upload page. `POST /repos` (accept git URL or zip) → kicks off parse → `GET /repos/{id}/files`. Wire UI to it. | Paste a GitHub URL in the browser, see the file/symbol list |
| Sun | 9 Aug | Buffer. Write `docs/architecture/overview.md`. Tag `v0.1-parser`. | Week 1 demo recorded (30s screen capture, for yourself) |

**Risk:** Neo4j/Qdrant on Docker eating a day. If containers fight you Tuesday, fall back to Neo4j Aura free tier + Qdrant Cloud free tier and move on.

---

## Week 2 — Knowledge Graph (10–16 Aug)

Goal: the parsed facts become a real graph you can query.

| Day | Date | Work | Done when |
|---|---|---|---|
| Mon | 10 Aug | Design graph schema on paper: nodes (`Repo`, `File`, `Module`, `Class`, `Function`) + edges (`CONTAINS`, `IMPORTS`, `CALLS`, `INHERITS`). Write it into `docs/architecture/graph-schema.md`. | Schema doc reviewed and committed |
| Tue | 11 Aug | Neo4j driver + repository layer. Idempotent `MERGE` writes so re-parsing doesn't duplicate. | Parse twice → node count unchanged |
| Wed | 12 Aug | Import-resolution pass: turn `import x.y` into real edges between file nodes. Handle relative imports. | Graph of the fixture repo has >0 `IMPORTS` edges, spot-checked correct |
| Thu | 13 Aug | Call-graph pass: resolve `foo()` to a defined `Function` node where possible. Accept partial resolution — record confidence. | `CALLS` edges exist; unresolved calls logged, not crashed |
| Fri | 14 Aug | Graph query API: `GET /repos/{id}/graph`, `/dependencies/{node}`, `/dependents/{node}` (upstream + downstream, depth-limited). | Cypher-backed endpoints return JSON in <1s on fixture repo |
| Sat | 15 Aug | Persist repo/job metadata in Postgres (repo record, parse status, timestamps). Background job for parsing so the upload request doesn't block. | Upload returns immediately, UI polls status → "complete" |
| Sun | 16 Aug | Buffer. Tag `v0.2-graph`. | Week 2 demo: upload → graph populated → query dependents |

**Note:** Authentication is deliberately deferred. It adds no demo value and can be added Week 5 in half a day.

---

## Week 3 — RAG + AI Chat (17–23 Aug)

Goal: ask ARGUS a question in English, get an answer grounded in the actual code.

| Day | Date | Work | Done when |
|---|---|---|---|
| Mon | 17 Aug | **Pick the LLM provider** (decision deferred from Week 1). Write `LLMProvider` interface — `complete()`, `embed()` — with one concrete impl behind it, config-selected. | Swapping provider = one env var |
| Tue | 18 Aug | Chunking strategy: chunk by *symbol* (function/class body + docstring + file path header), not fixed token windows. Embed → Qdrant with metadata payload. | Fixture repo fully embedded, collection count sane |
| Wed | 19 Aug | Retrieval: semantic search endpoint. Test with 10 hand-written questions, eyeball whether the right chunks come back. Tune `top_k` and chunk size. | 8/10 questions retrieve the correct symbol in top 5 |
| Thu | 20 Aug | **Hybrid retrieval — the differentiator.** Combine vector hits with graph expansion: retrieve a function, then pull its callers/callees from Neo4j into context. | Answers reference related code the pure-vector search missed |
| Fri | 21 Aug | Chat endpoint: prompt template with retrieved context + citation instructions, streaming response, conversation history in Postgres. | `POST /chat` streams a grounded answer with file:line citations |
| Sat | 22 Aug | Chat UI: message list, streaming render, citation chips that link to the source file view. | Full chat works in the browser |
| Sun | 23 Aug | Buffer. Write `docs/architecture/rag-design.md` explaining the hybrid approach — this is your interview talking point. | Tag `v0.3-chat` |

**Risk:** this is the heaviest week. If Thursday's hybrid retrieval isn't working by Friday morning, ship pure vector RAG and revisit in Week 5.

---

## Week 4 — Impact Analysis & Visualization (24–30 Aug)

Goal: the headline feature — "if I change this, what breaks?"

| Day | Date | Work | Done when |
|---|---|---|---|
| Mon | 24 Aug | Impact algorithm: reverse-traverse `CALLS`/`IMPORTS` from a target symbol, N levels deep, with per-hop decay. Return affected nodes + paths. | `GET /impact/{node}` returns a ranked blast radius |
| Tue | 25 Aug | Git history ingestion: `git log` → co-change frequency between files. Files that historically change together are coupled even without a static edge. | Co-change edges in the graph |
| Wed | 26 Aug | Risk score: combine blast radius + co-change + centrality + test coverage proxy into a 0–100 score. Document the formula — you *will* be asked to justify it. | Score returned per symbol, formula in `docs/architecture/risk-model.md` |
| Thu | 27 Aug | Cytoscape.js graph component: render nodes/edges, layout, zoom/pan, click-to-select. | Fixture repo renders without freezing the browser |
| Fri | 28 Aug | Graph interactivity: highlight blast radius on selection, color by risk, filter by node type, collapse by module. Handle big graphs (cap visible nodes, expand on demand). | Click a function → see it light up its dependents |
| Sat | 29 Aug | LLM-explained impact: feed the impact set to the LLM → plain-English "changing this breaks X because Y". Wire into both the graph panel and chat. | Natural-language impact summary appears alongside the graph |
| Sun | 30 Aug | Buffer. Tag `v0.4-impact`. | **Midpoint check: this is the demo that sells the project. It must look good.** |

---

## Week 5 — Tech Debt, Hardening, Polish (31 Aug – 6 Sep)

Goal: from "impressive prototype" to "believable product".

| Day | Date | Work | Done when |
|---|---|---|---|
| Mon | 31 Aug | Tech debt detectors: cyclomatic complexity, god files, circular imports, dead code (unreferenced symbols), missing docstrings. | Detector suite runs, produces findings on fixture repo |
| Tue | 1 Sep | Debt report: aggregate + rank findings, `GET /repos/{id}/debt`, exportable as Markdown/JSON. | Report endpoint + downloadable file |
| Wed | 2 Sep | Dashboard page: repo stats, risk heatmap, top-10 riskiest files, debt summary cards. | Landing dashboard looks like a product, not a demo |
| Thu | 3 Sep | Performance pass: profile the parse pipeline, batch Neo4j writes, cache graph queries, paginate everything. Target: 1000-file repo parsed in <5 min. | Timing numbers recorded in `docs/performance.md` |
| Fri | 4 Sep | Test + CI: raise backend coverage to ~60% on core logic, GitHub Actions running lint + tests on every push. Add auth here if you still want it (JWT, one afternoon). | CI badge green on `main` |
| Sat | 5 Sep | Error handling + UX: loading states, empty states, error toasts, input validation, rate limiting on LLM endpoints. Kill every unhandled exception you can trigger. | You can't break the app in 15 minutes of trying |
| Sun | 6 Sep | Buffer. Tag `v0.5-rc`. | Feature freeze — nothing new after today |

---

## Week 6 — Deploy, Document, Demo (7–13 Sep)

Goal: a link you can put on a CV.

| Day | Date | Work | Done when |
|---|---|---|---|
| Mon | 7 Sep | Production Dockerfiles (multi-stage, non-root), `docker-compose.prod.yml`, secrets via env, health checks. | Full stack runs from prod compose locally |
| Tue | 8 Sep | Deploy: managed Postgres + Neo4j Aura + Qdrant Cloud, backend on Railway/Render/Fly, frontend on Vercel. HTTPS, CORS, env config. | Public URL loads and works |
| Wed | 9 Sep | Smoke-test production with a real repo end to end. Fix what only breaks in prod (it will). Add basic logging/monitoring. | A stranger could use the live URL without you present |
| Thu | 10 Sep | README: problem, architecture diagram, feature list with screenshots, tech stack, local setup, API overview. This is the most-read file in the project — spend the whole day. | README reads like a real OSS project |
| Fri | 11 Sep | Remaining docs: architecture deep-dive, API reference, design decisions + trade-offs, known limitations, future scope. | `docs/` complete |
| Sat | 12 Sep | Demo video (4–6 min): problem → upload → graph → chat → impact analysis → debt report. Script it, record clean takes. | Video uploaded, linked in README |
| Sun | 13 Sep | Final polish: repo topics, description, LICENSE, pinned on profile. Write a LinkedIn/portfolio post. | **Project shipped** |

---

## Dependency-order cheat sheet

Nothing here is optional-order — each unlocks the next:

```
parser output schema  →  graph schema  →  graph queries  →  impact analysis  →  risk score
                      ↘  chunking      →  embeddings     →  retrieval        ↗
```

Freeze the parser's output schema in Week 1. Changing it in Week 3 costs two days.

---

## Weekly checkpoint questions

Ask these every Sunday. Two "no"s means cut scope, not add hours.

- Does the app run end-to-end from a clean clone?
- Is everything committed and pushed?
- Could I demo this week's work to someone in 3 minutes?
- Is next week's first task unambiguous?
