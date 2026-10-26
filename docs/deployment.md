# Deploying ARGUS

Five pieces: three data stores, an API and a web app. The stores are managed
services because running Neo4j and Qdrant yourself for a demo is a worse use of
a week than a free tier.

```
Vercel (web) ──HTTPS──> Render or Fly (API) ──> Neo4j Aura
                                          ├──> Qdrant Cloud
                                          └──> managed Postgres
```

Nothing here can be done from this repository alone — every step needs an account
and, in two cases, a card on file for the free tier. Work top to bottom; each
step produces a value the next one needs.

---

## 1. The three data stores

Create these first: the API cannot start without their URLs.

### Postgres

Render's Blueprint declares one (`render.yaml`), so on Render this is automatic
and `DATABASE_URL` is wired from it. On Fly, or anywhere else, use Neon or Supabase
and copy the connection string.

One thing to check on the string you are given: most managed Postgres requires
TLS, so append `?sslmode=require` if the provider has not already.

You do **not** need to fix up the scheme. Every managed provider hands out
`postgres://` or `postgresql://`, and SQLAlchemy 2 maps both to psycopg2, which is
not installed here — the project uses psycopg 3. `Settings.name_the_driver`
rewrites the scheme on the way in, so a connection string pasted verbatim from any
provider works.

### Neo4j Aura

Create a free AuraDB instance. It gives you a URI like
`neo4j+s://abc123.databases.neo4j.io`, a username (`neo4j`) and a password shown
**once** — save it then.

`neo4j+s://` carries TLS in the scheme. `core/graph.py` passes no `encrypted=`
argument precisely so this works unchanged; adding one would conflict with the
scheme and fail at connect time.

The free tier pauses after a few days of inactivity and takes a minute to wake.
That is the most likely reason a working demo is suddenly returning 503s from the
graph endpoints — the API reports it as "the graph database is not reachable"
rather than as a bug, which is what `core/errors.py` is for.

### Qdrant Cloud

Create a free cluster. You get a URL and an API key; unlike the local container,
the key is required. Both go in as `QDRANT_URL` and `QDRANT_API_KEY`.

The collection is created on first write, sized from `EMBEDDING_DIMENSIONS`.
**That value must match your embedding model** — 1536 for
`text-embedding-3-small`. A mismatch is not an error, it is silent nonsense:
vectors of the wrong width still store and still return neighbours, just
meaningless ones.

---

## 2. The API

### On Render

Dashboard → New → Blueprint → select this repository. It reads `render.yaml`,
creates the Postgres instance and the web service, and asks for the seven
secrets marked `sync: false`.

Leave `CORS_ORIGINS` until after step 3 — its value is the frontend's URL, which
does not exist yet.

### On Fly

```bash
fly launch --no-deploy
fly volumes create argus_workspace --size 10 --region lhr
fly secrets set DATABASE_URL='postgresql+psycopg://…' \
                NEO4J_URI='neo4j+s://…' NEO4J_PASSWORD='…' \
                QDRANT_URL='https://…' QDRANT_API_KEY='…' \
                ANTHROPIC_API_KEY='…' OPENAI_API_KEY='…'
fly deploy
```

Run `fly deploy` from the repository root, not from `backend/` — the image needs
`parser/` and `database/` in its context.

### Either way

The image binds `$PORT` when the platform sets one, falling back to 8000. A
hardcoded port is the single most common reason a container that runs locally
fails its first health check on Render or Railway.

Migrations run as a release/pre-deploy command, before the new instance takes
traffic. `alembic upgrade head` is idempotent, so a deploy carrying no new
migration is a no-op.

Confirm it before moving on:

```bash
curl https://your-api.onrender.com/health
```

Every store should read `up`. `llm_configured` and `embedding_configured` tell you
whether the keys took.

---

## 3. The web app

Vercel → New Project → this repository → set the root directory to `frontend`.

One environment variable, and it is the one people get wrong:

```
NEXT_PUBLIC_API_URL = https://your-api.onrender.com
```

**It is baked into the client bundle at build time**, not read at runtime. The
compiler inlines every `NEXT_PUBLIC_*` value, so this must be the URL the
*browser* resolves. An internal service name works in the compose network and in
no browser anywhere. Changing it requires a rebuild, not a restart.

---

## 4. Close the CORS loop

Now that the frontend has a URL, set it on the API and let it redeploy:

```
CORS_ORIGINS = https://argus.vercel.app
```

This is the **frontend's** origin, not the API's — the API compares it against the
`Origin` header the browser sends. Getting these two backwards produces a site
that loads, renders, and fails every request with a CORS error in the console and
nothing visible on the page.

---

## 5. Check it end to end

1. Open the Vercel URL. The repository list should load — if it shows "Cannot
   reach the ARGUS API", either `NEXT_PUBLIC_API_URL` is wrong or CORS is.
2. Paste `https://github.com/psf/requests`. It should go
   `pending → parsing → complete` in a couple of minutes.
3. Dashboard: stats, risk heatmap, debt counts.
4. Graph: click a function, watch its dependents light up.
5. Ask a question in chat and check a citation links to real code.

---

## What will go wrong

Collected because each of these has a symptom that points somewhere else.

| Symptom | Cause |
|---|---|
| Deploy rolls back, health check never passes | The process is not on `$PORT`. Fixed in the image, but a custom start command can undo it |
| Site loads, every request fails, console shows CORS | `CORS_ORIGINS` is the API's own URL instead of the frontend's |
| Site loads, requests go to `localhost:8000` | `NEXT_PUBLIC_API_URL` was set after the build. It is compiled in — rebuild |
| Graph endpoints 503 after idle | Aura free tier paused. First request wakes it, ~1 minute |
| Search returns nothing, no error | `EMBEDDING_DIMENSIONS` does not match the model. Wrong-width vectors store and retrieve fine, they are just meaningless. Recreate the collection |
| Chat 503s, everything else works | No `ANTHROPIC_API_KEY`. Deliberate: the API degrades rather than failing, and `/health` says which provider is unconfigured |
| Parse fails on a big repository | `MAX_REPO_SIZE_MB` is 200 on the deployed config, against 500 locally — a starter instance's disk is small |
| Every restart re-clones everything | No persistent disk mounted at `WORKSPACE_DIR` |
| A private URL is refused | Deliberate. `ALLOW_PRIVATE_GIT_HOSTS` is false: a server that fetches arbitrary internal addresses on request can be used to read them |

---

## Costs

Free tiers cover a demo. What they cost you instead:

- **Render free Postgres expires after 90 days** and is not backed up
- **Aura free pauses on inactivity** and wakes slowly
- **Render's free web service sleeps**, so the first request after idle is slow —
  the API plan in `render.yaml` is `starter` for that reason
- **The model providers are the only real spend.** Embedding a repository is a
  one-off per parse; chat is per question. The rate limits added in Week 5 Day 6
  exist so a loop in a client cannot run up a bill
