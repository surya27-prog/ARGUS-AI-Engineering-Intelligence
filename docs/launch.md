# Launch kit

The text for the things that live outside the repository — GitHub's description
and topics, the write-up post — plus the order to do them in.

Written here rather than typed straight into a form so the claims can be checked
against the code, and so publishing twice says the same thing.

---

## Publish nothing until this is true

The post links to a repository and a live URL. Both have to work before anyone
reads it.

1. ~~Push the branch.~~ Done on 25 August 2026: `394cd85..eb5999a` on
   `surya27-prog`, with the `v0.4-impact` tag.
2. CI green. The push above triggered the first run ever; nobody has read the
   result. A red badge on a freshly announced repository is worse than no
   announcement.
3. Deployed, with the URL loading and answering — Week 6 Days 2–3, still
   outstanding.
4. `scripts/smoke_test.py <url>` passing against production.
5. The demo video recorded and linked ([`demo-script.md`](demo-script.md)).

Steps 1 and 2 are enough to set the description, topics and LICENSE. The post
needs all five.

---

## Repository description

GitHub allows 350 characters. This is 247:

> Answers "what breaks if I change this?" for a Python codebase. Parses a repo
> into a Neo4j knowledge graph plus a Qdrant vector index, then serves
> confidence-weighted impact analysis, risk scores, technical-debt detection and
> grounded chat over it.

It leads with the question rather than the architecture, because the architecture
is the second thing anyone wants to know and the description is often read
alongside forty others.

## Topics

GitHub allows 20; these are 16, ordered so the truncated view keeps the useful
ones:

```
knowledge-graph  code-analysis  static-analysis  neo4j  qdrant  rag
vector-search    fastapi        python           nextjs typescript
impact-analysis  technical-debt  ast              cypher  developer-tools
```

`code-analysis`, `knowledge-graph` and `rag` are the three anyone browsing for
this kind of project actually searches. `llm` and `ai` are deliberately left off —
they are noise-dense tags that would put this next to a hundred wrappers.

## Website field

Set it to the deployed frontend URL, not the API. Someone clicking it wants the
interface.

---

## The post

For LinkedIn. The first line is the whole hook — everything after it is behind
"see more".

---

I spent six weeks building a tool to answer one question: **if I change this
function, what breaks?**

Grep answers it badly. It finds the string, not the caller three hops away, and
it can't tell you which of forty matches matters.

ARGUS parses a Python repository into a knowledge graph (Neo4j) and a vector index
(Qdrant), keeps the flat facts in Postgres, and answers architecture, impact and
technical-debt questions from all three.

Two decisions I'd defend:

**Hybrid retrieval.** Vector search finds code that *reads* like your question.
The call graph finds code that's *connected* to it. Neither alone is enough, so
ARGUS retrieves by similarity and then expands through call edges before the model
sees anything — an answer can cite machinery a semantic search would never have
surfaced.

**Every inferred edge carries a confidence.** Python resolves plenty of calls at
runtime, so a static call graph is necessarily incomplete. Each edge records how
it was resolved — a local name at 1.0, a method on `self` at 0.85 — and blast
radius multiplies those confidences along the path. Ambiguous call sites produce
no edge at all and are counted instead, because a guess that inflates a blast
radius is worse than a gap you can see.

The most useful thing I learned had nothing to do with models. My first
circular-import detector reported 200 findings. Three were worth acting on — the
rest were supersets of the same few small cycles. Filtering to minimal cycles cut
it to 9. A tool that reports 200 problems teaches people to close the tool, and
most of the work on the debt detectors after that was calibration rather than
detection.

Stack: FastAPI, Neo4j, Qdrant, Postgres, Next.js, Docker, GitHub Actions.

Code: <repo URL>
Live: <deployed URL>
Demo: <video URL>

---

### Portfolio or CV version

One paragraph, no links:

> **ARGUS — AI Engineering Intelligence Platform.** Ingests a Python repository
> into a Neo4j knowledge graph and a Qdrant vector index, and answers change-impact,
> architecture and technical-debt questions over it. Blast radius is
> confidence-weighted: each inferred call edge records how it was resolved, and
> ambiguous call sites are counted rather than guessed. FastAPI, Postgres,
> Next.js, Docker, CI.

---

## Numbers, and which are safe to quote

Measured, as of Week 6 Day 7:

| | |
|---|---|
| Application code | ~14,400 lines (backend 9.3k, parser 1.9k, frontend 2.8k, migrations 0.4k) |
| Tests | 475 across 26 files, ~7.6k lines |
| API operations | 25 across 23 paths |
| Debt detectors | 5 |

**Do not quote these yet** — nothing has measured them:

- test coverage (CI gates at 60%; the only datapoint is 44% from a tenth of the
  suite)
- "1,000 files in under five minutes" — the Week 5 target, never run against a
  repository that size
- "all tests passing" — the suite has not run in full since Week 5 Day 1

Say "475 tests" if you like; it is a count of what exists. "475 passing tests" is
a claim about a run that has not happened.

---

## The manual steps

Nothing here can be done from a terminal.

- [ ] Repository **About**: paste the description, the topics and the website URL
- [ ] Confirm the LICENSE holder. `LICENSE` names Deepanjali Chandrasekaran; the
      history has one commit from another author (`suryah`, the initial commit on
      `main`), so if this is joint work the copyright line needs both names
- [ ] Pin the repository on the GitHub profile
- [ ] Publish the post, once all five gates above are met
- [x] ~~Reconcile the repository URL.~~ `surya27-prog` is canonical; `TIMELINE.md`
      and `SETUP.md` now match the README's badge. The `DeepuChandru/...` URL that
      appeared in the docs was the fork account's old name, which GitHub
      redirects — not a third repository
