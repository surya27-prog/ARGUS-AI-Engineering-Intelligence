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
3. Use this entry shape:
   ```markdown
   ## <Weekday, DD Month YYYY> at <HH:MM> (UTC<offset>)

   **<Week N, Day(s) — short theme>**

   <One or two sentences on what was accomplished.>

   - <specific change, with concrete versions/names where relevant>
   - <specific change>

   **Next:** <the next task from TIMELINE.md>

   ---
   ```
4. Summarize what was **actually done in the session**, not what was planned.
   Name real versions, file paths, and commands. If something was attempted and
   failed or was left incomplete, say so — the log is for honest review, not a
   highlight reel.
5. Confirm to the user in one line that the entry was added, and don't print the
   whole entry back to them.

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
