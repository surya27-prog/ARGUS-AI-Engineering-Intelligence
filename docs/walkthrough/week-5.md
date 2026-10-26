# Week 5 — Technical debt, performance and hardening

*Part of the [walkthrough](README.md). Previous: [Week 4 — impact, risk and the
graph](week-4.md).*

By the end of Week 4 the tool could answer questions about a codebase. This week
it starts making **judgements** about one — and immediately has to learn restraint
about how many.

**The week's goal:** from "impressive prototype" to "believable product".

---

## Day 1 — Five detectors

**What we did.** Code that looks for five kinds of problem: overly complex
functions, oversized files, circular imports, dead code, and missing docstrings.

**The measurement behind the first one.** *Cyclomatic complexity* counts the
decision points in a function — each `if`, `for`, `while`, `except` or `and` adds
a branch. One straight-line function is 1; one with ten branches is 11, needs at
least eleven tests to cover, and is where bugs live.

One deliberate detail: nested functions are **excluded** from their parent's
count. A function is not complex merely for containing one — the nested function
is a symbol in its own right and gets its own score.

**The rule that shapes every detector: thresholds are relative to the
repository.** A 600-line file is unremarkable in one codebase and the worst
offender in another. So each detector compares against the repository's own
distribution — the 90th percentile — with an absolute floor so a small tidy
project does not report its least-tidy tenth as debt.

A finding therefore means **"unusual for this codebase"**, not "over a number
someone picked".

**Which store answers which question.** Complexity, file size and docstrings come
from PostgreSQL; circular imports and dead code need the graph. The split is not
tidiness — it is that each question is cheap in exactly one of them.

**And the honest one.** Dead-code findings are capped at **0.75 confidence**,
permanently, because a call at module scope creates no `CALLS` edge — so a
function used only by module-level code looks unreferenced. The cap is
documented, and the tool would rather say "probably unused" than have you delete
a live handler.

---

## Day 2 — The report, and the lesson of the week

**What we did.** Aggregate the findings, rank them, serve them, export as
Markdown.

**Then this happened: the circular-import detector reported 200 findings.** Three
were worth acting on.

The other 197 were not false positives exactly — they were **supersets**. If
`a → b → a` is a cycle, then `a → b → c → a` also technically is, and so is every
longer loop containing it. The detector was faithfully reporting all of them.

The fix was to keep only **minimal** cycles: a cycle is discarded if it contains
a smaller cycle already reported. 200 findings became 9.

**Why this is the most useful thing in the week.** A tool that reports 200
problems when three matter does not merely waste time — it teaches people to close
it. The second time someone opens a report like that, they skim; the third time
they stop opening it. Recall was never the constraint. **Precision was**, and most
of the work on the detectors after this day was calibration rather than detection.

**One more thing the report does.** It names the detectors that ran **and any
that failed**. A detector that crashed reports zero findings, which reads exactly
like a clean result — so a report missing its circular-import pass says so, loudly,
above the findings rather than in a footnote.

---

## Day 3 — The dashboard

**What we did.** A landing page per repository: counts, a risk heatmap, the
riskiest functions, debt summary cards.

**The colour work worth mentioning.** The risk bands were checked against
colour-vision deficiency simulations, and the original palette **failed** — two
adjacent bands were nearly indistinguishable for deuteranopia. They were replaced
with a validated sequential ramp. About 1 in 12 men has some form of colour
blindness; a heatmap they cannot read is a heatmap that lies to them.

Clicking a score opens *why this score* — the factors, the weights, the raw
numbers. The dashboard never shows a number it cannot explain.

---

## Day 4 — Make it fast, and know where the time goes

**What we did.** Per-stage parse timing, a cache for the three expensive reads,
and one N+1 query fixed.

**Per-stage timing.** The job row recorded one duration for a whole parse. That
tells you a parse took four minutes and nothing about *which stage* spent them.
Now each of the five stages is timed separately — and the timer records on the way
out **whether or not the stage failed**, so a failed parse still reports how long
the failing stage had been running. That is the case where the timing matters
most, and the one a profiler run would never capture.

**The cache, and the decision inside it.** `/risk`, `/debt` and `/graph` each read
the whole repository to answer one request, and the dashboard asks for four of
them on load. None of their answers change between parses.

So the cache key is **the parse, not the clock**: `(repository_id, parsed_at)`. A
time-to-live would have been a guess about acceptable staleness, and wrong in both
directions — too long and a re-parsed repository serves the old graph, too short
and the cache never helps. Keying on `parsed_at` makes an entry valid exactly as
long as the parse behind it is current, and invalidates on re-parse with nothing
having to remember to.

*(Measured later: `/risk` is 741 ms cold and 18 ms warm.)*

**The N+1.** `GET /conversations` returned every conversation *with all its
messages*, lazily loaded — so a page of 50 conversations was **51 queries**,
shipping full message bodies to render what is only ever a picker. Now it returns
summaries with a message count from a single aggregate query.

**A non-finding, recorded as one.** The plan said "batch Neo4j writes". They had
been batched since Week 2. Writing that down is worth as much as a fix — it stops
the next person assuming it is still to do.

---

## Day 5 — CI

**What we did.** GitHub Actions running lint and tests on every push, with all
three databases as **service containers** — real Postgres, real Neo4j, real
Qdrant, started fresh for each run.

This is where Week 3's stub providers prove themselves: CI runs the whole suite
with **no API key and no cost**, because `LLM_PROVIDER=stub` and
`EMBEDDING_PROVIDER=hash` make every model-dependent path exercisable for free.

One small thing that cost real time: neither the Neo4j nor the Qdrant image ships
`curl`, so a container health check cannot poll itself. The wait loop runs on the
runner instead.

---

## Day 6 — Try to break it

**What we did.** Error handling, input validation and rate limiting. The day's
acceptance test was *"you can't break the app in 15 minutes of trying"*.

**A 503 is not a 500.** Neo4j unreachable, Postgres refusing connections and a
missing API key were all surfacing as `500 Internal Server Error` —
indistinguishable from a bug, and useless to anyone deciding whether to retry.
They are now **503s with a sentence naming the store**, because "the graph
database is not reachable" is actionable and "internal server error" is not.

**Every 500 carries an error id**, which is the same string as the `X-Request-ID`
header and the server log line — so "it broke, id a3f19c22" is enough to find the
traceback. The traceback itself never crosses the wire: it names file paths,
library versions and sometimes query text.

**The URL is the most validated input in the system**, because it becomes an
argument to `git clone`. Embedded credentials, private and loopback addresses —
including the cloud metadata endpoint `169.254.169.254` — control characters and
oversized values are all refused, each with a reason.

**Rate limiting is a token bucket**, not a fixed window. A fixed window lets a
user spend a whole minute's allowance in one second and then sit blocked; a
bucket refills steadily and allows a **burst**, because three quick questions is
normal use. Every rejection carries `Retry-After`, since without it a client can
only guess, and guessing means retrying immediately and being refused again.

**A validation detail worth stealing.** The default validation error echoes the
submitted value back. For a URL containing credentials, that is exactly the kind
of echo that ends up in a log aggregator — so errors name the *field* and not the
value.

---

## Day 7 — Stop

**What we did.** Feature freeze. Refreshed the architecture document and wrote
[`release-checklist.md`](../release-checklist.md).

**Why the checklist exists.** A long stretch of this week was built while the
local Docker stack was down, so a great deal had been *written and reviewed*
without being *observed working*. Those are different things, and the gap between
them is the difference between a release candidate and a hope. Each box in that
file is something nobody had watched happen yet.

It proved its worth three weeks later, when the suite finally ran in full and
found two real defects — one of them in the product, not the tests.

---

## The week, in one picture

```
Five detectors → findings, calibrated against the repo's own distribution
      ↓
Minimal-cycle filtering: 200 findings → 9
      ↓
Ranked report + Markdown export, naming any detector that failed
      ↓
Dashboard: heatmap, riskiest functions, debt cards
      ↓
Per-stage timing · parse-scoped cache · N+1 fixed
      ↓
CI on every push · 503s · rate limits · validated input
      ↓
Feature freeze, and a written list of what was never verified
```

---

## What Week 5 did not do

`v0.5-rc` was not tagged, deliberately: the suite had not run in full, so the
label would have been a claim nobody could support. Nothing was deployed, and
`performance.md` was left with **empty tables** rather than invented numbers —
both filled in only when the stack came back weeks later.

**Next: [Week 6 — deployment, documentation and shipping](week-6.md)**, the week
of turning a working repository into something a stranger can use.
