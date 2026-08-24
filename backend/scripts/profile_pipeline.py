"""Measure where a parse spends its time, and how slow each endpoint is.

Run this to produce the table in `docs/performance.md`. It exists as a committed
script rather than a one-off because a performance number with no way to
reproduce it is a number nobody can check next month.

    cd backend
    LLM_PROVIDER=stub EMBEDDING_PROVIDER=hash \
      .venv/Scripts/python.exe scripts/profile_pipeline.py https://github.com/psf/requests.git

Needs the compose stack up. Add `--skip-parse` to time only the endpoints
against whatever is already ingested.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID

# Run from `backend/` without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.core.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import ParseJob, ParseStatus, Repository  # noqa: E402
from app.services.parsing import parse_repository  # noqa: E402

# Each endpoint is called this many times and the median reported. A single call
# measures the cold cache and the connection handshake as much as the query.
REPEATS = 5

# The stages, in pipeline order, so the table reads the way the parse runs.
STAGE_ORDER = ("analyze", "store", "graph", "cochange", "vectors")


def parse(source: str) -> tuple[UUID, dict[str, Any]]:
    """Ingest `source` from scratch and return its per-stage timings."""
    from sqlalchemy import select

    with SessionLocal() as db:
        repo = db.scalars(select(Repository).where(Repository.url == source)).first()
        if repo is None:
            repo = Repository(name=source.rstrip("/").split("/")[-1], url=source)
            db.add(repo)
            db.commit()
            db.refresh(repo)
        repo_id = repo.id

    started = time.perf_counter()
    parse_repository(repo_id, source)
    wall = time.perf_counter() - started

    with SessionLocal() as db:
        repo = db.get(Repository, repo_id)
        job = db.scalars(
            select(ParseJob)
            .where(ParseJob.repository_id == repo_id)
            .order_by(ParseJob.started_at.desc())
            .limit(1)
        ).first()
        if repo.status != ParseStatus.COMPLETE:
            sys.exit(f"parse did not complete: {repo.error_message}")
        return repo_id, {
            "wall_s": wall,
            "files": repo.file_count,
            "symbols": repo.symbol_count,
            "stage_ms": dict(job.stage_ms or {}),
            "duration_ms": job.duration_ms,
            "chunks": job.chunk_count,
            "graph_nodes": job.graph_nodes,
            "graph_rels": job.graph_relationships,
        }


def time_endpoints(repo_id: UUID) -> list[tuple[str, float, float, int]]:
    """Median and worst milliseconds per endpoint, plus the status code."""
    client = TestClient(app)
    calls = [
        ("GET /repos/{id}", f"/repos/{repo_id}"),
        ("GET /files (200)", f"/repos/{repo_id}/files?limit=200"),
        ("GET /symbols (200)", f"/repos/{repo_id}/symbols?limit=200"),
        ("GET /graph (files, 400)", f"/repos/{repo_id}/graph?view=files&limit=400"),
        ("GET /graph (calls, 400)", f"/repos/{repo_id}/graph?view=calls&limit=400"),
        ("GET /risk (File, 120)", f"/repos/{repo_id}/risk?type=File&limit=120"),
        ("GET /risk (Function, 10)", f"/repos/{repo_id}/risk?type=Function&limit=10"),
        ("GET /debt", f"/repos/{repo_id}/debt?limit=1"),
        ("GET /cochange", f"/repos/{repo_id}/cochange?limit=100"),
        ("GET /conversations", f"/repos/{repo_id}/conversations"),
    ]

    rows = []
    for label, url in calls:
        samples = []
        status = 0
        for _ in range(REPEATS):
            begin = time.perf_counter()
            response = client.get(url)
            samples.append((time.perf_counter() - begin) * 1000)
            status = response.status_code
        rows.append((label, statistics.median(samples), max(samples), status))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", nargs="?", default="https://github.com/psf/requests.git")
    ap.add_argument("--skip-parse", action="store_true", help="time endpoints only")
    args = ap.parse_args()

    from sqlalchemy import select

    if args.skip_parse:
        with SessionLocal() as db:
            repo = db.scalars(
                select(Repository).where(Repository.status == ParseStatus.COMPLETE).limit(1)
            ).first()
            if repo is None:
                return int(bool(sys.stderr.write("no completed repository to profile\n")))
            repo_id, stats = repo.id, None
    else:
        repo_id, stats = parse(args.source)

    if stats:
        print(f"\n## Parse — {stats['files']} files, {stats['symbols']} symbols\n")
        print(f"Wall clock: **{stats['wall_s']:.1f}s**")
        print(f"({stats['graph_nodes']} graph nodes, {stats['graph_rels']} "
              f"relationships, {stats['chunks']} chunks)\n")
        print("| Stage | ms | % of parse |")
        print("|---|---:|---:|")
        total = sum(stats["stage_ms"].values()) or 1
        for stage in STAGE_ORDER:
            ms = stats["stage_ms"].get(stage)
            if ms is None:
                print(f"| {stage} | — | skipped |")
                continue
            print(f"| {stage} | {ms:,} | {100 * ms / total:.0f}% |")
        print(f"| **total** | **{total:,}** | |")

    print("\n## Endpoints\n")
    print(f"Median of {REPEATS} calls, in-process via TestClient (no HTTP overhead).\n")
    print("| Endpoint | median ms | worst ms | status |")
    print("|---|---:|---:|---:|")
    for label, median, worst, status in time_endpoints(repo_id):
        flag = "" if status == 200 else f" ⚠️ {status}"
        print(f"| `{label}` | {median:.0f} | {worst:.0f} | {status}{flag} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
