# Week 4 — Impact, risk and the graph

*Part of the [walkthrough](README.md). Previous: [Week 3 — retrieval and
chat](week-3.md).*

Three weeks of groundwork: a parser, a graph with confidence on every inferred
edge, and retrieval that can find code by meaning. This is the week they become
the product.

**The week's goal:** the headline feature — *if I change this, what breaks?*

---

## Day 1 — Blast radius

**What we did.** Given any symbol, return everything that reaches it, ranked.

**The traversal is backwards.** Follow `CALLS` and `IMPORTS` edges *in reverse*
from the target: who calls this, who calls them, and so on, depth-limited. That
set is the blast radius — everything a change here could touch.

**The ranking is the interesting part.** Returning 60 affected symbols in no
order is barely more useful than grep. Two things decide the order:

**Confidence, from Week 2.** Every `CALLS` edge already carries how it was
resolved — 1.0 for a local name, 0.85 for a method on `self`. A path is only as
trustworthy as the weakest guess in it, so the confidences **multiply** along it.

**Decay per hop.** Something two hops away is genuinely less affected than a
direct caller, so each hop multiplies by a decay factor, 0.6 by default:

```
score = confidence × decay^(hops − 1)
```

Together these produce the property worth stating out loud: **two certain hops
outrank one guessed hop.** A path through two `local` calls scores
`1.0 × 1.0 × 0.6 = 0.6`; a single `attribute_self` call scores `0.85`. A chain of
three uncertain hops falls away fast, which is correct — that is exactly the
inference you should trust least.

Every row also returns the **route** it travelled. Not decoration: it is how a
user checks the tool's work instead of believing it.

---

## Day 2 — What the code does not say

**What we did.** Read the repository's `git log` and recorded which files
repeatedly change *together*.

**Why this exists.** Static analysis sees `import`s and calls. It cannot see that
`serializers.py` and `schema.py` have been edited in the same commit fourteen
times, because there is no edge in the code — yet anyone who has worked on that
codebase knows to open both.

Co-change edges are the only edges in the graph **not derived from the source**.
They come from history, and they catch coupling that exists in the team's
behaviour rather than in the syntax.

**Read undirected.** If A and B change together, neither causes the other. The
data supports "these move as a unit" and nothing stronger, and the documentation
says so rather than implying causation.

Two practical limits: history is capped at a depth, because `git log` over a
repository with 50,000 commits is not something to do inside a request. And a zip
upload has no history at all, which matters on Day 3.

---

## Day 3 — The risk score

**What we did.** Combined five signals into one number from 0 to 100, and wrote
[`risk-model.md`](../architecture/risk-model.md) to justify it.

```
score = 100 × Σ (weight × factor)
```

| Factor | Weight | What it measures |
|---|---:|---|
| `blast` | 0.35 | how many symbols reach this one |
| `coverage` | 0.20 | test references — **inverted** |
| `centrality` | 0.20 | direct dependents plus dependencies |
| `coupling` | 0.15 | how many files change alongside it |
| `churn` | 0.10 | how often it changes |

**Why blast radius carries the most weight.** It is the only input that measures
*consequences*. Everything else is a property of the code as it sits; blast radius
is a property of **changing** it, which is the question actually being asked.

**Why coverage is inverted.** Untested code is not neutral — it is precisely the
case where a mistake ships. So the factor is `1 − saturate(tests)`: 1.0 for
something nothing tests, falling as test callers appear. It is why a leaf function
with no dependents still scores around 20 rather than 0.

**Why churn is separate from coupling**, when the plan folded them together: they
answer different questions. How often does this file move, versus what moves *with*
it. A stable file welded to three others and a file rewritten weekly in isolation
are different risks, and one blended number cannot tell you which you have.

**Saturating curves, not raw counts.** Each factor uses `x / (x + k)`. Going from
2 dependents to 10 is a large change in risk; from 200 to 208 is not. A linear
scale would let one enormous file flatten every other score in the repository.

**Every response shows its working** — the normalised factors, the weights
applied, the raw metrics, and a list of reasons in English. A bare number out of
100 cannot be argued with, and "why?" is the next question every single time.

**The honest caveat, stated in the doc.** ARGUS does not run your test suite and
has no real coverage data. It proxies coverage by *reachability from a file that
looks like a test*. That is a proxy, it is named as one, and the model says what
it would take to replace it with the real thing.

**A detail that matters more than it sounds.** A zip upload has no git history,
so `coupling` and `churn` have no input. Scoring them as zero would quietly make
every uploaded repository look safer than a cloned one. Instead the remaining
weights are **renormalised**, and the response says the history factors were
unavailable.

---

## Day 4 — Draw it

**What we did.** The graph rendered in the browser with **Cytoscape.js**, a
JavaScript library for network diagrams: nodes, edges, automatic layout, zoom and
pan, click-to-select.

**The constraint that shapes this day.** A few thousand nodes will lock a browser
tab solid. So the node count is capped, and the layout runs for a bounded number
of iterations rather than to convergence — `cose` on several hundred nodes will
happily spin for tens of seconds otherwise, which is indistinguishable from a
frozen page.

---

## Day 5 — Make it interactive

**What we did.** Click a node and its dependents light up. Colour by risk. Filter
by node type. Collapse by module.

**The performance decision worth copying.** Colour is read through a reference
inside the style function, so switching from *colour by type* to *colour by risk*
**repaints without rebuilding the elements**. A rebuild would re-run the layout
and throw away the user's pan, zoom and mental map of where things are — the
visual equivalent of losing your place.

**Collapse by module** is the answer to a call graph too dense to read. Fold every
symbol into its module and **remap the edges onto the modules** rather than
hiding them — so the shape of the dependencies survives at a coarser grain,
instead of the picture simply having less in it.

---

## Day 6 — Say it in English

**What we did.** Fed the impact set to the model: *changing this breaks X because
Y*, in the graph panel and in chat.

**Note what is and is not the model's job.** The blast radius is computed by
Cypher and arithmetic — the model never decides what is affected. It is handed a
finished, ranked answer and asked to phrase it. Everything it says can be checked
against the table beside it.

The response reports how many nodes it actually read, and the panel shows that:
*"read from the 15 highest-scoring of 62 affected nodes"*. A summary that silently
covers a quarter of the data reads exactly like one that covers all of it.

It is cached per parse, so asking twice costs one model call — and this is also
where the graph hands a node over to chat, carrying the selected key so the
conversation starts focused on it rather than guessing intent from the wording.

---

## Day 7 — Tag it

`v0.4-impact` — the one tag in this project that was actually created. The plan
called this the midpoint and the demo that sells the project, and it is the point
where the pieces first added up to something you could show someone.

---

## The week, in one picture

```
Pick a symbol
     ↓
Reverse-traverse CALLS / IMPORTS, depth-limited
     ↓
Score each path: confidence × decay^(hops−1)
     ↓
Blast radius, ranked, each row carrying its route
     ↓                                    ↓
Risk score (5 weighted factors)      Model phrases it in English
     ↓                                    ↓
Cytoscape: click a node, watch its dependents light up
```

---

## What Week 4 did not do

Nothing was looking for problems yet — no debt detection, no dashboard, no
caching, no CI. The scores existed but nothing aggregated them into "here is what
is wrong with this repository", which is Week 5.

**Next: [Week 5 — technical debt, performance and hardening](week-5.md)**, where
the tool starts making judgements about code, and immediately has to learn
restraint about how many.
