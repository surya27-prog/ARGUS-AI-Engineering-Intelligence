# Week 6 — Deployment, documentation and shipping

*Part of the [walkthrough](README.md). Previous: [Week 5 — technical debt,
performance and hardening](week-5.md).*

Five weeks produced a working system on one machine. This week is about the
distance between that and something a stranger can use — which is longer than it
looks, and made almost entirely of unglamorous work.

**The week's goal:** a link you can put on a CV.

---

## Day 1 — Production images

**What we did.** Multi-stage Dockerfiles and a production Compose file.

**Multi-stage build.** The first stage installs build tools and compiles
dependencies; the second copies only the result into a clean image. The compiler,
the headers and the package caches never ship. A smaller image is faster to
deploy and has less in it that can be exploited.

**Runs as a non-root user** (uid 10001). If something does go wrong inside the
container, it should not be root that it goes wrong as.

**The production Compose file differs from development in ways that matter.**
Data stores publish **no ports** — in development you connect to Postgres from
your laptop; in production only the API should reach it. Every secret is a
`${VAR:?message}` reference, so the stack **refuses to start** with a missing
password rather than falling back to a development default. Migrations run as a
one-shot service the API waits on, so an API instance cannot serve requests
against a schema that has not been upgraded yet.

---

## Day 2 — Deployment configuration, and two bugs it exposed

**What we did.** Config for Render, Fly and Vercel.

**Two bugs that only show up here, and would have failed silently:**

**The hardcoded port.** The container bound port 8000. Render, Railway and Fly
all assign a port at runtime through `$PORT` and health-check *that* — so the
container would have started, looked healthy locally, and been killed by the
platform for never answering. Fixed by binding `${PORT:-8000}`, via `sh -c` with
`exec` so that shutdown signals still reach the server rather than the shell.

**The database driver.** Managed Postgres providers hand out URLs beginning
`postgresql://`, which SQLAlchemy maps to `psycopg2` — a package this project does
not install. It uses `psycopg` 3. So the first connection in production would have
failed with an import error. Settings now normalise the URL to name the driver
explicitly.

Neither is exotic. Both are the kind of thing found at 11pm during a deploy, and
both were found by reading the configuration against the platforms' documentation
instead of assuming.

---

## Day 3 — Logging that works, and a repeatable smoke test

**What we did.** Structured logging with request correlation, and a script that
exercises a running instance.

**A dead setting, found by looking.** `LOG_LEVEL` appeared in five configuration
files and was **wired to nothing**. Every `logger.info` in the codebase was being
dropped. Now it configures logging properly: JSON output, a request id attached to
every line.

**One id per request**, returned as `X-Request-ID`, held in a `ContextVar` rather
than thread-local storage — because async request handlers hop between threads and
thread-local would attach one request's id to another's log lines. The same id is
the `error_id` a 500 hands back, so a user's complaint maps to a traceback in one
search.

**The smoke test is a script, not a checklist.** *"A stranger could use the live
URL without you present"* is a claim that needs re-checking after every deploy, and
a checklist someone works through from memory is a checklist that drifts. It
exercises health, the error paths, and the read endpoints; it collects failures
rather than stopping at the first, because knowing whether one thing is broken or
everything is the difference between a bad deploy and a bad service. Its exit code
is the failure count, so a deploy hook can gate on it.

---

## Day 4 — The README

**What we did.** Rewrote it from 113 lines to around 330.

**Why a whole day on one file.** It is the most-read file in any repository by a
wide margin, and for most visitors it is the *only* one. It leads with the problem
rather than the architecture, because nobody cares how something is built until
they believe it solves something.

Screenshots were left as **explained placeholders** rather than mockups. A mockup
in a README is a claim about software that does not exist.

---

## Day 5 — The API reference

**What we did.** Generated `openapi.json` from the running app, and wrote the
human half by hand.

**Generated, not written**, because a hand-maintained reference drifts the first
time a query parameter changes and nobody notices for a month. The script dumps
the schema with sorted keys, so regenerating produces a stable diff and a change
to the API surface shows up in review.

The handwritten half covers what a schema cannot express: the pagination envelope
and why `total` is the count *before* the page; the single error shape and its
status table; which three endpoints cost money and their exact rate limits; and
confidence as part of the answer.

**Every number in it was checked against the code**, not recalled — the rate
limits read off the limiter objects, the confidences off `call_resolver.py`.

---

## Day 6 — The demo, scripted

**What we did.** A ten-shot script with timings, the click path and the narration.

**The recording could not be made** — it needs a running stack and real provider
keys, and the stack was down for most of this stretch. So the honest deliverable
was the script, which is most of the work: what to show, in what order, and what
to say.

Checking it against the actual interface corrected two shots. The graph canvas has
**no search box**, so "search for the node" would have been dead air on camera. And
changing the view re-runs the layout, moving the node you had lined up — so the
shot order matters.

---

## Day 7 — Ship

**What we did.** `LICENSE` (MIT — the README had claimed MIT for three days with
no file behind it, which is the one licence state worse than having none), and a
launch kit: the repository description, topics, and the write-up post, written
where the claims can be checked rather than typed straight into a form.

**The numbers were split into measured and not.** Coverage, the 1,000-file target
and "all tests passing" had never been observed, so the document said not to quote
them. *"475 tests"* is a count of what exists; *"475 passing tests"* is a claim
about a run that had not happened.

---

## After the plan: the week the claims got tested

The six weeks ended here. What followed is the part worth reading.

**The branch was pushed, and CI ran for the first time — and failed.** The whole
suite had not run since Week 5 Day 1. Running it against live stores found two
real defects:

- a test that iterated a pagination envelope instead of the page inside it — a
  test bug, never executed until now
- **a filtered debt report that contradicted itself**, opening with "1 findings"
  and then breaking that one finding down into five. `total` was derived from the
  findings list while the per-kind tallies were stored fields, so filtering
  updated one and not the other. That is a defect in the product, in a feature
  whose entire selling point is not misleading you

**Then the measurements.** Coverage was **90%**, not the 44% the documents had
carried — that figure came from a tenth of the suite. A parse of 37 files takes
**6 seconds**. `/risk` is **741 ms cold and 18 ms warm**, which finally turned the
cache from an argument in a design document into a measurement.

**And a stale Qdrant volume**, which the checklist had predicted might "force a
re-embed". It did worse: v1.19.0 **panics** reading v1.12.5's segment format and
crash-loops, surfacing as sixteen connection errors that looked nothing like a
storage problem.

---

## What is still not done

Stated plainly, because the whole project argues for saying so:

- **Nothing is deployed.** No public URL. The configuration is written and
  reviewed; it has never run on a platform
- **No demo video, no screenshots.** Both scripted, both needing real provider
  keys
- **The 1,000-file target is unmeasured.** `psf/requests` is 37 files
- **`main` is stale.** All the work is on a branch, with a pull request open

Every one of these is in [`release-checklist.md`](../release-checklist.md), sorted
by what blocks it — rather than left for a reader to discover.

---

## The six weeks, in one line each

| Week | What it added |
|---|---|
| 1 | Repository in, structured facts out — the parser, and its frozen output schema |
| 2 | Those facts become a graph, with a confidence on every inferred edge |
| 3 | Code becomes searchable by meaning, and answers get citations |
| 4 | Blast radius, risk scoring, and a graph you can click |
| 5 | The tool starts judging code — and learns restraint about how much |
| 6 | Everything needed for someone else to run it |

The line running through all of it: **say how sure you are.** Confidence on every
call edge, ambiguous calls counted rather than guessed, dead code capped at 0.75,
risk scores that show their arithmetic, a retrieval eval recorded at 8/10 when the
new feature did not improve it, and a release checklist listing what was never
verified. The tool argues that software should admit what it does not know, and
the project tried to hold itself to the same standard.
