# ARGUS — Performance

**Target** (from the Week 5 plan): a 1,000-file repository parsed in under five
minutes, and every list endpoint bounded.

> **Measured 21 September 2026** on `psf/requests` (37 files, 807 symbols), with
> `scripts/profile_pipeline.py` against a local Docker stack — Postgres 16, Neo4j
> 5.26, Qdrant v1.19.0 — on a Windows 11 development machine. These are one
> machine's numbers, not a benchmark: quote the ratios, not the milliseconds.

---

## How to measure

```bash
cd backend
LLM_PROVIDER=stub EMBEDDING_PROVIDER=hash \
  .venv/Scripts/python.exe scripts/profile_pipeline.py https://github.com/psf/requests.git
```

The script ingests a repository from scratch, reads the per-stage timings back
off the `parse_jobs` row it produced, then times ten endpoints at the median of
five calls each, and prints both tables in Markdown ready to paste below.

Two deliberate choices in how it measures:

- **Median of five, not one call.** A single call measures the cold cache, the
  connection handshake and the query in one number, and reports whichever
  dominated.
- **In-process via `TestClient`, not over HTTP.** The interesting cost is the
  query, not the loopback. A real deployment adds network time on top of these
  figures rather than changing their ratios.

Use `EMBEDDING_PROVIDER=hash` to time the pipeline without a paid API in the
loop. The embedding stage's *wall clock* with a real provider is dominated by
that provider's latency, not by ARGUS, so measuring it with the hash provider
isolates what this codebase controls.

---

## Where a parse spends its time

Every parse now records this itself, so this table is a snapshot of one run
rather than the only place the numbers exist. `parse_jobs.stage_ms` holds the
same breakdown for every run ever performed, and `GET /repos/{id}/jobs` exposes
it.

The five stages, in order:

| Stage | What it does | Bound by |
|---|---|---|
| `analyze` | clone or unzip, walk the tree, parse every file's AST | disk and CPU; the clone is network |
| `store` | write files and symbols to Postgres | one bulk insert per file |
| `graph` | write nodes and edges to Neo4j | round trips, batched at `BATCH_SIZE` |
| `cochange` | `git log` to co-change pairs | history depth, capped at `history_depth` |
| `vectors` | chunk by symbol, embed, upsert to Qdrant | the embedding provider |

### Measured

| Stage | ms | % of parse |
|---|---:|---:|
| analyze | 2,194 | 35.6% |
| store | 285 | 4.6% |
| graph | 1,214 | 19.7% |
| cochange | 56 | 0.9% |
| vectors | 2,411 | 39.1% |
| **total** | **6,160** | 100% |

**Nothing here needs optimising, and that is the useful result.** Six seconds for
37 files leaves room against the five-minute target, and the two stages that
dominate — parsing ASTs and embedding — are the two doing irreducible work.

`vectors` at 39% is measured with the **hash** provider, so it is the chunking and
the Qdrant upsert, not a model. With a real embedding provider this stage is
dominated by that provider's latency and will grow far beyond the others, which is
what makes the unused `SourceFile.sha256` re-parse skip the largest available
saving.

A second parse of the same repository recorded a different split — graph 5,077ms
(47%) against vectors 2,206ms — on a cold Neo4j page cache. Warm and cold differ
by roughly 4× on that stage, so a single run is a sample, not a measurement.

---

## Endpoint latency

### Measured

Median of five calls, in-process via `TestClient`. The **worst** column is the
first call of the five — a cold cache — which is why it is reported separately
rather than averaged away.

| Endpoint | median ms | worst ms |
|---|---:|---:|
| `GET /repos/{id}` | 11 | 36 |
| `GET /files` (200) | 16 | 19 |
| `GET /symbols` (200) | 26 | 29 |
| `GET /graph` (files, 400) | 18 | 240 |
| `GET /graph` (calls, 400) | 16 | 228 |
| `GET /risk` (File, 120) | 18 | 741 |
| `GET /risk` (Function, 10) | 15 | 586 |
| `GET /debt` | 15 | 688 |
| `GET /cochange` | 17 | 165 |
| `GET /conversations` | 15 | 49 |

**The gap between the two columns is the cache, and it is the whole argument for
it.** `/risk` costs 741ms cold and 18ms warm — a 41× difference — because scoring
reads the entire repository to answer one request. The dashboard asks for four of
these on load, so without the cache every visit would pay two seconds; with it,
only the first visit after a parse does.

The endpoints that barely move — files, symbols, conversations — are the ones
answered by a single indexed Postgres query, and they need no cache at all. That
split is why the cache is on three endpoints rather than applied globally.

---

## What was changed, and why

### Neo4j writes were already batched

Worth recording as a non-finding. The graph writer has used `UNWIND` with a
batch size since Week 2 Day 2, so the plan's "batch Neo4j writes" item needed no
work. A `MERGE` per node would have been the obvious hot spot; it was never
written that way.

### Per-stage timing on every parse

`parse_jobs` recorded one `duration_ms` for a whole run. That tells you a parse
took four minutes and nothing about which stage spent them, which is the only
version anyone can act on. There is now a `stage_ms` JSONB column, written by a
context manager around each stage.

It records on the way out whether or not the stage raised, so a *failed* parse
still says how long the failing stage had been running — the case where the
timing matters most, and the one a profiler run would never capture.

### Three expensive reads are cached

`/risk`, `/debt` and `/graph` each read the whole repository to answer one
request, and the dashboard asks for four of them on load. None of their answers
change between parses.

**The cache key is the parse, not the clock.** `app/core/cache.py` keys entries
on `(repository_id, Repository.parsed_at, variant)`. A TTL would have been a
guess about acceptable staleness, wrong in both directions — too long and a
re-parsed repository serves the old graph, too short and the cache never helps.
Keying on `parsed_at` makes an entry valid exactly as long as the parse behind it
is current, invalidates on re-parse with nothing having to remember to, and costs
no extra query: `parsed_at` is already on the repository row the endpoint loaded
to check for a 404.

Deliberate limits:

- **Per-process.** Lost on restart, not shared between workers. The alternative
  is adding Redis to the stack for something that only saves recomputation, and a
  cold cache is slow rather than wrong. Week 6 can revisit if deployment shows it
  matters.
- **Computed outside the lock.** Two callers arriving on a cold cache may both
  compute. That wastes a little work; holding the lock instead would block the
  second caller for the *entire* duration of the first's scan, which is worse.
- **A repository with no completed parse is never cached.** `parsed_at` of `None`
  means there is nothing stable to key against.
- **Deletion invalidates explicitly.** `parsed_at` retires stale entries, but a
  deleted repository has no next parse to retire them, so `DELETE /repos/{id}`
  clears them rather than leaving scans in memory until eviction.

### The one unbounded endpoint

`GET /conversations` returned `ConversationOut`, which carries every message, with
the relationship left on lazy loading. A page of 50 conversations was **51
queries**, each shipping full message bodies and citation payloads — to render
what is only ever a picker. It now returns `Page[ConversationSummaryOut]` with
`message_count` from a single aggregate query, and takes `limit`/`offset` like
every other list endpoint.

Every other endpoint was already capped. The audit is worth stating as a result:
`/graph` (≤5000), `/graph/search` (≤200), `/dependencies` and `/dependents`
(≤1000, depth ≤5), `/impact` (≤1000), `/risk` (≤200), `/cochange` (≤1000),
`/search` (top-k bounded), `/debt` (≤2000), `/files` and `/symbols` (≤1000),
`/repos` (≤200), `/jobs` (≤100).

---

## Known costs not yet addressed

- **The circular-import query** matches variable-length `IMPORTS` paths up to
  depth 6 and takes `LIMIT 1000`. It is the most expensive single query in the
  debt scan, and its cost grows with import density rather than repository size.
  On `psf/requests` it returned 200 rows before minimal-cycle filtering cut them
  to 9 — the filtering is in Python, so the query still pays for all 200.
- **`impact_explain` has its own cache**, predating `app/core/cache.py` and
  keyed on `commit_sha` rather than `parsed_at`. Two cache implementations is one
  too many; it should move.
- **Embedding cost scales with symbol count**, and the vector pass re-embeds
  every symbol on every parse. `SourceFile.sha256` exists precisely so unchanged
  files could be skipped, and nothing uses it yet. This is likely the single
  largest available saving on a re-parse.
