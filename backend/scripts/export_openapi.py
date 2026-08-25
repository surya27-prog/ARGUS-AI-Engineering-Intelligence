"""Write the OpenAPI document to docs/api/openapi.json.

    python scripts/export_openapi.py

A generated file rather than a hand-maintained one, because a hand-written API
reference drifts the first time a query parameter changes and nobody notices for
a month. This is the machine-readable half; `docs/api/README.md` is the half that
explains the conventions, and it links here for the exact shapes.

Committed to the repository so the reference is readable without running
anything, and so a diff on it shows up in review when the surface changes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app  # noqa: E402

OUTPUT = Path(__file__).resolve().parents[2] / "docs" / "api" / "openapi.json"


def main() -> int:
    spec = app.openapi()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    # sort_keys so a regeneration produces a stable diff: without it a dict
    # reordering shows as a change to the whole file and hides the real one.
    OUTPUT.write_text(
        json.dumps(spec, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    operations = sum(len(methods) for methods in spec["paths"].values())
    print(f"wrote {OUTPUT.relative_to(OUTPUT.parents[2])}: "
          f"{operations} operations, {len(spec['paths'])} paths")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
