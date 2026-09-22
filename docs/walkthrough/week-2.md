# Week 2 — The knowledge graph

*Part of the [walkthrough](README.md). Previous: [Week 1 — foundations and the
parser](week-1.md).*

Week 1 ended with facts in PostgreSQL: this file exists, it contains these
functions, this function calls something named `send`. Useful, and not yet an
answer to anything, because *"what breaks if I change this?"* is a question about
**connections**, and a table of rows has none.

**The week's goal:** those parsed facts become a real graph you can query.

This is the week the project's central idea appears — that an inferred connection
should carry a confidence rather than pretend to certainty.

---

## Day 1 — Design the schema on paper

**What we did.** Decided what the graph is made of, wrote it into
[`architecture/graph-schema.md`](../architecture/graph-schema.md), and wrote no
code at all.

**The vocabulary.** A graph database stores two things:

- **Nodes** — the things. Here: `Repo`, `File`, `Class`, `Function`, `Module`.
- **Edges** (relationships) — how they connect. Here: `CONTAINS`, `IMPORTS`,
  `CALLS`, `INHERITS`.

Read as a sentence: a *Repo* CONTAINS a *File*, which CONTAINS a *Class*, which
CONTAINS a *Function*, which CALLS another *Function*.

**One decision worth seeing.** Functions and methods share a single `:Function`
label, separated by a `kind` property, rather than being two labels. A call edge
does not care whether its target is a free function or a method — and every query
that *does* care can still filter on `kind`. Two labels would have meant every
traversal in the project spelling out both, forever.

**Why a whole day on paper.** Every pass built afterwards reads this shape. The
cost of changing it later is not the schema file — it is the writer, all three
resolution passes, every Cypher query, and the API on top. Deciding it once, in
prose, was the cheapest day of the week.

---

## Day 2 — Write the graph, and make re-parsing safe

**What we did.** The Neo4j driver and the writer that turns parser output into
nodes and edges.

**The technology, and why:**

**Cypher** is Neo4j's query language. Where SQL joins tables, Cypher draws the
pattern you are looking for, and the arrows are the relationships:

```cypher
MATCH (caller:Function)-[:CALLS]->(target:Function {name: 'send'})
RETURN caller
```

That readability is the point. The same question in SQL means a join per hop, and
"three hops away" is not something you can write at all without recursion.

**`MERGE`, and the single `key`.** `MERGE` means *create it if absent, match it if
present*. Every node carries one deterministic string key —
`file:{repo_id}:{path}`, `sym:{repo_id}:{module}:{qualname}` — and every write is
a `MERGE` on it. Parse the same repository twice and the node count is unchanged,
which was the day's acceptance test.

The obvious alternative — a composite key over several properties — was rejected
for a blunt reason: Neo4j's `NODE KEY` constraint is an **Enterprise** feature and
this project runs Community. One indexed string is also the cheapest lookup the
query planner can do.

**A bug caught the same day, by thinking rather than by a test.** The key format
uses the file's module path. A file outside any package has no module, so two
unpackaged scripts each defining `helper` would produce *the same key* — and
`MERGE` would have silently collapsed two unrelated functions into one node,
inventing call edges between strangers. The fix: when there is no module, the key
falls back to the file path. One module builds every key, so the rule cannot drift
between the writer and the passes that look nodes up.

**Stamp and sweep.** `MERGE` alone never deletes, so a file removed from the
repository would live in the graph forever. Every write therefore stamps a
`run_id` — a fresh UUID per parse — and the run ends by deleting anything in that
repository carrying an older one:

```cypher
MATCH (n {repo_id: $repo_id})
WHERE n.run_id <> $run
DETACH DELETE n
```

Non-duplicating *and* correctly removing. A pure-`MERGE` design gets the first
and quietly fails the second.

**`UNWIND` batching.** Writes go a thousand rows at a time in a single query
rather than one query per node. A round trip to the database costs far more than
the write itself, so this is the difference between seconds and minutes.

---

## Day 3 — Resolve imports

**What we did.** Turned `from .models import Response` — which the parser records
as raw text — into an actual edge between two file nodes.

**Why it is a separate pass.** The parser deliberately resolves nothing. It reads
one file at a time, and `import utils` cannot be resolved without knowing every
module in the repository. That table only exists once every file has been walked.
So the parser records what is written, and resolution happens here, with the whole
picture available.

**What made it fiddly.** Relative imports (`from . import x`, `from ..core import
y`) must be resolved against the importing file's own package position. And an
import may point *outside* the repository — `import requests` — which is not an
error and must not be dropped. Those become `:Module` nodes marked external, so
the graph can still answer "what do we depend on that we don't own".

---

## Day 4 — The call graph, and the idea the project rests on

**What we did.** Resolved `foo()` to the `:Function` node that defines `foo`,
wherever that was possible.

**Here is the problem.** Python resolves much of this at runtime, not in the
source. Given `self.client.get()`, static analysis cannot know what `self.client`
is — it might be set from configuration, injected in a test, or reassigned. A
call graph for a dynamic language is **necessarily incomplete**, and any tool
claiming otherwise is guessing.

Two bad options and one good one:

- Guess a target — inflates every blast radius downstream with paths that do not
  exist, and the user has no way to know.
- Drop anything uncertain — the graph looks clean and is quietly wrong.
- **Record how each edge was resolved, and how much to trust it.**

Every `CALLS` edge carries a `resolution` and a `confidence`:

| Resolution | Meaning | Confidence |
|---|---|---:|
| `local` | Defined in the same file | **1.00** |
| `imported` | Follows an import that was itself resolved | **0.95** |
| `attribute_module` | `module.function()` where the module is known | **0.90** |
| `attribute_self` | `self.method()` — the class is known, but subclasses may override | **0.85** |

And where a name is genuinely ambiguous, **no edge is written at all**. The call
site is counted instead, on the calling function's `unresolved_calls` property,
with a reason: `ambiguous`, `unresolved`, or `module_level`.

**Why counting beats guessing.** A gap you can see is recoverable — you can look.
An invented edge propagates: it inflates the blast radius, which inflates the risk
score, which puts the wrong file at the top of a dashboard, and nothing about the
output tells you it happened.

Those four numbers are what Week 4's impact analysis multiplies along each path,
so that two certain hops outrank one guessed one. The whole product leans on a
decision made this day.

**`INHERITS`** landed here too, with the same honesty: a base class ARGUS cannot
find inside the repository is written as a node marked `is_external` rather than
silently dropped.

---

## Day 5 — Make the graph answerable

**What we did.** The query API: `GET /repos/{id}/graph`, plus `/dependencies` and
`/dependents`.

**The one idea worth keeping.** `dependencies` and `dependents` are *the same
traversal with the arrow reversed*. What this needs, versus what needs this. Both
are depth-limited, because an unbounded traversal on a large repository will
happily try to return the entire graph.

Every list endpoint here is capped — the graph view at 5,000 nodes, traversals at
depth 5. A limit is not a nicety: without it, one request against a big repository
takes the whole service down.

---

## Day 6 — Remember every parse, and clean up after deletion

**What we did.** A `parse_jobs` table in PostgreSQL recording each run, and graph
cleanup when a repository is deleted.

**Why job history is separate from the repository row.** `Repository.status` is
overwritten by each re-parse — it only ever holds the latest state. The job rows
are append-only, so "why was that repository slow last Tuesday" and "how often
does this fail" remain answerable. This table is where Week 5's per-stage timings
later landed.

**Deleting across two databases.** A repository's rows live in PostgreSQL and its
nodes live in Neo4j, and there is no transaction spanning both. Deleting one
without the other leaves an orphaned graph nobody can reach — invisible, and
counted by every global query. So deletion explicitly clears the graph too.

---

## Day 7 — Do not announce work that has not finished

**What we did.** Changed when a parse is allowed to report `complete`, and
refreshed the architecture overview.

**The bug this fixed.** The status was being published as soon as PostgreSQL had
been written — but the graph write happens *after* that. For a few seconds a
repository said `complete` while its graph was still empty or half-written.
Anything asking for dependencies in that window got a confident, wrong answer:
nothing depends on this.

`complete` now means *every store has been written*. It is a one-line change and a
genuine correctness fix — the kind that never shows up in a demo, because
demonstrations are slow and race conditions are fast.

---

## The week, in one picture

```
Parser output (Week 1)
      ↓
Graph writer — MERGE on one key, stamped with a run_id
      ↓
Import resolution   → IMPORTS edges (internal files, external modules)
Call resolution     → CALLS edges, each with a resolution and a confidence
Inheritance         → INHERITS edges
      ↓
Sweep: delete anything this run did not touch
      ↓
Query API: /graph, /dependencies, /dependents
```

---

## What Week 2 did not do

Nothing was embedded and nothing was searchable by meaning — Qdrant was still
running and still empty. There was no chat, no scoring, no interface beyond the
file list. And `v0.2-graph` was never tagged.

What *did* exist by Sunday: paste a URL, and afterwards ask the graph what
depends on any function in it — with every answer carrying how sure it is.

**Next: Week 3 — retrieval and chat**, where the same code gets embedded so it can
be found by meaning, and the two retrieval methods are combined. Not written yet.
