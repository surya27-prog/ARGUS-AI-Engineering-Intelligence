# ARGUS — AI Engineering Intelligence Platform

> Ask your codebase questions. Understand what breaks before you break it.

ARGUS ingests a software repository and builds a combined **knowledge graph + vector index** of it, then uses an LLM over both to explain architecture, answer engineering questions with citations, predict the blast radius of a code change, and surface technical debt.

**Status:** 🚧 In development (6-week build, 3 Aug – 13 Sep 2026)

---

## The problem

You join a company with millions of lines of code. Before you can safely change anything, you need to know what depends on it. Documentation is stale, the original authors have left, and reading the code takes weeks.

ARGUS answers *"If I change this API, what breaks?"* with a graph, a risk score, and evidence from the actual source.

## Features

| Feature | Description |
|---|---|
| **Repository ingestion** | Point at a Git URL or upload a zip |
| **Dependency graph** | Files, modules, classes, functions and the edges between them |
| **AI chat over code** | Grounded answers with `file:line` citations |
| **Impact analysis** | Reverse-traverse the graph to compute a change's blast radius |
| **Risk scoring** | 0–100 score per symbol from blast radius, co-change history, and centrality |
| **Tech debt detection** | Complexity hotspots, circular imports, dead code, god files |
| **Architecture visualization** | Interactive Cytoscape.js graph |

## Architecture

```
Repository
    │
    ▼
  Parser  ──────────────┐
 (AST + Git history)    │
    │                   │
    ▼                   ▼
Knowledge Graph      Vector DB
   (Neo4j)            (Qdrant)
    │                   │
    └────────┬──────────┘
             ▼
        AI Engine
   (hybrid graph + RAG retrieval)
             │
             ▼
       FastAPI REST
             │
             ▼
      Next.js Dashboard
```

Metadata, jobs, and chat history live in **PostgreSQL**.

The distinguishing design choice is **hybrid retrieval**: vector search finds semantically relevant code, then the knowledge graph expands that set with structurally related symbols (callers, callees, importers). Pure RAG over code misses structural relationships; pure graph traversal misses intent. See [`docs/architecture/rag-design.md`](docs/architecture/rag-design.md).

## Tech stack

**Frontend:** Next.js / React · Cytoscape.js
**Backend:** FastAPI (Python 3.11) · SQLAlchemy · Alembic
**AI:** provider-agnostic LLM interface · LangChain / LlamaIndex
**Data:** PostgreSQL · Neo4j · Qdrant
**Ops:** Docker Compose · GitHub Actions

## Repository layout

```
ARGUS/
├── backend/          FastAPI service
│   ├── app/
│   │   ├── api/      Route handlers
│   │   ├── core/     Config, logging, dependencies
│   │   ├── models/   SQLAlchemy + Pydantic models
│   │   └── services/ Graph, RAG, impact, debt engines
│   └── tests/
├── frontend/         Next.js dashboard
├── parser/           Language-agnostic code parser (Python first)
├── database/         Migrations and seed data
├── docs/
│   ├── architecture/ Design docs and diagrams
│   ├── api/          API reference
│   └── planning/     Timeline and delivery plan
├── infra/            Deployment config
└── scripts/          Dev utilities
```

## Getting started

Requires Docker Desktop, Python 3.11+, and Node 22+. Full instructions and troubleshooting in [`docs/SETUP.md`](docs/SETUP.md).

```bash
git clone https://github.com/DeepuChandru/ARGUS-AI-Engineering-Intelligence.git
cd ARGUS
cp .env.example .env
docker compose up -d
```

That brings up PostgreSQL (5432), Neo4j (7687 Bolt, 7474 browser), and Qdrant (6333). The backend and frontend land in Week 1.

## Development plan

The full week-by-week and day-by-day delivery plan is in [`docs/planning/TIMELINE.md`](docs/planning/TIMELINE.md).

## Future scope

Multi-repository analysis · GitHub App integration · multi-agent review · CI/CD recommendations · enterprise deployment.

## License

MIT
