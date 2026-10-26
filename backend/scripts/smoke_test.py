"""End-to-end smoke test against a running ARGUS, local or deployed.

    # against a local stack
    python scripts/smoke_test.py http://localhost:8000

    # against production, ingesting a real repository
    python scripts/smoke_test.py https://argus-api.onrender.com --ingest

Committed as a script rather than performed by hand because "a stranger could use
the live URL without you present" is a claim that needs re-checking after every
deploy, and a checklist somebody works through from memory is a checklist that
drifts.

Read-only by default: it exercises what is already there and reports. `--ingest`
adds a real parse, which costs a clone and — if an embedding key is configured —
real money, so it is opt-in.

Exit code is the number of failures, so CI or a deploy hook can gate on it.
"""

from __future__ import annotations

import argparse
import time
from typing import Any

import httpx

# A parse of a small repository takes a couple of minutes; a cold free-tier
# instance plus a clone can take longer.
PARSE_TIMEOUT_S = 600
POLL_S = 5

FIXTURE_REPO = "https://github.com/psf/requests"


class Report:
    """Collects results so one failure does not stop the rest of the run.

    A smoke test that aborts on its first failure tells you one thing is broken.
    Running the remainder tells you whether it is one thing or everything, which
    is the difference between a bad deploy and a bad service.
    """

    def __init__(self) -> None:
        self.failures = 0
        self.checks = 0

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks += 1
        if not ok:
            self.failures += 1
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}" + (f" - {detail}" if detail else ""), flush=True)
        return ok

    def note(self, text: str) -> None:
        print(f"         {text}", flush=True)


def get(client: httpx.Client, path: str, **kwargs: Any) -> httpx.Response | None:
    try:
        return client.get(path, **kwargs)
    except httpx.HTTPError as exc:
        print(f"  [FAIL] GET {path} - {type(exc).__name__}: {exc}", flush=True)
        return None


def check_health(client: httpx.Client, report: Report) -> dict[str, Any]:
    print("\nHealth")
    response = get(client, "/health")
    if not report.check("/health responds", response is not None and response.status_code == 200):
        return {}

    body = response.json()
    # /health deliberately returns 200 even when a store is down, so the status
    # code proves the process is serving and the body is what says more.
    for store in ("postgres", "neo4j", "qdrant"):
        state = body.get(store, "missing")
        report.check(f"{store} reachable", state == "up", state)

    report.check(
        "a request id is returned",
        bool(response.headers.get("x-request-id")),
        response.headers.get("x-request-id", "absent"),
    )

    for flag in ("llm_configured", "embedding_configured"):
        if not body.get(flag):
            report.note(f"{flag} is false - the features needing it will return 503")
    return body


def check_errors(client: httpx.Client, report: Report) -> None:
    """The failure paths, which are the ones nobody exercises by accident."""
    print("\nError handling")

    response = get(client, "/repos/00000000-0000-0000-0000-000000000000")
    report.check(
        "unknown repository is a 404, not a 500",
        response is not None and response.status_code == 404,
        f"got {response.status_code if response else 'no response'}",
    )

    try:
        bad = client.post("/repos", json={"url": "http://169.254.169.254/latest/meta-data/"})
        report.check(
            "a link-local URL is refused",
            bad.status_code == 422,
            f"got {bad.status_code}",
        )
    except httpx.HTTPError as exc:
        report.check("a link-local URL is refused", False, str(exc))

    try:
        creds = client.post("/repos", json={"url": "https://u:tok@github.com/o/r.git"})
        report.check(
            "a URL with credentials is refused",
            creds.status_code == 422,
            f"got {creds.status_code}",
        )
    except httpx.HTTPError as exc:
        report.check("a URL with credentials is refused", False, str(exc))

    response = get(client, "/repos", params={"limit": 99999})
    report.check(
        "an out-of-range limit is a 422, not a huge response",
        response is not None and response.status_code == 422,
        f"got {response.status_code if response else 'no response'}",
    )


def check_reads(client: httpx.Client, report: Report, repo_id: str) -> None:
    print("\nRead endpoints")
    for name, path, params in [
        ("files", f"/repos/{repo_id}/files", {"limit": 5}),
        ("symbols", f"/repos/{repo_id}/symbols", {"limit": 5}),
        ("graph", f"/repos/{repo_id}/graph", {"view": "calls", "limit": 50}),
        ("risk", f"/repos/{repo_id}/risk", {"limit": 5}),
        ("debt", f"/repos/{repo_id}/debt", {"limit": 5}),
        ("cochange", f"/repos/{repo_id}/cochange", {"limit": 5}),
    ]:
        started = time.perf_counter()
        response = get(client, path, params=params)
        elapsed = (time.perf_counter() - started) * 1000
        ok = response is not None and response.status_code == 200
        detail = f"{elapsed:.0f}ms" if ok else str(response.status_code if response else "-")
        report.check(f"/{name}", ok, detail)

        # An endpoint that answers 200 with nothing in it is the failure mode a
        # status-code-only check misses entirely.
        if ok and name in ("files", "symbols"):
            total = response.json().get("total", 0)
            report.check(f"/{name} is not empty", total > 0, f"total={total}")


def ingest_and_wait(client: httpx.Client, report: Report, url: str) -> str | None:
    print(f"\nIngesting {url}")
    try:
        created = client.post("/repos", json={"url": url})
    except httpx.HTTPError as exc:
        report.check("POST /repos", False, str(exc))
        return None

    accepted = report.check(
        "POST /repos accepted", created.status_code == 202, f"got {created.status_code}"
    )
    if not accepted:
        report.note(created.text[:200])
        return None

    repo_id = created.json()["id"]
    report.note(f"repository {repo_id}")

    deadline = time.time() + PARSE_TIMEOUT_S
    status = "pending"
    while time.time() < deadline:
        response = get(client, f"/repos/{repo_id}")
        if response is None or response.status_code != 200:
            break
        body = response.json()
        if body["status"] != status:
            status = body["status"]
            report.note(f"status -> {status}")
        if status in ("complete", "failed"):
            break
        time.sleep(POLL_S)

    if not report.check("parse completed", status == "complete", status):
        response = get(client, f"/repos/{repo_id}")
        if response is not None:
            report.note(f"error: {response.json().get('error_message')}")
        return None

    body = get(client, f"/repos/{repo_id}").json()
    report.check("files were found", body["file_count"] > 0, f"{body['file_count']} files")
    report.check("symbols were found", body["symbol_count"] > 0, f"{body['symbol_count']} symbols")
    return repo_id


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("base_url", help="e.g. http://localhost:8000")
    ap.add_argument(
        "--ingest",
        action="store_true",
        help="also parse a repository — costs a clone, and embeddings if configured",
    )
    ap.add_argument("--repo", default=FIXTURE_REPO, help=f"what to ingest (default {FIXTURE_REPO})")
    args = ap.parse_args()

    report = Report()
    print(f"ARGUS smoke test against {args.base_url}")

    with httpx.Client(
        base_url=args.base_url.rstrip("/"), timeout=60.0, follow_redirects=True
    ) as client:
        health = check_health(client, report)
        if not health:
            print("\nThe API did not respond. Nothing else can be checked.")
            return max(1, report.failures)

        check_errors(client, report)

        repo_id: str | None = None
        if args.ingest:
            repo_id = ingest_and_wait(client, report, args.repo)
        else:
            listing = get(client, "/repos", params={"limit": 50})
            if listing is not None and listing.status_code == 200:
                complete = [r for r in listing.json()["items"] if r["status"] == "complete"]
                if complete:
                    repo_id = complete[0]["id"]
                    report.note(f"reusing {complete[0]['name']} ({repo_id})")
                else:
                    report.note("no parsed repository to read from; pass --ingest")

        if repo_id:
            check_reads(client, report, repo_id)

    print(f"\n{report.checks - report.failures}/{report.checks} checks passed")
    if report.failures:
        print(f"{report.failures} FAILED")
    return report.failures


if __name__ == "__main__":
    raise SystemExit(main())
