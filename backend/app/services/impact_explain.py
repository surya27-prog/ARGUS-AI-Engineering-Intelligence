"""Turn a blast radius into prose: "changing this breaks X because Y".

The ranked list from `impact` is already correct and already defensible — every
row carries its route and its confidence. What it is not is *readable*. Forty
rows of `sym:…:Session.request` do not tell a reviewer whether to be worried,
and the one thing a reviewer wants is exactly the thing a ranked table cannot
say: which of these actually break, and why.

Two decisions shape this module:

**The model is given signatures, not just names.** A list of qualnames produces
prose that restates the list. The "because Y" has to come from somewhere, so the
context carries each involved symbol's file, line, signature and docstring,
pulled from Postgres — the graph deliberately stores none of that. What the model
adds is the reading of the routes, not knowledge of the code.

**An empty radius never reaches the model.** Nothing depending on a symbol is a
fact, not a question, and asking a model to narrate it invites invented risk.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models import Symbol
from app.services.impact import DEFAULT_IMPACT_DEPTH, ImpactResult, impact
from app.services.providers import ChatMessage, ChatProvider, get_chat_provider

logger = logging.getLogger(__name__)

# How many affected nodes are described to the model. The list is score-ordered,
# so this is the top of the radius; past roughly this many the prompt stops
# being about a change and starts being a directory listing.
EXPLAINED_NODES = 25

# Explanations are short by design — this is a panel beside a graph, not a
# report. The cap is a ceiling, not a target.
MAX_TOKENS = 900

# Per-process, keyed on the repository's commit. Explanations are deterministic
# given the graph, and the graph only moves on re-parse, so a hit is safe until
# the commit changes. Not shared between workers and lost on restart — worth
# noting rather than fixing, since the alternative is a table and a migration.
_CACHE_SIZE = 64
_cache: OrderedDict[tuple[str, str, int, str | None], str] = OrderedDict()

SYSTEM_PROMPT = """You are ARGUS, explaining the consequences of changing one \
symbol in a specific codebase.

You are given the symbol being changed, and the nodes that reach it, each with \
the route the change would travel along, the confidence of each link, and the \
signatures and docstrings involved. That is your only evidence. Do not describe \
behaviour you cannot see in it, and do not assume a library's public behaviour \
from its name — this is the user's code.

Lead with one sentence a reviewer could act on: is this change contained, or \
does it reach far? Then explain the specific breakages, most serious first. For \
each one, name the caller with its `path:line` and say what about the route \
makes it vulnerable — a direct call passing arguments through is a different \
risk from a three-hop chain that only imports a module.

Distinguish what will break from what might. Links resolved by name are \
certain; a low-confidence link is a guess ARGUS made, and saying "probably" \
about one is more useful than stating it flatly. If most of the radius is \
low-confidence or reached only through imports, say the blast radius is weaker \
than its size suggests.

Do not restate the ranked list. The user can already see it. Say what it means.

Be concise: a short paragraph, then at most five bullets. No preamble, no \
summary of your own instructions."""


@dataclass(frozen=True, slots=True)
class ImpactExplanation:
    """Prose plus the numbers it was derived from, so it can be checked."""

    key: str
    display: str
    text: str
    model: str
    affected: int
    explained: int
    depth: int
    truncated: bool
    cached: bool
    input_tokens: int = 0
    output_tokens: int = 0
    refused: bool = False


def explain_impact(
    db: Session,
    repository_id: UUID | str,
    key: str,
    *,
    depth: int = DEFAULT_IMPACT_DEPTH,
    commit_sha: str | None = None,
    provider: ChatProvider | None = None,
    result: ImpactResult | None = None,
) -> ImpactExplanation:
    """Explain the blast radius of `key` in prose. Raises `NodeNotFound`.

    `result` lets a caller that already computed the radius avoid a second
    traversal; omit it and the radius is computed here.
    """
    provider = provider or get_chat_provider()
    repo_id = str(repository_id)
    radius = result or impact(repo_id, key, depth=depth)
    root_display = radius.root.get("display") or key

    if not radius.items:
        # Deterministic, and true: no model call to make something up with.
        return ImpactExplanation(
            key=key,
            display=root_display,
            text=(
                f"Nothing in this repository reaches `{root_display}` within {depth} "
                "hops, so a change to it is contained. Callers outside the "
                "repository, dynamic dispatch and calls ARGUS could not resolve are "
                "not covered by that."
            ),
            model="",
            affected=0,
            explained=0,
            depth=depth,
            truncated=False,
            cached=False,
        )

    cache_key = (repo_id, key, depth, commit_sha)
    if (hit := _cache.get(cache_key)) is not None:
        _cache.move_to_end(cache_key)
        return ImpactExplanation(
            key=key,
            display=root_display,
            text=hit,
            model=provider.model,
            affected=len(radius.items),
            explained=min(len(radius.items), EXPLAINED_NODES),
            depth=depth,
            truncated=radius.truncated,
            cached=True,
        )

    top = radius.items[:EXPLAINED_NODES]
    details = _symbol_details(db, repo_id, [radius.root, *top])
    prompt = _build_prompt(radius, top, details)

    response = provider.complete(
        [ChatMessage(role="user", content=prompt)],
        system=SYSTEM_PROMPT,
        max_tokens=MAX_TOKENS,
    )

    if response.text and not response.refused:
        _cache[cache_key] = response.text
        _cache.move_to_end(cache_key)
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)

    logger.info(
        "Explained impact of %s: %d affected, %d described, %d output tokens",
        key,
        len(radius.items),
        len(top),
        response.output_tokens,
    )

    return ImpactExplanation(
        key=key,
        display=root_display,
        text=response.text
        or (
            "The model returned no explanation for this change"
            + (" (the request was refused)." if response.refused else ".")
        ),
        model=response.model,
        affected=len(radius.items),
        explained=len(top),
        depth=depth,
        truncated=radius.truncated,
        cached=False,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        refused=response.refused,
    )


def _symbol_details(
    db: Session, repo_id: str, nodes: list[dict[str, Any]]
) -> dict[tuple[str | None, str], Symbol]:
    """Signatures and docstrings for the symbols in a radius, from Postgres.

    Keyed on `(module, qualname)` because that is what a graph symbol key is made
    of, and one qualname can exist in several modules.
    """
    qualnames = {n["qualname"] for n in nodes if n.get("qualname")}
    if not qualnames:
        return {}

    rows = db.scalars(
        select(Symbol)
        .options(joinedload(Symbol.file))
        .where(Symbol.repository_id == repo_id, Symbol.qualname.in_(qualnames))
    ).all()
    # Later rows would otherwise silently win; the first match for a
    # (module, qualname) pair is as good as any and keeps this deterministic.
    details: dict[tuple[str | None, str], Symbol] = {}
    for row in rows:
        details.setdefault((row.module, row.qualname), row)
    return details


def _signature(symbol: Symbol) -> str:
    params = ", ".join(
        p.get("name", "") + (f": {p['annotation']}" if p.get("annotation") else "")
        for p in symbol.parameters or []
    )
    returns = f" -> {symbol.returns}" if symbol.returns else ""
    prefix = "async " if symbol.is_async else ""
    if symbol.kind == "class":
        bases = ", ".join(symbol.base_classes or [])
        return f"class {symbol.qualname}({bases})" if bases else f"class {symbol.qualname}"
    return f"{prefix}def {symbol.qualname}({params}){returns}"


def _describe(
    node: dict[str, Any], details: dict[tuple[str | None, str], Symbol]
) -> str:
    """One node as a line the model can cite, with whatever detail exists."""
    qualname = node.get("qualname")
    symbol = details.get((node.get("module"), qualname)) if qualname else None

    if symbol is not None:
        path = symbol.file.path if symbol.file else node.get("path")
        location = f"{path}:{symbol.line_start}" if path else node.get("module") or "?"
        line = f"{location} — {_signature(symbol)}"
        if symbol.docstring:
            # First line only: a full docstring for 25 nodes crowds out the
            # routes, which are the part the model cannot get anywhere else.
            summary = symbol.docstring.strip().splitlines()[0]
            line += f"\n    doc: {summary}"
        return line

    # Files and external modules have no symbol row, and need none.
    if node.get("path"):
        return f"{node['path']} — {node.get('type', 'node')}"
    return f"{node.get('display', node.get('key', '?'))} — {node.get('type', 'node')}"


def _build_prompt(
    radius: ImpactResult,
    top: list[dict[str, Any]],
    details: dict[tuple[str | None, str], Symbol],
) -> str:
    summary = radius.summary
    lines = [
        "The symbol being changed:",
        f"  {_describe(radius.root, details)}",
        "",
        (
            f"{summary['total']} node(s) reach it within {summary['depth']} hop(s): "
            f"{summary['direct']} directly"
            + (", and the radius hit the result cap" if radius.truncated else "")
            + "."
        ),
        f"Affected by type: {_counts(summary['by_type'])}.",
        f"Affected by distance: {_counts(summary['by_hop'], suffix=' hop')}.",
        "",
        f"The {len(top)} highest-scoring, each with the route the change travels:",
    ]

    for index, item in enumerate(top, start=1):
        route = " -> ".join(_short(k) for k in item["route"])
        via = " then ".join(item["via"])
        lines.append(
            f"\n[{index}] hop {item['hops']}, confidence {item['confidence']:.2f}, "
            f"score {item['score']:.2f}, via {via}"
        )
        lines.append(f"    {_describe(item, details)}")
        lines.append(f"    route: {route}")

    lines.append(
        "\nExplain what a change to the symbol above would break, and why, "
        "using only the evidence here."
    )
    return "\n".join(lines)


def _counts(counts: dict[str, Any], *, suffix: str = "") -> str:
    if not counts:
        return "none"
    return ", ".join(f"{value} at {label}{suffix}" for label, value in counts.items())


def _short(key: str) -> str:
    """`sym:<uuid>:app.core.config:get_settings` -> `get_settings`.

    Graph keys carry a repository UUID that means nothing to the model and costs
    tokens on every route line.
    """
    return key.rsplit(":", 1)[-1] or key


__all__ = ["EXPLAINED_NODES", "ImpactExplanation", "explain_impact"]
