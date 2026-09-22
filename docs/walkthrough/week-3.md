# Week 3 — Retrieval and chat

*Part of the [walkthrough](README.md). Previous: [Week 2 — the knowledge
graph](week-2.md).*

Two weeks in, ARGUS could answer questions asked in *its* language: give me the
node with this key, tell me what calls it. This week it learns to answer
questions asked in English — grounded in the actual source, with citations.

**The week's goal:** ask a question in English, get an answer built from the
code, with a file and line for every claim.

---

## The thing this week is arguing against

The standard recipe for "AI over a codebase" is: cut every file into fixed-size
chunks of text, convert each to a vector, find the few nearest to the question,
paste them into a prompt. It is quick to build and demonstrates well.

It has two failures, and both come from the same place — **code has structure
that prose does not, and the obvious recipe throws it away.**

**A fixed-size window is not a unit of meaning.** A 512-token slice cuts through
the middle of a function: half a body in one chunk, half in the next, a docstring
separated from the code it describes. Neither fragment answers the question, and
neither can be cited as *this function*, so the user gets a passage with no
address they can go and check.

**Similarity finds what a question sounds like, not what the answer depends on.**
Ask *"how is a request turned into something that can be sent over the wire"* and
the nearest vectors are the functions that **talk** like that — `Session.send`,
`HTTPAdapter.send`. The function that actually does it, `PreparedRequest.prepare`,
is not phrased like the question at all. It is one hop away along a call edge — a
relationship that exists in the code and nowhere in the vector space.

Everything this week is a response to one of those two.

---

## Day 1 — Pick the providers, and hide them behind two doors

**What we did.** Defined two interfaces — `ChatProvider.complete()` and
`EmbeddingProvider.embed()` — with one real implementation behind each, selected
by an environment variable.

**What an embedding is.** A model converts a piece of text into a long list of
numbers, positioned so that things meaning similar things sit near each other.
"Retry the request" and "attempt again after a failure" land close together even
with no words in common. Searching becomes *find the nearest points*, which is
why meaning-based search works at all.

**Why two interfaces and not one.** Chat and embeddings are **separate
choices**, and the reason is concrete rather than theoretical: Anthropic has no
embeddings endpoint. Setting `LLM_PROVIDER=anthropic` still leaves the question of
who does the embedding entirely open. Bundling them into one "AI provider" would
have made a legal configuration impossible to express.

**The decision that paid for itself repeatedly.** Each interface has a fake
implementation — a `stub` chat provider and a `hash` embedding provider that
turns text into a deterministic vector with no model involved. That means the
whole test suite and CI run **with no API key and no cost**, and every part of
the system that does not need a model can be exercised for free. Every
verification run in this project since has used them.

---

## Day 2 — Chunk by symbol, not by size

**What we did.** Split code into pieces, embedded them, stored them in Qdrant.

**The rule: one chunk = one symbol**, because a symbol is what a developer asks
about.

| Kind | What the chunk holds |
|---|---|
| Function or method | Signature, docstring, whole body |
| Class | Signature, docstring, and its methods' **signatures** — an outline |
| Module | Imports, constants, and any code no symbol claimed |

Three problems fall out of that choice, and each is handled rather than ignored:

**Symbols nest.** A class contains its methods. Embedding the whole class *and*
each method stores the same code twice and returns one answer at two different
ranks. So a class is stored as an **outline** — its signature and docstring plus
its methods' signatures, bodies left to the method chunks. The outline is what
answers "what can this class do", which no individual method chunk can.

**Not all code lives in a symbol.** Imports, constants and module-level setup
answer real questions and belong to no function. They become a module chunk
covering only the lines nothing else claimed, so nothing is stored twice.

**Some symbols are enormous.** A 2,000-line function exceeds any embedding
model's input limit, so oversized bodies split into overlapping windows that keep
their symbol identity and a part number.

Every chunk is prefixed with a header naming its file and module. A bare body is
ambiguous — `def get(self, url)` exists in a hundred repositories — and that
header is also what the user reads as the citation.

---

## Day 3 — Search, and decide how you will know if it works

**What we did.** A semantic search endpoint, and a **ten-question evaluation
set**.

**Why the eval set is the important half.** "Does retrieval work?" is not
answerable by looking at results and feeling pleased. So ten questions were
written down, each paired with the symbol that ought to come back, and the score
is how often it lands in the top five.

Two details that make it worth having:

- The questions are phrased as **intent**, not as identifiers. A question
  containing the answer's function name only measures keyword matching, and would
  flatter any implementation.
- It was written on **Day 3, before hybrid retrieval existed**. That fixes the
  target in advance, so Thursday's work is measured against a benchmark it could
  not have been shaped around.

Baseline: **8 out of 10**, mean rank 1.1.

---

## Day 4 — Hybrid retrieval, the differentiator

**What we did.** Combined the two ways of finding code — meaning and structure.

The pipeline:

```
question
   ↓ embed
vector search → the strongest hits
   ↓ use those as seeds
Neo4j: their CALLS neighbours, 1–2 hops, both directions
   ↓
search again, restricted to those neighbours, against the same question
   ↓
merge: direct hits + reserved slots for graph hits
```

**Both directions on purpose.** Callees answer *how does this work* — the
machinery a function delegates to. Callers answer *what uses this* — the context
explaining why it exists.

**Two attempts failed first, and both failures are instructive.**

*Merging by score does not work.* Expanded hits were scored below their seed so
context could never displace a direct answer. But that put every neighbour around
0.20 while direct hits sat at 0.33 — so at any realistic cut-off the neighbours
were **all** removed, and hybrid returned exactly what pure vector returned. Zero
graph results. The fix is to **reserve slots**: a fixed share of the results
belongs to graph-expanded hits. That is the entire point — the neighbourhood is
included because it is *structurally related*, not because its similarity score
happened to compete.

*Neighbours must be ranked by their own relevance, not inherited from their seed.*
A seed with thirty callees gave all thirty near-identical derived scores, so the
reserved slots went to whichever row came back first — arbitrary. Re-running the
search restricted to the neighbour set asks the question that actually matters:
*of the symbols structurally related to the good hits, which are about this?* On
the worked example, the graph slots changed from `dispatch_hook` and
`extract_cookies_to_jar` to `Session.prepare_request` and `Request` — from noise
to exactly right.

If Neo4j is unreachable, expansion is skipped and search degrades to vector-only
rather than failing.

---

## Day 5 — The chat endpoint

**What we did.** `POST /chat`: retrieve context, build a prompt, stream an answer
with citations, store the conversation.

**Server-Sent Events (SSE)** is the streaming mechanism — a single HTTP response
the server keeps open, pushing pieces as they are produced. It is why the answer
appears word by word instead of after a long pause.

**The ordering decision.** The stream sends one `context` frame carrying the
citations **first**, then the text, then `done`. The user sees *what the answer
will be built from* before any of it is written. That is the difference between
an answer you can check and an answer you must trust — and checking is the entire
premise of the product.

Conversations are stored in PostgreSQL so a thread has history and a follow-up
question makes sense.

---

## Day 6 — The chat interface

**What we did.** A message list that renders the stream as it arrives, and
**citation chips** — each retrieved symbol shown as a small link that opens the
file at the cited line.

The chips are colour-coded by where the context came from: found by similarity,
or pulled in by graph expansion. A user can see the hybrid retrieval working
rather than taking the architecture's word for it.

---

## Day 7 — Write down the design, including the part that did not work

**What we did.** [`architecture/rag-design.md`](../architecture/rag-design.md),
and re-ran the eval set against the finished system.

**The result, which is the most interesting thing in this week:**

| | Correct symbol in top 5 |
|---|---|
| Vector only | 8/10 (mean rank 1.1) |
| Hybrid | 8/10 |

**Hybrid did not improve top-k accuracy — and that is recorded rather than tuned
away.** It pulls in 20 extra related symbols at one hop and 38 at two, and the
ones it surfaces are relevant, but neither of the two misses is recovered at any
setting tried. The trace shows why: the missing symbol sits two hops from the
seeds and would have to outrank ~38 other neighbours for two reserved slots.

So the claim in the documentation is deliberately narrow: **hybrid supplies
structural context that similarity cannot reach, and that context is measurably
relevant** — *not* that it retrieves better by this metric.

It would have been easy to quietly adjust the eval set, or the depth, or the
number of reserved slots until the number moved. The number is 8/10 both ways, in
writing, in the repository. A project whose whole argument is that a tool should
admit what it does not know cannot make an exception for its own headline
feature.

---

## The week, in one picture

```
Code → chunk by symbol → embed → Qdrant
                                    ↓
Question → embed → nearest chunks ──┤
                                    ↓ seeds
                        Neo4j: CALLS neighbours, both directions
                                    ↓
                        re-rank neighbours against the question
                                    ↓
              direct hits + reserved graph slots → prompt
                                    ↓
                    SSE: citations first, then the answer
```

---

## What Week 3 did not do

Nothing was scored or ranked by importance — no risk, no blast radius, no
dashboard. The graph was being used to *find* code, not yet to judge it. And
`v0.3-chat` was never tagged.

What existed by Sunday: ask a question in English and get an answer built out of
the repository's own code, with a clickable citation for every part of it.

**Next: [Week 4 — impact, risk and the graph](week-4.md)**, where the confidence
values from Week 2 finally get multiplied along a path and turned into a number.
