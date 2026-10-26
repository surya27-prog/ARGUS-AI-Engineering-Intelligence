# ARGUS — RAG Design

**Status:** end of Week 3. This document explains how ARGUS answers a question
about a codebase, and why each piece is shaped the way it is. The graph's own
design is in [graph-schema.md](graph-schema.md); the system overview is in
[overview.md](overview.md).

---

## The problem with the obvious approach

The standard recipe for RAG over a repository is: split every file into
fixed-size token windows, embed them, retrieve the nearest few to the question,
paste them into a prompt. It is quick to build and it produces a demo that
mostly works.

It also has two failures that matter, and both come from the same place —
**code has structure that prose does not, and the obvious recipe throws it
away.**

**A token window is not a unit of meaning.** A 512-token slice cuts through the
middle of a function: half a body in one vector, half in the next, a docstring
separated from the code it describes. Neither fragment answers the question, and
neither can be cited as *this function* — so the user gets a passage with no
address they can check.

**Similarity retrieves what a question sounds like, not what the answer depends
on.** Ask "how is a request turned into something that can be sent over the
wire" and the nearest vectors are the functions that *say* things like that:
`Session.send`, `HTTPAdapter.send`, `Session.request`. The function that
actually does it, `PreparedRequest.prepare`, is not phrased like the question.
It is reachable in one hop along a call edge — a relationship that exists in the
code and nowhere in the embedding space.

Everything below is a response to one of those two.

---

## Chunking: by symbol

The unit is the symbol, because a symbol is what a developer asks about.

| Kind | What the chunk holds |
|---|---|
| Function / method | Signature, docstring, whole body |
| Class | Signature, docstring, and the **signatures** of its methods — an outline |
| Module | Imports, constants, and any code no symbol claimed |

Three problems fall out of that choice, and each is handled rather than ignored:

**Symbols nest.** A class contains its methods. Embedding both the full class
and each method stores the same code twice and returns one answer at two ranks.
So a class is chunked as an *outline*: its own signature and docstring plus its
methods' signatures, with the bodies left to the method chunks. The outline keeps
"what can this class do" answerable — which no individual method chunk is.

**Not all code lives in a symbol.** Imports, constants and module-level setup
answer real questions ("what does this module depend on") and belong to no
function. They become a module chunk covering only the lines nothing else
claimed, so it never duplicates a definition.

**Some symbols are enormous.** A 2,000-line function exceeds any embedding
model's input limit, so oversized bodies split into overlapping line windows
that keep their symbol identity and part numbering.

Every chunk is prefixed with a header naming its file and module. A bare body is
ambiguous — `def get(self, url)` exists in a hundred repositories — and the
header is also what the user reads as the citation.

### Identity

A chunk's point id is a UUIDv5 over `(repo_id, path, qualname, line_start,
part)` — **location, not content**. Editing a function's body leaves its id alone
and replaces its vector; a content hash would orphan the old vector on every
edit.

`line_start` is in that key because of a bug the fixture repo caught: **a
qualname is not unique within a file.** `@typing.overload` gives one function
several definitions under one name, and on `psf/requests` nine qualnames had
three to five definitions each. Without position in the key, every overload
overwrote the real implementation and 846 chunks silently became 826 points.

Keying on position is safe only because of the sweep, below.

---

## Storage: three stores, one join

| Store | Holds | Answers |
|---|---|---|
| PostgreSQL | repos, jobs, files, symbols, conversations | "what exists" |
| Neo4j | `CONTAINS`, `IMPORTS`, `CALLS`, `INHERITS` | "what depends on what" |
| Qdrant | chunk vectors + payload | "what is semantically near this" |

The load-bearing detail is that **every symbol chunk carries `symbol_key`, which
is its Neo4j node key**. That single field is the join between the vector index
and the graph. A vector hit lands on a chunk, the chunk names a graph node, and
the traversal continues from there. Without it the two stores would be
unjoinable and hybrid retrieval would be impossible.

### Re-parsing: stamp and sweep

Both the graph and the vector index use the same strategy, for the same reason.
`MERGE`/upsert makes a re-parse non-duplicating but never *removes* anything, so
a deleted function's vectors would keep answering questions about code that no
longer exists. Every write carries the parse run's `run_id`, and the run ends by
deleting whatever it did not stamp.

That is also what makes location-based chunk ids safe: code that moves gets a
new id, and the point at the old one is swept in the same run.

---

## Retrieval: hybrid

```
question
   ↓  embed
vector search (top N, filtered to this repo)
   ↓  seeds = strongest hits' symbol_keys
Neo4j: CALLS neighbours, 1–2 hops, both directions
   ↓  candidate symbol_keys
Qdrant again, restricted to those keys, ranked by the same query vector
   ↓
merge: direct hits + reserved slots for graph hits
```

Both directions of `CALLS` on purpose. Callees answer *how does this work* — the
machinery a retrieved function delegates to. Callers answer *what uses this* —
the context that explains why it exists.

Two decisions here came from measurement, not intuition, and both were wrong on
the first attempt:

**Score-based merging does not work.** Expanded hits are scored below their seed
so that context never displaces a direct answer. But that put every neighbour
around 0.20 while direct hits sat at 0.33 — so at any realistic `top_k` the
neighbours were all cut, and hybrid returned *exactly* what pure vector
returned. Zero graph results. The fix is to **reserve slots**: a fixed fraction
of the result set belongs to graph-expanded hits. That is the point of the
feature — the neighbourhood is included because it is structurally related, not
because its cosine score happened to compete.

**Neighbours must be ranked by their own relevance, not by their seed.** A seed
with thirty callees gives all thirty an almost identical derived score, so the
reserved slots went to whichever row came back first — arbitrary. Re-querying
the same vectors restricted to the neighbour set asks the question that actually
matters: *of the symbols structurally related to the good hits, which are about
this?* On the worked example the graph slots went from `dispatch_hook` and
`extract_cookies_to_jar` to `Session.prepare_request` and `Request`.

Graph expansion degrades to vector-only if Neo4j is unreachable, rather than
failing the search.

### What the measurements actually say

A ten-question eval set ([retrieval_eval.py](../../backend/tests/fixtures/retrieval_eval.py))
was written on Day 3, *before* hybrid existed, so the comparison has a fixed
target. Questions are phrased in intent rather than in the identifiers they hope
to find — a question containing the answer's name only measures keyword
matching.

| | Correct symbol in top 5 |
|---|---|
| Vector only | 8/10 (mean rank 1.1) |
| Hybrid | 8/10 |

**Hybrid does not improve top-k accuracy on this eval, and that is recorded
rather than tuned away.** It pulls in 20 additional related symbols at depth 1
and 38 at depth 2, and the ones it surfaces are relevant — but neither of the two
misses is recovered at `top_k` 5 or 10, depth 1 or 2. The trace shows why:
`PreparedRequest.prepare` is two hops from the seeds and has to outrank ~38 other
neighbours for two reserved slots.

So the honest claim is narrow: **hybrid supplies structural context that
similarity cannot reach, and that context is measurably relevant** — not that it
retrieves better by this metric. Whether the extra context produces better
*answers* is a different question, and a better one; it needs the chat output as
the signal rather than top-k membership. That is Week 5's work.

---

## Generation

The prompt is built to make ungrounded answering the awkward path.

- **Excerpts are labelled with their exact `path:line`**, and the instructions
  require an inline citation for every claim about the code. A claim the user
  cannot check is not useful.
- **The excerpts win over prior knowledge.** Stated explicitly, because this is
  the failure mode that matters: the model has seen the public version of
  whatever library this resembles, and the user is asking about *their* code.
- **Not knowing is an allowed answer.** When the excerpts do not cover the
  question, the instruction is to say so and name what is missing, rather than
  filling the gap. A wrong answer about someone's own codebase is worse than no
  answer.
- **Graph-reached excerpts are marked `via call graph`**, with an instruction to
  use them as supporting machinery rather than answering about them directly.
  The same distinction is carried into the UI as a dashed citation chip.

**Excerpts go in the user turn, not the system prompt.** The system prompt then
stays byte-identical across a conversation, which is what makes it cacheable —
prompt caching is a prefix match, so anything that changes per question has to
come after the cached part.

### Provider interfaces

Chat and embeddings are two interfaces selected by two independent env vars,
because Anthropic has no embeddings endpoint — a single `LLMProvider` carrying
both would be a shape no real provider can fill.

The chat interface has **no `temperature` parameter**. On current Claude models
`temperature`, `top_p` and `top_k` were removed and return a 400, so exposing
one would mean either a dead argument or a provider that rejects its own
contract. `effort` is the supported control and is what the interface carries.

Offline `stub` and `hash` providers are registered alongside the real ones. They
are not toys: they are why the entire retrieval, chunking and chat test suite
runs with no API key and no billable call.

---

## Conversations

Turns are stored in Postgres, and **each assistant turn keeps the citations it
was given**. Retrieval is not deterministic across re-embeds, so an answer
without its context cannot be audited after the fact — "why did it say that" is
unanswerable unless the exact excerpts are recorded.

The assistant row is created empty and filled as the stream drains, in a
`finally` block, so a client that disconnects mid-answer still leaves a record of
what was asked and what it was grounded in.

---

## Known limits

| Limit | Consequence |
|---|---|
| No type inference | `self.client.get()` cannot be resolved, so that call edge is missing and hybrid cannot expand through it |
| Chunk text is stored in Qdrant | The index holds a copy of the source; acceptable at this scale, a size problem later |
| One embedding model per collection | Switching models needs a new collection, not a migration |
| Reserved graph slots are a fixed ratio | A question needing no structural context still spends slots on it |
| Eval is ten questions on one repository | Enough to catch a regression, not enough to tune ranking confidently |

---

## What Week 4 changes

Impact analysis reuses the same graph traversal with per-hop decay, and the risk
score reads `confidence` off the same `CALLS` edges hybrid retrieval walks.
Nothing in this design blocks that; the traversal is already written and already
returns confidence-weighted paths.
