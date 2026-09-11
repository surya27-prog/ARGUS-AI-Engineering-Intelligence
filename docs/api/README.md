# API reference

25 operations across 8 groups. Interactive docs at `/docs` on a running instance;
[`openapi.json`](openapi.json) is the generated schema, exact shapes and all.

That file is produced by `backend/scripts/export_openapi.py` rather than
maintained by hand — a hand-written reference drifts the first time a query
parameter changes and nobody notices for a month. Regenerate it after any change
to a route:

```bash
cd backend && python scripts/export_openapi.py
```

This page covers the conventions, which the schema cannot express: what a page
looks like, what an error looks like, which endpoints cost money, and which
answers are honest about being incomplete.

---

## Conventions

### Everything that lists is paginated the same way

```json
{ "items": [...], "total": 1284, "limit": 200, "offset": 0 }
```

`total` is the count *before* the page was taken, which is what stops a page being
mistaken for the whole result. Where a filter narrows things, the summary reports
both — `/debt` returns `total` alongside `scanned_total`, so a narrow filter cannot
be misread as a clean repository.

Every `limit` has a ceiling and an out-of-range value is a `422`, not a silently
clamped one: a caller asking for 99999 has made a mistake worth telling them about.

### Errors have one shape

```json
{ "detail": "The graph database is not reachable. …", "error_id": "a3f19c22" }
```

`detail` is always present and always a sentence. `error_id` appears on 5xx and is
the same string as the `X-Request-ID` response header and the server log line — so
"it broke, id a3f19c22" is enough to find the traceback.

| Status | Means |
|---|---|
| `202` | Accepted — a parse was queued. Poll `GET /repos/{id}` |
| `404` | No such repository, node or conversation |
| `409` | The repository is not in a state for this. Scanning debt before a parse completes, or re-parsing an upload that has no URL to re-fetch |
| `422` | The request is wrong. Field names are named; the submitted value is not echoed back |
| `429` | Rate limited. `Retry-After` says how long |
| `500` | A bug in ARGUS. Carries an `error_id` |
| `503` | A dependency is down, or a provider is unconfigured. Says which |

**A 503 is not a 500.** Neo4j unreachable, Postgres refusing connections and a
missing API key are all dependency problems a caller can act on — retry, or
configure a key. They are deliberately not reported as internal errors.

### Three endpoints cost money

`POST /chat`, `GET /search` and `GET /impact/explain` call a paid provider, and are
rate limited per client with a token bucket:

| Endpoint | Sustained | Burst |
|---|---|---|
| `/chat` | 10/min | 4 |
| `/impact/explain` | 20/min | 6 |
| `/search` | 60/min | 15 |

Burst above the sustained rate is deliberate — three quick questions is normal
use. A `429` carries `Retry-After`, because without it a client can only guess and
guessing means retrying immediately and being refused again.

With no API key configured these return `503` naming the unconfigured provider.
**Everything else still works** — files, symbols, the graph, impact, risk and debt
need no model at all.

### Confidence is part of the answer

Anything derived from static analysis of a dynamic language reports how sure it
is, rather than presenting an inference as a fact:

- `CALLS` edges carry a `resolution` and a `confidence` — `local` 1.0, `imported`
  0.95, `attribute_module` 0.90, `attribute_self` 0.85
- impact multiplies those along the path, so a blast radius is weighted by how
  certain each hop is
- dead-code findings never exceed `0.75`, because a call at module scope creates
  no edge and would read as unused
- ambiguous call sites produce **no edge** and are counted on the caller as
  `unresolved_calls`, since a guess that inflates a blast radius is worse than a
  gap you can see

---

## The groups

### `health`

`GET /health` — always `200` while the process is serving, with each store's
reachability in the body. That is deliberate: a liveness probe that failed when
Postgres blipped would restart a container that is working. Also reports
`llm_configured` and `embedding_configured`, which is the quickest way to tell
whether a key took.

### `repositories`

Ingestion and browsing. `POST /repos` takes a git URL, `POST /repos/upload` a zip;
both return `202` immediately, because a real repository takes minutes — far longer
than any sensible HTTP timeout. Poll `GET /repos/{id}` until `status` settles on
`complete` or `failed`.

`GET /jobs` is the parse history with per-stage timings in `stage_ms`, which is
where "why was that repository slow" is answerable. `Repository.status` is
overwritten by each re-parse; the job rows are not.

The URL on `POST /repos` is the most validated input in the API — it becomes an
argument to `git clone`. Credentials in it, private and loopback addresses,
control characters and oversized values are all refused, each with a reason.

### `graph`

`GET /graph` returns nodes and edges capped for rendering, in one of two views:
`files` (files and `IMPORTS`) or `calls` (symbols and `CALLS`). `graph/search`
finds a node key by name, path or qualname — every other endpoint that takes a
`key` expects one from here.

`dependencies` and `dependents` are the same traversal with the arrow reversed,
depth-limited.

### `impact`

`GET /impact` is the blast radius: everything that reaches a node, ranked by
`confidence × decay^(hops-1)`, each row carrying the `route` it travelled. Score
rather than hop count picks a node's representative path — two certain hops beat
one guessed one.

`GET /impact/explain` is the same radius in prose. Cached per parse, so asking
twice costs one model call.

### `risk`

`GET /risk` scores `:Function` and `:File` nodes 0–100 from blast radius,
coverage, centrality, coupling and churn. Every response carries the normalised
factors, the weights actually applied, the raw metrics and a list of reasons —
because a bare number cannot be argued with, and "why?" is the next question every
time. Weights are renormalised when a repository has no git history, rather than
scoring it as though its churn were zero.

### `cochange`

`GET /cochange` returns files that historically change together, from `git log`.
The only edges in the graph not derived from the code — and coupling that static
analysis cannot see. Read undirected: neither file causes the other.

### `debt`

`GET /debt` runs five detectors and returns ranked findings, filterable by `kind`,
`min_severity` and `min_confidence`. `?format=markdown` returns the whole scan as
a downloadable document instead — same scan, one code path, so the two cannot
disagree.

Thresholds are calibrated against the repository's own distribution, so a finding
means *unusual for this codebase*. The response names the detectors that ran **and
any that failed**, because a detector that crashed reports zero findings and that
reads exactly like a clean result.

### `chat`

`POST /chat` streams Server-Sent Events: one `context` frame with the citations,
then `delta` frames, then `done`. Citations come first so the UI can show what the
answer will be grounded in before any text arrives.

An optional `focus_key` pulls a node's blast radius into the context alongside the
retrieved code. It is set by the graph panel from the node the user selected —
there is no intent detection on the message text.

`GET /conversations` lists threads **without** their messages; fetching one
conversation is the only way to get message bodies.

---

## Reading the schema

```bash
# every path and its query parameters
python -c "import json;s=json.load(open('docs/api/openapi.json'));\
[print(m.upper(), p) for p in sorted(s['paths']) for m in s['paths'][p]]"
```

Or open `/docs` on a running instance and try the calls directly — every endpoint
is documented there with its response model.
