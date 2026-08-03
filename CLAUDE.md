# ARGUS — Project Instructions

AI Engineering Intelligence Platform. Ingests a repository, builds a knowledge
graph plus a vector index over it, and answers architecture, impact, and
technical-debt questions about the codebase.

## The `#workdone` keyword

When the user types `#workdone` (anywhere in a message), append a new entry to
[`docs/planning/WORKLOG.md`](docs/planning/WORKLOG.md):

1. **Never delete or rewrite existing entries.** Append only, at the end of the
   file, so the log reads top-to-bottom as the project progresses.
2. Get the real timestamp from the system — do not guess it:
   ```
   powershell -c "$n = Get-Date; '{0:dddd, dd MMMM yyyy} at {0:HH:mm} (UTC{1})' -f $n, (Get-Date -Format 'zzz')"
   ```
3. **Keep entries short.** Simple bullets of what was done — no prose
   paragraphs, no explanations, no "Next:" section. Use this shape:
   ```markdown
   ## <Weekday, DD Month YYYY> at <HH:MM> (UTC<offset>)

   Week N, Day(s) — branch `<branch>`

   - <what was done>
   - <what was done>

   ---
   ```
   Always record the branch the work was committed on. Get it from the system,
   don't assume it: `git rev-parse --abbrev-ref HEAD`.
4. List what was **actually done in the session**, not what was planned. If
   something failed or was left incomplete, add a bullet saying so.
5. Reply with one line confirming the entry was added. Don't print it back.

## Project conventions

- The delivery plan is [`docs/planning/TIMELINE.md`](docs/planning/TIMELINE.md).
  Check it for what the current day's task is before starting work.
- Work happens on `surya_branch`. `main` and `deepu_branch` also exist on the
  remote; `main` has unrelated history from the initial commit.
- Every day ends with a commit.
- Data stack runs via `docker compose up -d` — see [`docs/SETUP.md`](docs/SETUP.md).
- Chat and embedding providers are configured **separately** in `.env`.
  Anthropic has no embeddings endpoint, so `LLM_PROVIDER=anthropic` still needs
  an `EMBEDDING_PROVIDER` set.
