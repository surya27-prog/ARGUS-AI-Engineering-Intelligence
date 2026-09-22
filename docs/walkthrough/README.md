# Walkthrough — how ARGUS was built

Six weeks, one week per page: what was built each day, which technology it used,
and why that one rather than an alternative.

This is not the architecture reference — that is
[`architecture/overview.md`](../architecture/overview.md), which describes the
system as it stands today. These pages describe it as it was *assembled*, in
order, with the reasoning still attached. Read them if you want to know why the
pieces are shaped the way they are; read the architecture docs if you only need
to know what they do now.

Every technology is explained where it first appears, so no prior familiarity
with the stack is assumed.

| Week | Subject | |
|---|---|---|
| 1 | Foundations and the parser — Docker, the three stores, FastAPI, Python's `ast` | [read](week-1.md) |
| 2 | The knowledge graph — Neo4j, Cypher, and the confidence model | not written yet |
| 3 | Retrieval and chat — embeddings, Qdrant, hybrid retrieval | not written yet |
| 4 | Impact, risk and the graph UI | not written yet |
| 5 | Technical debt, caching, CI and hardening | not written yet |
| 6 | Deployment, documentation and shipping | not written yet |

## A note on the numbers

No page here uses invented figures. Where a measurement appears it was taken from
a real run against a real repository, and the ones that have never been measured
are named as such — in [`performance.md`](../performance.md) and
[`release-checklist.md`](../release-checklist.md). There is no demo dataset in
this project; every figure it has ever displayed came from parsing real source
code at runtime.
