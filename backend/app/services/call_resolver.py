"""Resolves call sites and base classes to the symbols they name.

The parser records `foo()` as the text `foo` and `class X(Base)` as the text
`Base`, because resolving either needs the whole repository's symbol table. This
module builds that table and applies it.

Resolution is never complete, and the design leans into that rather than hiding
it. Every edge records *how* it was resolved and carries a confidence; a call
site that cannot be resolved honestly is counted on the calling function as
`unresolved_calls` instead of being pointed at a plausible guess. Week 4's risk
score reads confidence as an edge weight, so a graph that admits its uncertainty
produces better numbers than one that invents edges.

Nothing here talks to Neo4j — same split as `import_resolver`.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from app.services.import_resolver import ResolvedImport, bindings_for_file, build_module_index
from parser import ParsedFile, ParsedRepo, Symbol, SymbolKind

logger = logging.getLogger(__name__)

# How far to follow base classes when looking for an inherited method. Deep
# hierarchies are rare and a malformed one can be cyclic, so the walk is bounded
# rather than trusted.
MAX_MRO_DEPTH = 10

# How many re-export hops to follow — `__init__.py` importing from a private
# module that imports from another. Two is enough for every real package layout
# and keeps a circular re-export from looping.
MAX_REEXPORT_HOPS = 2

# Names that refer to the enclosing class from inside one of its methods.
_SELF_NAMES = frozenset({"self", "cls"})


class CallResolution(StrEnum):
    """How a call site found its target. Confidences come from the schema doc."""

    LOCAL = "local"
    IMPORTED = "imported"
    ATTRIBUTE_MODULE = "attribute_module"
    ATTRIBUTE_SELF = "attribute_self"


CONFIDENCE: dict[CallResolution, float] = {
    CallResolution.LOCAL: 1.0,
    CallResolution.IMPORTED: 0.95,
    CallResolution.ATTRIBUTE_MODULE: 0.90,
    CallResolution.ATTRIBUTE_SELF: 0.85,
}


class UnresolvedReason(StrEnum):
    """Why a call site produced no edge. Counted and logged, never guessed at."""

    # The name matches several symbols with nothing to choose between them.
    # An edge to each would inflate every downstream blast radius with paths
    # that do not exist.
    AMBIGUOUS = "ambiguous"
    # Dynamic dispatch, an attribute on a value ARGUS cannot type, or a call
    # into an external package.
    UNRESOLVED = "unresolved"
    # A call at module scope has no enclosing :Function to be the edge's source.
    MODULE_LEVEL = "module_level"


@dataclass(frozen=True, slots=True)
class SymbolRef:
    """Enough to build a symbol's node key without carrying the whole Symbol.

    `label` rides along because a callee is not always a `:Function`:
    `Settings()` is a constructor call, and the writer has to MATCH the right
    label or the edge silently goes missing.
    """

    path: str
    module: str | None
    qualname: str
    label: str = "Function"


@dataclass(frozen=True, slots=True)
class ResolvedCall:
    """One resolved call site, before aggregation into a `CALLS` edge."""

    caller: SymbolRef
    callee: SymbolRef
    resolution: CallResolution
    line: int

    @property
    def confidence(self) -> float:
        return CONFIDENCE[self.resolution]


@dataclass(frozen=True, slots=True)
class CallEdge:
    """One `CALLS` edge: aggregated per caller/callee pair, not per call site."""

    caller: SymbolRef
    callee: SymbolRef
    resolution: CallResolution
    lines: tuple[int, ...]

    @property
    def count(self) -> int:
        return len(self.lines)

    @property
    def confidence(self) -> float:
        return CONFIDENCE[self.resolution]


@dataclass(frozen=True, slots=True)
class ResolvedBase:
    """One `INHERITS` edge. `position` preserves MRO order."""

    subclass: SymbolRef
    position: int
    base: SymbolRef | None = None
    external_name: str | None = None

    @property
    def resolution(self) -> str:
        return "internal" if self.base is not None else "external"


@dataclass(slots=True)
class CallGraph:
    """The whole Day 4 result, plus the counts that make its gaps visible."""

    edges: list[CallEdge] = field(default_factory=list)
    bases: list[ResolvedBase] = field(default_factory=list)
    # (path, qualname) -> number of call sites in that body that produced no
    # edge. Written onto the :Function node as `unresolved_calls`.
    unresolved_by_caller: dict[tuple[str, str], int] = field(default_factory=dict)
    reasons: Counter[str] = field(default_factory=Counter)
    resolutions: Counter[str] = field(default_factory=Counter)

    @property
    def unresolved_total(self) -> int:
        return sum(self.unresolved_by_caller.values())


def resolve_call_graph(parsed: ParsedRepo, imports: Sequence[ResolvedImport]) -> CallGraph:
    """Resolve every call site and base class in the repo."""
    index = _RepoIndex(parsed, imports)
    graph = CallGraph()

    # Inheritance first: `self.method()` is resolved against the enclosing class
    # *and its in-repo bases*, so the hierarchy has to exist before calls run.
    graph.bases = _resolve_inheritance(index)
    inherits = _internal_base_map(graph.bases)

    sites: list[ResolvedCall] = []
    for parsed_file in parsed.files:
        for call in parsed_file.calls:
            if not call.caller:
                graph.reasons[UnresolvedReason.MODULE_LEVEL] += 1
                continue

            caller = SymbolRef(parsed_file.path, parsed_file.module, call.caller)
            callee = _resolve_callee(call.callee, call.caller, parsed_file, index, inherits)
            if callee is None:
                graph.reasons[UnresolvedReason.UNRESOLVED] += 1
                _count_unresolved(graph, caller)
                continue
            if callee is _AMBIGUOUS:
                graph.reasons[UnresolvedReason.AMBIGUOUS] += 1
                _count_unresolved(graph, caller)
                continue

            target, resolution = callee
            sites.append(ResolvedCall(caller, target, resolution, call.line))
            graph.resolutions[str(resolution)] += 1

    graph.edges = _aggregate(sites)

    logger.info(
        "Resolved %d call sites into %d CALLS edges (%s); %d unresolved (%s); "
        "%d INHERITS edges",
        len(sites),
        len(graph.edges),
        dict(graph.resolutions),
        graph.unresolved_total,
        dict(graph.reasons),
        len(graph.bases),
    )
    return graph


# A distinct sentinel so "several candidates" is not confused with "no match" —
# the two are counted separately and mean different things about the code.
_AMBIGUOUS = object()


def _count_unresolved(graph: CallGraph, caller: SymbolRef) -> None:
    key = (caller.path, caller.qualname)
    graph.unresolved_by_caller[key] = graph.unresolved_by_caller.get(key, 0) + 1


def _aggregate(sites: Sequence[ResolvedCall]) -> list[CallEdge]:
    """Collapse call sites into one edge per caller/callee pair.

    A function calling another twenty times is one dependency with a weight, not
    twenty edges. Where the same pair resolved through different tiers on
    different lines the strongest wins: one confident call site is enough to
    establish that the dependency is real.
    """
    grouped: dict[tuple[SymbolRef, SymbolRef], list[ResolvedCall]] = defaultdict(list)
    for site in sites:
        grouped[(site.caller, site.callee)].append(site)

    edges: list[CallEdge] = []
    for (caller, callee), group in grouped.items():
        best = max(group, key=lambda s: s.confidence).resolution
        edges.append(
            CallEdge(
                caller=caller,
                callee=callee,
                resolution=best,
                lines=tuple(sorted({s.line for s in group})),
            )
        )
    return edges


# --- the repository-wide lookup tables ---------------------------------------


class _RepoIndex:
    """Every lookup call and base resolution need, built once per parse."""

    def __init__(self, parsed: ParsedRepo, imports: Sequence[ResolvedImport]) -> None:
        self.modules = build_module_index(parsed)
        self.files: dict[str, ParsedFile] = {f.path: f for f in parsed.files}
        self.bindings: dict[str, dict[str, ResolvedImport]] = {
            f.path: bindings_for_file(imports, f.path) for f in parsed.files
        }

        # Per file: qualname -> Symbol, and bare name -> the symbols with it.
        self.by_qualname: dict[str, dict[str, Symbol]] = {}
        self.by_name: dict[str, dict[str, list[Symbol]]] = {}
        for parsed_file in parsed.files:
            qualnames: dict[str, Symbol] = {}
            names: dict[str, list[Symbol]] = defaultdict(list)
            for symbol in parsed_file.symbols:
                qualnames[symbol.qualname] = symbol
                names[symbol.name].append(symbol)
            self.by_qualname[parsed_file.path] = qualnames
            self.by_name[parsed_file.path] = dict(names)

    def ref(self, path: str, symbol: Symbol) -> SymbolRef:
        label = "Class" if symbol.kind is SymbolKind.CLASS else "Function"
        return SymbolRef(path, symbol.module, symbol.qualname, label)

    def module_file(self, dotted: str) -> str | None:
        return self.modules.get(dotted)

    def longest_module_prefix(self, dotted: str) -> tuple[str, str] | None:
        """Split `app.core.config.get_settings` into its module and the rest.

        Tries the longest prefix first, so `import app.core.config` followed by
        `app.core.config.get_settings()` resolves even though the name bound
        into the file was only `app`.
        """
        parts = dotted.split(".")
        for cut in range(len(parts) - 1, 0, -1):
            prefix = ".".join(parts[:cut])
            if (path := self.modules.get(prefix)) is not None:
                return path, ".".join(parts[cut:])
        return None


def _pick(candidates: Sequence[Symbol]) -> Symbol | None | object:
    """Choose between same-named symbols in one file, or report ambiguity.

    A module-level definition beats a nested one: a bare call almost always
    means the top-level name, and a nested `def` of the same name is usually a
    closure that is not what the call site reached.
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    top_level = [s for s in candidates if s.parent is None]
    if len(top_level) == 1:
        return top_level[0]
    return _AMBIGUOUS


def _callable_kinds(symbol: Symbol) -> bool:
    """Classes count: `Settings()` is a constructor call, and a real dependency."""
    return symbol.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD, SymbolKind.CLASS)


# --- call-site resolution ----------------------------------------------------


def _resolve_callee(
    callee: str,
    caller_qualname: str,
    parsed_file: ParsedFile,
    index: _RepoIndex,
    inherits: dict[tuple[str, str], list[tuple[str, str]]],
):
    """Return (SymbolRef, CallResolution), None for unresolved, or _AMBIGUOUS."""
    if "." not in callee:
        return _resolve_bare(callee, parsed_file, index)

    head, tail = callee.split(".", 1)
    if head in _SELF_NAMES:
        return _resolve_self(tail, caller_qualname, parsed_file, index, inherits)
    return _resolve_attribute(callee, head, tail, parsed_file, index)


def _resolve_bare(name: str, parsed_file: ParsedFile, index: _RepoIndex):
    """`helper()` — defined in this file, or bound into it by an import."""
    chosen = _pick(
        [s for s in index.by_name[parsed_file.path].get(name, []) if _callable_kinds(s)]
    )
    if chosen is _AMBIGUOUS:
        return _AMBIGUOUS
    if chosen is not None:
        return index.ref(parsed_file.path, chosen), CallResolution.LOCAL

    binding = index.bindings[parsed_file.path].get(name)
    if binding is None or not binding.is_internal:
        return None

    # The name in the target file is what was imported, not the local alias.
    target_name = binding.imported_name or name
    return _lookup_in_file(binding.target_path, target_name, index, CallResolution.IMPORTED)


def _resolve_self(
    tail: str,
    caller_qualname: str,
    parsed_file: ParsedFile,
    index: _RepoIndex,
    inherits: dict[tuple[str, str], list[tuple[str, str]]],
):
    """`self.method()` — against the enclosing class, then its in-repo bases.

    Only a single attribute is resolvable. `self.client.get()` needs to know the
    type of `self.client`, which is real type inference and out of scope; it
    falls out as unresolved, which is exactly what the confidence model exists
    to make visible.
    """
    if "." in tail:
        return None

    class_qualname = _enclosing_class(caller_qualname, parsed_file, index)
    if class_qualname is None:
        return None

    path = parsed_file.path
    seen: set[tuple[str, str]] = set()
    frontier = [(path, class_qualname)]

    for _ in range(MAX_MRO_DEPTH):
        if not frontier:
            break
        next_frontier: list[tuple[str, str]] = []
        for owner_path, owner_qualname in frontier:
            if (owner_path, owner_qualname) in seen:
                continue
            seen.add((owner_path, owner_qualname))

            method = index.by_qualname[owner_path].get(f"{owner_qualname}.{tail}")
            if method is not None and _callable_kinds(method):
                return index.ref(owner_path, method), CallResolution.ATTRIBUTE_SELF

            next_frontier.extend(inherits.get((owner_path, owner_qualname), []))
        frontier = next_frontier

    return None


def _enclosing_class(
    caller_qualname: str, parsed_file: ParsedFile, index: _RepoIndex
) -> str | None:
    """The class owning a `self.x()` call site, walked up from the caller.

    The nearest enclosing scope is not necessarily the class — a method can
    define an inner function, and `self` inside it still refers to the method's
    class — so this walks outward until it finds one.
    """
    qualnames = index.by_qualname[parsed_file.path]
    scope = qualnames.get(caller_qualname)

    while scope is not None and scope.parent:
        parent = qualnames.get(scope.parent)
        if parent is None:
            return None
        if parent.kind is SymbolKind.CLASS:
            return parent.qualname
        scope = parent

    return None


def _resolve_attribute(
    callee: str, head: str, tail: str, parsed_file: ParsedFile, index: _RepoIndex
):
    """`mod.func()` where `mod` names a module reachable from this file."""
    # `import app.core.config` binds only `app`, so match the longest dotted
    # prefix that is a real module before falling back to the bound name.
    if (split := index.longest_module_prefix(callee)) is not None:
        target_path, remainder = split
        return _lookup_in_file(
            target_path, remainder, index, CallResolution.ATTRIBUTE_MODULE
        )

    binding = index.bindings[parsed_file.path].get(head)
    if binding is None or not binding.is_internal:
        return None
    return _lookup_in_file(binding.target_path, tail, index, CallResolution.ATTRIBUTE_MODULE)


def _lookup_in_file(
    path: str,
    name: str,
    index: _RepoIndex,
    resolution: CallResolution,
    _hops: int = 0,
):
    """Find `name` — bare or dotted qualname — among a file's symbols.

    A name the file does not define but *imports* is followed one hop further.
    This is the re-export pattern every Python package uses: `requests/__init__`
    contains `from .api import get`, so `requests.get()` names a function that
    lives in `api.py` and appears nowhere in `__init__.py`. Without this the
    resolver misses the public API of every package it looks at.
    """
    if path not in index.by_qualname:
        return None

    if (symbol := index.by_qualname[path].get(name)) is not None and _callable_kinds(symbol):
        return index.ref(path, symbol), resolution

    if "." in name:
        return None

    chosen = _pick([s for s in index.by_name[path].get(name, []) if _callable_kinds(s)])
    if chosen is _AMBIGUOUS:
        return _AMBIGUOUS
    if chosen is not None:
        return index.ref(path, chosen), resolution

    if _hops >= MAX_REEXPORT_HOPS:
        return None
    binding = index.bindings[path].get(name)
    if binding is None or not binding.is_internal or binding.target_path == path:
        return None
    return _lookup_in_file(
        binding.target_path,
        binding.imported_name or name,
        index,
        resolution,
        _hops + 1,
    )


# --- inheritance -------------------------------------------------------------


def _resolve_inheritance(index: _RepoIndex) -> list[ResolvedBase]:
    """Resolve every class's base list, keeping unresolvable bases as external.

    A base that resolves to nothing — `Protocol`, `BaseSettings`, anything from
    a dependency — still gets a node, because "everything inheriting from
    BaseSettings" is a question people actually ask.
    """
    resolved: list[ResolvedBase] = []

    for path, parsed_file in index.files.items():
        for symbol in parsed_file.symbols:
            if symbol.kind is not SymbolKind.CLASS:
                continue
            subclass = index.ref(path, symbol)
            for position, raw in enumerate(symbol.base_classes):
                base = _normalise_base(raw)
                if not base:
                    continue
                target = _resolve_base(base, path, index)
                resolved.append(
                    ResolvedBase(subclass=subclass, position=position, base=target)
                    if target is not None
                    else ResolvedBase(
                        subclass=subclass, position=position, external_name=base
                    )
                )

    return resolved


def _normalise_base(raw: str) -> str:
    """`Generic[T]` -> `Generic`. The subscript is not part of the identity."""
    return raw.split("[", 1)[0].strip()


def _resolve_base(base: str, path: str, index: _RepoIndex) -> SymbolRef | None:
    if "." in base:
        if (split := index.longest_module_prefix(base)) is not None:
            target_path, remainder = split
            return _class_in_file(target_path, remainder, index)
        head, tail = base.split(".", 1)
        binding = index.bindings[path].get(head)
        if binding is not None and binding.is_internal:
            return _class_in_file(binding.target_path, tail, index)
        return None

    if (found := _class_in_file(path, base, index)) is not None:
        return found

    binding = index.bindings[path].get(base)
    if binding is None or not binding.is_internal:
        return None
    return _class_in_file(binding.target_path, binding.imported_name or base, index)


def _class_in_file(path: str, name: str, index: _RepoIndex) -> SymbolRef | None:
    if path not in index.by_qualname:
        return None
    symbol = index.by_qualname[path].get(name)
    if symbol is None:
        candidates = [s for s in index.by_name[path].get(name, []) if s.kind is SymbolKind.CLASS]
        chosen = _pick(candidates)
        if chosen is None or chosen is _AMBIGUOUS:
            return None
        symbol = chosen
    if symbol.kind is not SymbolKind.CLASS:
        return None
    return index.ref(path, symbol)


def _internal_base_map(
    bases: Sequence[ResolvedBase],
) -> dict[tuple[str, str], list[tuple[str, str]]]:
    """`(path, class) -> [(path, base), ...]`, in MRO order, internal bases only."""
    mapping: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for entry in sorted(bases, key=lambda b: b.position):
        if entry.base is None:
            continue
        key = (entry.subclass.path, entry.subclass.qualname)
        mapping[key].append((entry.base.path, entry.base.qualname))
    return dict(mapping)
