# Demo script — 5m30s

A shot list for the walkthrough video, written to be read while recording.

The plan for this day was "script it, record clean takes". The script is here;
**the recording is not done** — see [Recording status](#recording-status) at the
bottom. Everything above that line is what to say and click when the stack is up.

Order follows the question a viewer actually has: *what problem does this solve,
and does it work?* Ingest is shown but not waited for. Impact analysis is the
centre of the video, because it is the one thing here that grep cannot do.

---

## Before you record

Nothing on this list is optional. Each item is something that, missed, costs a
re-record rather than a re-take.

**A real key, not the stubs.** `LLM_PROVIDER=anthropic` and a real
`EMBEDDING_PROVIDER`. The `stub` and `hash` providers exist so CI can run without
credentials — on camera they produce fluent nonsense and citations that point at
unrelated files. Confirm before recording: `GET /health` must show
`llm_configured: true` and `embedding_configured: true`.

**Parse the demo repository the day before.** `https://github.com/psf/requests`
takes minutes to clone, parse, embed and write. That is not a shot. Have it
`complete` and sitting in the list before the camera starts.

**Have a second repository ready but unparsed** — something small — for the
ingest shot. You submit it and cut away while it is still `parsing`.

**Warm every page.** Visit the dashboard, graph, chat and files pages once and
ask one throwaway question. First load compiles routes in dev, and the first
impact call fills the parse-scoped cache; a cold spinner reads as "slow tool" to
a viewer who has no idea what a cold cache is.

**Pick one function and keep it for the whole video** — shots 2, 6, 7 and 8 are
one story about one change, and swapping functions halfway makes them four
disconnected demos. `Session.request` in `psf/requests` works well: it has real
dependents, and the name means something to anyone who has used the library.

**Find that node on the canvas first, and note where it sits.** The graph is
click-only — `graph/search` exists as an endpoint but the canvas has no search
box, so hunting for a node on camera is dead air. Narrow with the **SHOW** type
filters, find your function, and remember roughly where the layout puts it.
Cytoscape's layout is not stable across reloads, so do this on the same page load
you record, or budget a few seconds of quiet panning.

**Have the follow-up question ready.** Shot 8 uses the pre-filled question the
graph hands over, but if you want a second one, try it first — an answer that
comes back weak is a bad shot, and you cannot tell which those are without
asking.

**Window and capture.** 1440×900, scale 100% — a 1.25 DPR display crops the right
edge of the dashboard in a naive capture, which has already caused one false
alarm in this project. Hide the bookmarks bar, close other tabs, and use a clean
browser profile so no extension icons or personal URLs are in frame. Record at
30fps; the graph animates and 15fps looks broken.

**Zoom in.** Bump the browser to 110–125% for the code and citation shots. Text
that is comfortable on your monitor is unreadable in a 720p embed.

**Numbers.** Read every figure off the screen while narrating. Do not read them
from this file — an earlier parse of `psf/requests` gave 807 symbols, 591 imports,
2,687 call sites and 509 debt findings, and yours will differ with the default
branch. A narrated number that contradicts the screen is the one thing every
viewer notices.

---

## Shot list

| # | Time | Screen | What you do | What you say |
|---|---|---|---|---|
| 1 | 0:00–0:25 | Editor, a large file open | Scroll slowly through a long module. Stay silent for the first two seconds. | "You've joined a codebase with a million lines in it. Before you can change anything safely, you need to know what depends on it. The docs are stale and the authors have left." |
| 2 | 0:25–0:40 | Terminal, `grep -rn "\.request(" requests/` | Let the match list fill the screen. | "Grep finds the string. It doesn't find the caller three hops away, and it can't tell you which of these matters." |
| 3 | 0:40–1:05 | ARGUS home, `/` | Paste a git URL, submit. Status goes `pending` → `parsing`. | "ARGUS takes a repository — a URL or a zip. It clones it, parses every Python file into a call graph, and embeds the code for search. On a real repository that takes a few minutes, so here's one already done." |
| 4 | 1:05–1:50 | `/repos/{id}` dashboard | Cut to the parsed repo. Sit on **At a glance**. Then the **Risk by file** heatmap; hover one dark cell. Then click a function in **Riskiest functions** — the panel becomes **Why this score**. | "Files, symbols, call sites — all from static analysis, no model involved. Risk combines blast radius, coverage, centrality, coupling and churn. And clicking a score shows the factors and the weights behind it, because a bare number out of a hundred is not something you can argue with." |
| 5 | 1:50–2:35 | `/repos/{id}/graph` | Open on **Files & imports**. Switch to **Calls**. Set **Colour → by risk**. Tick **Collapse by module**, node count drops. Untick it, and end there — the next shot needs this view, and every view or node-cap change re-runs the layout and moves your node. | "Two views: files and their imports, or symbols and their calls. Colour by risk to see where the dangerous parts cluster. And collapse by module when the call graph is too dense to read — the edges get remapped onto the modules, not just hidden." |
| 6 | 2:35–3:30 | Same page, **Selection** panel | Click the node you located during pre-flight — there is no search box on this canvas. Read **affected / direct / max hops** aloud. Scroll the ranked list and point at a route. | "This is the part grep can't do. Everything that reaches this function, ranked — not by how many hops away it is, but by confidence times decay. Two certain hops beat one guessed one. And every row shows the route it travelled, so you can check the tool's work." |
| 7 | 3:30–3:55 | Same panel | Click **Explain this in English**. Keep the two-second wait in. | "Same data, in prose, for the pull request description. It reads the highest-scoring nodes and tells you how many it read. Cached per parse, so asking twice costs one model call." |
| 8 | 3:55–4:40 | → `/repos/{id}/chat` | Click **Ask follow-ups in chat →**. The box arrives pre-filled with *"What breaks if I change …?"* and a focus note naming the node. Press **Ask**. Let the citation chips appear before the text, then click one — the file opens at the cited line. | "The graph hands the node over to chat, so the answer is built from the blast radius as well as the retrieved code — no guessing intent from the wording. Citations arrive before the text, so you can see what the answer will be built from, and each one opens the actual lines. Retrieval is hybrid: vector search finds code that reads like the question, then it expands through call edges to pull in machinery a similarity search would never surface." |
| 9 | 4:40–5:15 | Dashboard, **Technical debt** | Scroll the findings. Point at a confidence value. Click **Download the full report (Markdown)** and show the file. | "Five detectors, calibrated against this repository's own distribution — so a finding means unusual *here*, not over some global threshold. Dead code caps at 0.75 confidence, because a call at module scope is invisible to a call graph, and the tool would rather admit that than tell you to delete a live handler." |
| 10 | 5:15–5:30 | Graph, blast radius still lit | Hold still. No clicking. | "Knowledge graph, vector index, and a confidence on every inferred edge. Code, deployment notes and design decisions are in the repo." |

---

## The three things that must land

If a take runs long, cut from shots 4, 5 and 9 — not these.

1. **Impact is scored, not counted** (shot 6). It is the claim that separates this
   from a grep wrapper.
2. **Citations arrive before the prose** (shot 8). Visible proof the answer is
   grounded rather than recalled.
3. **Confidence is reported, including where it is low** (shots 6 and 9). The
   thing most tools in this space round up to certainty.

---

## If something breaks mid-take

Keep recording and finish the shot — whether a take is usable is an editing
decision, and stopping mid-sentence guarantees it isn't.

- **A 503 naming a store.** A dependency dropped. Stop, fix it, restart the take;
  don't narrate over an error panel.
- **A 429.** You re-took a chat or explain shot too many times in a minute — chat
  is 10/min with a burst of 4. Wait out `Retry-After` and resume.
- **An empty graph.** Wrong view for the repository, or the node cap is hiding it.
  Raise **Max nodes** rather than explaining the cap on camera.
- **A weak chat answer.** Don't re-ask the same question hoping for better. Cut to
  the question you tested during pre-flight.

---

## After recording

1. Export 1080p, keep it under six minutes.
2. Upload unlisted first and watch it once at 720p on a phone. Anything
   unreadable there is unreadable for most viewers.
3. Make it public, then replace the placeholder in the README's **Demo** section
   with the link.
4. Tick the video line in [`release-checklist.md`](release-checklist.md).

---

## Recording status

**Not recorded.** The video needs a running stack with real provider keys, and
neither has been available on this machine — the data stores have been down for
most of this stretch and there is no `.env` with a real key in it. The script is
committed on its own so that recording is half an hour of work rather than a day
of deciding what to show.

Outstanding before it can be recorded: bring up `docker compose`, apply the two
pending migrations, configure real chat and embedding providers, and parse
`psf/requests`. All of that is already on
[`release-checklist.md`](release-checklist.md).
