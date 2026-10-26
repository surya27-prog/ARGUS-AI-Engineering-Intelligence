"""Command-line entry point: `python -m parser.cli <source>`.

Prints the file inventory for a git URL, a zip archive, or a local directory.
The JSON mode is the same payload the API will return once ingestion is wired
up on Day 6, so it doubles as a contract check.
"""

from __future__ import annotations

import argparse
import sys

from parser import analyze_repo, parse_repo
from parser.ingest import (
    DEFAULT_MAX_SIZE_MB,
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_WORKSPACE,
    IngestError,
)
from parser.models import ParsedRepo, RepoInventory, SymbolKind


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m parser.cli",
        description="Inventory the source files in a repository.",
    )
    parser.add_argument("source", help="git URL, path to a .zip, or a local directory")
    parser.add_argument(
        "--workspace",
        default=str(DEFAULT_WORKSPACE),
        help=f"where clones and archives are staged (default: {DEFAULT_WORKSPACE})",
    )
    parser.add_argument(
        "--max-size-mb",
        type=int,
        default=DEFAULT_MAX_SIZE_MB,
        help=f"reject repositories larger than this (default: {DEFAULT_MAX_SIZE_MB})",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"clone timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-stage the repository even if it is already in the workspace",
    )
    parser.add_argument(
        "--symbols",
        action="store_true",
        help="run AST extraction and list functions, classes, imports and calls",
    )
    parser.add_argument("--json", action="store_true", help="emit the inventory as JSON")
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="max files listed in table output, 0 for all (default: 50)",
    )
    parser.add_argument(
        "--show-skipped",
        action="store_true",
        help="list skipped paths instead of just counting them",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    run = analyze_repo if args.symbols else parse_repo
    try:
        result = run(
            args.source,
            workspace_dir=args.workspace,
            max_size_mb=args.max_size_mb,
            timeout_seconds=args.timeout,
            force=args.force,
        )
    except IngestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(result.to_json())
    elif isinstance(result, ParsedRepo):
        print_table(result.inventory, limit=args.limit, show_skipped=args.show_skipped)
        print_symbols(result, limit=args.limit)
    else:
        print_table(result, limit=args.limit, show_skipped=args.show_skipped)
    return 0


def print_symbols(parsed: ParsedRepo, *, limit: int = 50) -> None:
    """Per-file symbol listing, followed by extraction totals."""
    print()
    shown = 0
    for file in parsed.files:
        if not file.ok:
            print(f"{file.path}\n  !! {file.error}")
            continue
        if not file.symbols and not file.imports:
            continue
        if limit > 0 and shown >= limit:
            break
        shown += 1

        print(file.path)
        for imp in file.imports:
            print(f"  import   {imp.target}")
        for symbol in file.symbols:
            marker = "async " if symbol.is_async else ""
            signature = ", ".join(p.name for p in symbol.parameters)
            detail = f"({signature})" if symbol.kind is not SymbolKind.CLASS else ""
            if symbol.base_classes:
                detail = f"({', '.join(symbol.base_classes)})"
            print(
                f"  {symbol.kind:<8} {marker}{symbol.qualname}{detail}"
                f"  L{symbol.line_start}-{symbol.line_end}"
            )
        calls = [c for c in file.calls if c.caller]
        if calls:
            print(f"  {len(calls)} call sites inside functions")

    hidden = len(parsed.files) - shown
    if limit > 0 and hidden > 0:
        print(f"... {hidden} more files (use --limit 0 to show all)")

    print()
    print(
        f"{parsed.symbol_count} symbols, {parsed.import_count} imports, "
        f"{parsed.call_count} call sites"
    )
    if parsed.failed_files:
        print(f"{len(parsed.failed_files)} files failed to parse")


def print_table(inventory: RepoInventory, *, limit: int = 50, show_skipped: bool = False) -> None:
    print(f"{inventory.name}  ({inventory.source_kind})")
    print(f"  root    {inventory.root}")
    if inventory.commit_sha:
        branch = inventory.default_branch or "?"
        print(f"  commit  {inventory.commit_sha[:12]} on {branch}")
    print()

    if not inventory.files:
        print("No parseable source files found.")
    else:
        shown = inventory.files if limit <= 0 else inventory.files[:limit]
        width = max(len(f.path) for f in shown)
        print(f"{'FILE'.ljust(width)}  {'LINES':>7}  {'SIZE':>9}  MODULE")
        for file in shown:
            print(
                f"{file.path.ljust(width)}  {file.line_count:>7}  "
                f"{_human_size(file.size_bytes):>9}  {file.module or '-'}"
            )
        hidden = inventory.file_count - len(shown)
        if hidden > 0:
            print(f"... {hidden} more (use --limit 0 to show all)")

    print()
    print(
        f"{inventory.file_count} files, {inventory.total_lines} lines, "
        f"{_human_size(inventory.total_bytes)}"
    )

    reasons = inventory.skipped_by_reason()
    if reasons:
        summary = ", ".join(f"{count} {reason}" for reason, count in sorted(reasons.items()))
        print(f"skipped: {summary}")
    if show_skipped:
        for entry in inventory.skipped:
            print(f"  {entry.reason:<24} {entry.path}")


def _human_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


if __name__ == "__main__":
    raise SystemExit(main())
