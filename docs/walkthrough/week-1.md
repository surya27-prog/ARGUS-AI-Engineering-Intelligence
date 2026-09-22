# Week 1 — Foundations and the parser

*Part of the [walkthrough](README.md): what was built each week, which technology
it used, and why that one.*

Written for someone who has not seen this codebase before — every tool is
explained where it first appears. If you already know the stack, the day
headings alone are the summary.

**The week's goal:** a repository goes in, structured facts come out, and you can
see them in a browser.

---

## Day 1 — Set up the workbench

**What we did.** Created the folder structure, the tool configuration, and a
template listing every setting the application would need.

**The technology, and why:**

**Git**, with a `.gitattributes` file. Git is version control — it remembers
every change, so you can see what altered and go back. The `.gitattributes` file
settles a Windows-specific problem: Windows ends each line of a text file with
two invisible characters (`CRLF`), Linux and macOS use one (`LF`). Without a
rule, every file looks modified the moment someone else opens it, and real
changes drown in noise. Development happens on Windows here and CI runs on Linux,
so this is settled on day one rather than fought for six weeks.

**Ruff**, a linter and formatter for Python. A linter reads code without running
it and reports unused imports, shadowed names, and constructs that are usually
bugs. Ruff does it in milliseconds, so it can run on every save. The point is not
tidiness — it is that a whole category of mistake is caught before a test ever
runs.

**`.env.example`.** A template naming every setting the app reads — database
addresses, API keys — with placeholder values. The real `.env` is never
committed. Anyone cloning the repository learns what they must supply without a
secret ever reaching GitHub.

---

## Day 2 — Start the three databases

**What we did.** One command — `docker compose up -d` — that starts all three
data stores together.

**The technology, and why:**

**Docker** packages a piece of software together with everything it needs to run:
libraries, configuration, the lot. The usual image is a shipping container — what
is inside does not care which ship carries it. This matters because installing
PostgreSQL, Neo4j and Qdrant by hand takes hours and fails differently on every
machine. With Docker they behave identically on a laptop and on a server.

**Docker Compose** describes several containers in one file and starts them as a
unit. Three databases, one command, the same for everyone.

Then the three stores themselves, which are the architectural decision of the
week:

**PostgreSQL** is a relational database: data in tables of rows and columns, like
a spreadsheet with rules enforced. It holds the **flat facts** — this file has 340
lines, this function has a complexity of 12, this parse finished at 14:05. Asking
"which files are longest?" is a fast, ordinary query.

**Neo4j** is a *graph* database. Instead of tables it stores **things and the
connections between them**. The question this project exists to answer — *what
eventually calls this function, three steps away?* — is a short query in Neo4j
and an unpleasant one in SQL, where each extra step means another join.

**Qdrant** is a *vector* database. Text can be converted into a long list of
numbers, called an embedding, positioned so that things meaning similar things
sit near each other. Qdrant stores those and finds the nearest ones quickly. It
is what lets a search for *"how does retrying work"* find the right code even when
the word "retry" never appears in it.

**Why three.** Each answers the kind of question it is actually good at.
Structure to the graph, meaning to the vectors, plain facts to the relational
database. Forcing all three into one store would mean doing two of the jobs
badly.

---

## Day 3 — The API skeleton

**What we did.** A web server that starts, connects to PostgreSQL, reports its
health, and owns its first database table.

**The technology, and why:**

**FastAPI**, a Python framework for building web APIs. You write an ordinary
Python function, and it becomes a URL. It checks incoming data against the types
you declared and generates interactive documentation from them. That last part
paid off in Week 6, when the API reference was generated rather than written.

**SQLAlchemy** lets the database be addressed as Python objects rather than
hand-written SQL strings. The shape of each table is declared once, in one place,
and a mistyped column becomes an error rather than a query that silently returns
nothing.

**Alembic** is version control for the database's *shape*. Each change — a new
table, an added column — is a numbered file, applied in order. Your laptop, CI
and production therefore end up with an identical database, reproducibly, rather
than someone remembering which columns they added by hand.

**pydantic-settings** reads configuration from environment variables and
**validates it at startup**. A malformed database URL fails immediately, with a
message naming the setting, instead of surfacing three minutes later in the middle
of a request.

**A real bug, found the same day.** `/health` hung indefinitely when a database
was unreachable, because it waited for a connection with no timeout. A health
check that hangs is worse than one that fails: monitoring cannot distinguish
"slow" from "dead", so nothing gets restarted and no alarm fires. Fixed with a
timeout, and `/health` has answered within a bounded time ever since.

---

## Day 4 — Find the files worth reading

**What we did.** Code that takes a git URL or a zip, puts the contents in a
working directory, walks the tree, keeps the Python files and skips the rest.

**Why it is a day of its own.** Most of a repository is not source code.
`node_modules`, `.git`, virtual environments and build output can outnumber real
files by a hundred to one, and parsing them is wasted time and misleading
results. Equally, a skipped file must be *recorded as skipped* with a reason,
because "this file produced no symbols" and "this file was never read" look
identical in the output and mean completely different things.

---

## Day 5 — Read the code properly

**What we did.** Extraction: every function, class, import and call site in a
file, turned into plain Python objects.

**The technology, and why:**

**Python's `ast` module**, part of the standard library. It reads source code and
returns its **structure** as a tree — this is a class, containing a function,
which calls that other thing. The alternative is searching the text with regular
expressions, which cannot tell a real function from the word `def` inside a
comment or a string. The AST is what Python itself uses to run your code, so it
agrees with Python by construction.

**Dataclasses** hold the output: simple, typed containers, with no database or
framework involved. The parser is a pure function from a directory to data, which
is why it can be tested without any of the three databases running.

**The most consequential decision of the week.** The parser's output shape was
**frozen** here. Everything built afterwards reads this exact structure — the
graph writer in Week 2, the chunker in Week 3. The plan warned that changing it in
Week 3 would cost two days. It never had to change.

One detail that shows the level this was worked at: a function defined inside
another function is recorded as `outer.inner`, not Python's raw
`outer.<locals>.inner`. Left alone, that spelling would have leaked into the
graph, the user interface and every citation the product ever shows.

---

## Day 6 — Upload one, and see it work

**What we did.** An endpoint accepting a GitHub URL or a zip file, which parses
in the background, and a web page to drive it.

**The technology, and why:**

**Next.js** and **React**. React builds interfaces out of reusable pieces; Next.js
is the framework around it that handles routing, building and serving. The same
foundation later carried the graph, the dashboard and the chat page.

**TypeScript** is JavaScript with type checking, so a misspelled field is caught
while writing rather than by whoever is using the page.

**Background work, and `202 Accepted`.** Parsing a real repository takes minutes —
far longer than a browser or any sensible HTTP timeout will wait. So the API
accepts the request, answers **202** immediately, meaning *I have started, this is
not finished*, and does the work afterwards. The page then polls until the status
settles on `complete` or `failed`. Every long-running operation in the product
follows this shape.

**Why this day mattered.** It was the first moment the thing was *usable* — paste
a URL, watch a file list appear.

---

## Day 7 — Write down why

**What we did.** `docs/architecture/overview.md`: how the pieces fit, and what was
deliberately left out.

**Why.** The reasoning behind a decision is worth more than the decision itself,
and it evaporates in about a week. This document is the reason Week 3 never
reopened arguments Week 2 had already settled.

---

## The week, in one picture

```
You paste a GitHub URL
      ↓
FastAPI accepts it and answers 202 immediately
      ↓
The parser clones it, walks the tree, reads each file with Python's `ast`
      ↓
Files and symbols are stored in PostgreSQL
      ↓
The Next.js page polls until the status is `complete`
```

Neo4j and Qdrant were **started but not yet used**. Week 1 filled PostgreSQL
only; the graph came in Week 2 and the vectors in Week 3. Standing all three up on
day 2 meant no infrastructure surprises later — which is exactly the risk the plan
called out for that day.

---

## What is real data, and what is a fixture

Worth stating plainly, because it is the first question anyone sensible asks.

**There is no demo dataset, and no seeded data.** `database/seeds/` is empty.
Every number this project has ever shown — 37 files, 807 symbols, 509 debt
findings — was produced by cloning a real repository from GitHub and computing
it. Point ARGUS at a repository nobody here has seen and it produces that
repository's numbers.

There are **188 lines of test fixtures**, and they exist for testing rather than
for demonstrating:

| Fixture | Lines | What it is for |
|---|---:|---|
| `parser/tests/fixtures/simple_module.py` | 26 | Imports, a function, an `async def` |
| `classes_module.py` | 38 | Classes, inheritance, methods |
| `edge_cases.py` | 42 | Nested functions, decorators, the awkward shapes |
| `broken_syntax.py` | 4 | Deliberately invalid — proves a bad file is skipped, not fatal |
| `backend/tests/fixtures/retrieval_eval.py` | 78 | ~10 questions and the files that should be retrieved |

The first four are **inputs whose correct answer is known by counting**. A test
asserting "this file contains exactly three functions and two imports" needs a
file small enough to verify by hand. You cannot write that test against
`psf/requests`, because nobody knows its exact symbol count — which is the reason
this tool exists at all.

None of these appear in any output. They are inputs to tests, never sources of
anything a user sees.

---

## What Week 1 did not do

Stated so the next week's starting point is honest: no graph was written, nothing
was embedded, nothing was scored, and the planned `v0.1-parser` tag was never
created. The week ended with a repository going in and a file list coming out,
which was the goal.

**Next: Week 2 — the knowledge graph**, where the structure becomes a graph and
every inferred edge starts carrying a confidence. Not written yet.
