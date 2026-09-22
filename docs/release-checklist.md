# Release checklist — v0.5-rc onward

Week 5 ended in a **feature freeze**: nothing new after Day 6. What remains is
verification, deployment and documentation.

This file exists because a long stretch of Week 5 was built while the local
Docker stack was down, and the difference between "written and reviewed" and
"observed working" is the difference between a release candidate and a hope. Each
box below is something nobody has watched happen yet.

---

## Blocked on the stack coming back

Everything here needs `docker compose up -d`. Run them in this order — each one
depends on the last.

- [x] **Apply the two pending migrations.** Done 11 September 2026. Both applied
      cleanly, including the hand-written `4e21c7b9a30f` that had never run
      anywhere. CI's `Migrate` step passes too, so the chain works from empty.
- [x] **Run the whole backend suite.** Done 11 September 2026: **439 passed, 0
      failed**, against live Postgres, Neo4j and Qdrant. It found two real
      problems, which is what a suite that has never run in full is for — the
      conversations test iterated a pagination envelope (`8661795`), and a
      filtered debt report carried the unfiltered tallies, so it contradicted
      its own header (`5730984`).
- [ ] **Re-parse the fixture repository.** Symbols stored before
      `0cc6131ac5a6` carry the backfilled complexity of 1, so every
      complexity finding on them is wrong until a re-parse overwrites it.
- [ ] **Fill in `docs/performance.md`.** Its tables are deliberately empty. One
      run of the profiler produces both of them.
      ```bash
      cd backend && LLM_PROVIDER=stub EMBEDDING_PROVIDER=hash \
        .venv/Scripts/python.exe scripts/profile_pipeline.py
      ```
- [ ] **Confirm the 1,000-file target.** The Week 5 goal was a 1,000-file
      repository parsed in under five minutes. `psf/requests` is 37 files, so
      nothing measured so far speaks to it. Needs a genuinely large fixture.
- [x] **Check the coverage floor.** Measured 11 September 2026: **90%** of
      `app` (3,439 statements, 274 missed, 626 branches). The earlier 44% came
      from the 35 service-free tests — a tenth of the suite — and was never the
      real figure.

      The 60% gate is therefore slack by thirty points, and a gate with that much
      room in it will not notice a regression. Worth raising `COVERAGE_FLOOR`
      (repo **Settings → Secrets and variables → Actions → Variables**) to
      something just under the measured number — 85% leaves room for a legitimate
      dip without waving through a collapse. Set it as a variable rather than in
      `ci.yml`, which is what `${{ vars.COVERAGE_FLOOR || 60 }}` is already for.

      The thinnest files, if you want the number to go up rather than the gate to
      come down: `providers/openai_embeddings.py` 36%, `providers/anthropic_chat.py`
      58%, `services/retrieval.py` 62%. The two providers are thin because their
      tests run against the stub and hash providers, which is deliberate — CI has
      no API key.

## Pushed — 25 August 2026

- [x] **The branch is pushed.** `394cd85..eb5999a` — 33 commits and the
      `v0.4-impact` tag, on `surya27-prog/ARGUS-AI-Engineering-Intelligence`.
      The first attempt failed with `permission denied`: Git Credential Manager
      matched the credential keyed to bare `git:https://github.com` rather than
      the account with write access. Putting the username in the URL selected the
      right identity.
- [x] **The repository URL is reconciled.** `surya27-prog` is canonical. `origin`
      points there, the personal fork's remote has been removed, and
      `TIMELINE.md` and `SETUP.md` — which both named a `DeepuChandru/...` URL —
      now agree with the README's CI badge. That URL was never a third
      repository: it is the old name of the fork's account, which GitHub
      redirects.
- [ ] **Read the CI result for `5730984`.** The two earlier runs failed, both at
      the `Test` step and nowhere else — Install, Lint, the service wait and
      Migrate were green throughout. Both failures are fixed and the suite passes
      locally, so this run is expected green; it has not been read yet.

      Note that CI's Qdrant starts from no volume, so the 16 local failures that
      came from a stale one never appear there. A green CI run and a green local
      run are not quite measuring the same thing.

## Blocked on a browser

- [ ] **Graph interactivity.** Week 4 Day 5's criterion — "click a function, see
      it light up its dependents" — is verified at the data layer and has never
      been seen rendered. The Chrome extension disconnected mid-session.
- [ ] **The dashboard against live data.** Verified by rendering a static harness
      with the real CSS and measuring it (no overflow, `scrollWidth ==
      clientWidth`). Never rendered against the API.
- [ ] **The 15-minute break-it pass.** Week 5 Day 6's actual acceptance test.
      The error paths are unit-tested and the URL validator was exercised by
      hand, but nobody has tried to break the running app. Worth trying
      specifically: submit a URL to a repository that does not exist; delete a
      repository mid-parse; hold refresh on the chat page to trip the limiter;
      point at a repository with no Python in it.

## Blocked on a camera

Both need the app running with real provider keys, which is the first section of
this file.

- [ ] **The demo video.** Week 6 Day 6's criterion — "video uploaded, linked in
      README". The script and shot list are written and committed
      ([`demo-script.md`](demo-script.md)); nothing has been recorded. The
      pre-flight list in that file is the gate: real chat and embedding keys,
      `psf/requests` parsed the day before, every page warmed.
- [ ] **README screenshots.** The dashboard, the graph with a blast radius lit
      up, and a chat answer with citation chips. Same session as the recording —
      the shots are frames of shots 4, 6 and 8.

## Blocked on the GitHub UI

- [ ] **Description, topics, website field, pin, and the post.** All five are
      forms rather than commands. The text for each is written and the claims are
      checked in [`launch.md`](launch.md), including which numbers are safe to
      quote and which are not measured yet.

## Needs a decision, not a check

- [ ] **`.env` does not exist on the dev machine.** Every setting falls back to
      its default, which means `LLM_PROVIDER=anthropic` with an empty key — so
      chat and impact explanations return 503 rather than answers. Real answers
      need `cp .env.example .env` and a key. Everything verified so far used
      `LLM_PROVIDER=stub` passed per-process.
- [x] **~~The Qdrant container is 1.12.5.~~** Resolved 11 September 2026, and the
      re-embed this warned about was the least of it: `v1.19.0` does not merely
      ignore the old data, it **panics on startup** reading it —
      `unknown variant 'on_disk', expected 'mmap' or 'in_ram_mmap'` — and
      crash-loops. Sixteen tests failed on a connection refused that looked
      nothing like a storage-format problem. Fixed by dropping the
      `argus_qdrant_data` volume; vectors are derived, so a re-parse restores
      them. Worth knowing before anyone points a deployment at an old volume.
- [ ] **Whether to tag `v0.5-rc`.** The objection that blocked this is gone: the
      suite has now run in full and passes, at 90% coverage, against all three
      live stores. What is still unobserved is the deployed article — no public
      URL, no production smoke test, nothing rendered in a browser against live
      data. That is a fair thing to call a release candidate, and an unfair thing
      to call released. Reasonable to tag once CI is green on `5730984`.

---

## Known gaps that are staying

Not blockers — decisions, recorded so nobody spends Week 6 rediscovering them.
All are in [architecture/overview.md](architecture/overview.md) with reasoning.

| Gap | Consequence |
|---|---|
| Module-scope calls create no `CALLS` edge | Dead-code findings cap at 0.75 confidence and say why |
| No auth | The rate limiter keys on IP, which one office NAT shares |
| Re-parse re-embeds unchanged files | `SourceFile.sha256` exists to fix this; nothing uses it |
| The cache is per-process | Lost on restart, not shared between workers |
| Python only | One extension→language map plus an extractor per language |
| No type inference | `self.client.get()` stays unresolved, and is counted rather than hidden |
