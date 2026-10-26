# ARGUS — Risk Model

**Status:** Week 4, Day 3. This document defines the 0–100 risk score: what goes
into it, why each input is weighted the way it is, and what the number does
*not* mean. The graph it reads is described in
[graph-schema.md](graph-schema.md); the blast-radius traversal it builds on is
in [`app/services/impact.py`](../../backend/app/services/impact.py).

The implementation is [`app/services/risk.py`](../../backend/app/services/risk.py).
Every constant named here is a named constant there — if the two disagree, the
code is right and this file is stale.

---

## What the score is for

A reviewer looking at a diff asks one question: *how carefully do I need to read
this?* Answering it well means holding several unrelated facts in mind at once —
how much depends on this symbol, how connected it is, whether a test would catch
a mistake, how often this file moves and what it drags with it.

ARGUS already computes all of those separately. The risk score does that
arithmetic explicitly so a repository can be **ranked**, and so a change can be
argued about with a number instead of a hunch.

It is a triage aid, not a verdict. A 90 does not mean the code is bad; it means
a mistake here is expensive and nothing would catch it.

---

## The formula

```
score = 100 × Σ (weightᶠ × factorᶠ)      for f in {blast, coverage, centrality, coupling, churn}
```

Every factor is normalised into [0, 1] before weighting, so the score is bounded
in [0, 100] by construction rather than by clamping.

| Factor | Weight | Raw input | Knee (k) |
|---|---|---|---|
| `blast` | 0.35 | distinct nodes reaching this one within 2 hops of `CALLS`/`IMPORTS` | 8 |
| `coverage` | 0.20 | distinct callers/importers in test files — **inverted** | 2 |
| `centrality` | 0.20 | direct dependents + direct dependencies (degree) | 6 |
| `coupling` | 0.15 | co-change partners of the file, blended with the strongest pair | 3 |
| `churn` | 0.10 | commits touching the file in the history window | 10 |

Bands, so a UI does not invent its own thresholds:

| Score | Band |
|---|---|
| 0–24 | `low` |
| 25–49 | `moderate` |
| 50–74 | `high` |
| 75–100 | `critical` |

### Worked example

`get_settings` in this repository, scored against its own history:

| Input | Raw | Factor |
|---|---|---|
| dependents (2 hops) | 62 | 62 / (62 + 8) = 0.886 |
| test references | 1 | 1 − 1/(1 + 2) = 0.667 |
| degree | 14 in + 1 out = 15 | 15 / (15 + 6) = 0.714 |
| co-change partners / strongest | 8 / 0.667 | 0.6 × 8/(8+3) + 0.4 × 0.667 = 0.703 |
| commits in window | 6 | 6 / (6 + 10) = 0.375 |

```
100 × (0.35×0.886 + 0.20×0.667 + 0.20×0.714 + 0.15×0.703 + 0.10×0.375) = 72.9   → high
```

A config accessor that 62 things depend on, that changes with eight other files,
and that one test touches. That is the right answer, and the arithmetic to check
it ships in the response.

---

## Why these five

The timeline named four inputs — blast radius, co-change, centrality, test
coverage. **Churn is split out from co-change** rather than folded into it,
because they answer different questions: how often this file moves, versus what
moves with it. A stable file welded to three others and a file rewritten weekly
in isolation are different risks, and one blended number cannot say which is
which.

**Blast radius carries the most weight** because it is the only input that
measures consequences. Everything else is a property of the code; blast radius
is a property of *changing* it, which is the question being asked.

**Coverage is inverted.** Untested code is not neutral — it is the case where a
mistake ships. So the factor is `1 − saturate(tests)`: 1.0 for a symbol nothing
tests, falling as test callers appear. This is the reason a leaf function with
no dependents still scores ~20 rather than 0.

**Centrality is degree, not betweenness.** Betweenness or PageRank would be
better signals — they distinguish a bottleneck from a merely popular utility —
but both need Neo4j GDS, which is not in the community image this project runs
on. Degree is the honest approximation available, and it is named as such.

---

## Normalisation: saturating, not min–max

Every raw count becomes a factor through:

```
saturate(x, k) = x / (x + k)
```

zero at zero, exactly 0.5 at `x = k`, approaching but never reaching 1.

The alternative — scale each metric against the repository's own maximum — was
rejected for a specific reason: **it makes every score depend on the single most
extreme node.** Add one god-module with 400 dependents and every other symbol's
blast factor silently collapses toward zero, so the same function scores
differently in two repositories, and differently in the same repository after an
unrelated commit. Scores would not be comparable across repos, across time, or
across a refactor.

The saturating curve has no such coupling. It is also self-documenting: `k` is
the value at which a factor is half its maximum, so every knee in the table
above is a stated judgement rather than an emergent property of the data.

The cost is real and worth naming: **the curve compresses the top end.** 40
dependents and 400 dependents both land near 1.0 for the blast factor. That is
deliberate — past some point "a lot of things depend on this" stops carrying
extra information for a reviewer — but it does mean the score cannot be used to
rank two already-critical symbols against each other. Use the raw `metrics` for
that; they are in every response.

---

## Missing history is not zero risk

Coupling and churn only exist once the commit log has been read. A zip upload,
an exported directory, or a repository whose history pass failed has neither.

Scoring those factors as 0 would report *lower* risk for code we know *less*
about — the worst failure a risk score can have. So instead:

> When `change_count` is null, the coupling and churn weights (0.25 combined)
> are removed and the remaining three weights are rescaled to sum to 1.

The same structural evidence therefore produces a *higher* score without history
than with a quiet history, which is the correct direction: "we did not measure
it" is not "there is nothing there". The response says which weights were
applied, and the reasons list carries an explicit *"no commit history available
— scored on structure alone"*.

`change_count` is stored as null rather than 0 by the co-change writer precisely
so this distinction survives into the score.

---

## The test-coverage proxy

ARGUS does not run the test suite; it has no coverage data. What it has is the
graph, so coverage is proxied by **reachability from a test file**:

> a symbol's `test_references` is the number of distinct callers or importers
> that live in a file matching `tests?/…`, `test_*.py`, `*_test.py` or
> `conftest.py`.

This is a proxy and it is wrong in both directions. A test that imports a module
without exercising a branch still counts. A symbol covered only through three
layers of indirection does not, because the count is direct callers. Integration
tests that drive the system through an entry point make everything behind that
entry point look untested.

It is still worth having: at 0.20 weight it separates "nothing in the test suite
even mentions this" from "the tests reach it", which is the distinction that
actually changes how carefully a reviewer reads a diff. When real coverage data
exists (Week 5's debt work), this factor should be replaced rather than tuned.

---

## What this score is not

- **Not a code-quality measure.** It says nothing about whether the code is
  well written. A perfectly clean function that half the repository depends on
  scores high, correctly.
- **Not a defect predictor.** It is not fitted to any bug data, and no claim is
  made that high-risk symbols contain more bugs. The weights are stated
  judgements, not learned coefficients.
- **Not stable against parser gaps.** Dynamic dispatch, decorators and
  reflection all hide edges. A symbol whose callers all reach it dynamically
  scores as a leaf. `unresolved_calls` on `:Function` nodes is the honest signal
  that this is happening.
- **Not free.** Ranking a repository traverses two hops backwards from every
  node. On the fixture repos this is fast; caching is Week 5's performance pass.

---

## Where the number surfaces

- `GET /repos/{id}/risk` — the repository ranked, or one node with `?key=`.
  Every response carries `factors`, the `weights` actually applied and the raw
  `metrics`, so the score can be recomputed by hand from what it returns.
- `reasons` — mechanical, read straight off the metrics. Saturday's LLM impact
  summary takes them as *input*: the model explains a score, it does not invent
  one.
- The graph view (Day 5) colours nodes by band, and the dashboard (Week 5)
  ranks by score.
