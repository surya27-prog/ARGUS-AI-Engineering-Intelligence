"""Python AST extraction: source text in, `ParsedFile` out.

The extractor records what the source *says*, never what it means. `from . import
config` stays relative and `helper()` stays a bare name — resolving either needs
the whole repo's symbol table, which is Week 2's job. Keeping resolution out of
here means a re-resolve never requires re-reading the files.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

from parser.models import (
    CallRef,
    ImportRef,
    Language,
    Parameter,
    ParameterKind,
    ParsedFile,
    Symbol,
    SymbolKind,
)

_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)

# Every construct that adds one independent path through a body. `elif` needs no
# entry of its own: the grammar nests it as another `If`. `try`/`with`/`else` add
# none — an `else` is the path its `if` already accounted for.
_BRANCH_NODES = (
    ast.If,
    ast.IfExp,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.ExceptHandler,
    ast.Assert,
    ast.comprehension,
    ast.match_case,
)


def extract_file(
    path: Path | str,
    *,
    repo_root: Path | str | None = None,
    module: str | None = None,
) -> ParsedFile:
    """Extract symbols from a file on disk.

    `repo_root` makes the recorded path repo-relative and POSIX-separated, which
    is what every downstream store keys on.
    """
    path = Path(path)
    relative = path.as_posix()
    if repo_root is not None:
        try:
            relative = path.resolve().relative_to(Path(repo_root).resolve()).as_posix()
        except ValueError:
            pass

    try:
        # ast.parse takes bytes and honours PEP 263 coding declarations, so
        # files that are not UTF-8 still parse instead of raising.
        source = path.read_bytes()
    except OSError as exc:
        return ParsedFile(path=relative, module=module, error=f"{type(exc).__name__}: {exc}")

    return extract_source(source, path=relative, module=module)


def extract_source(
    source: str | bytes,
    *,
    path: str = "<unknown>",
    module: str | None = None,
) -> ParsedFile:
    """Extract symbols from source text already in memory."""
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        # Syntax errors are data, not failures: a repo with one unparseable
        # file should still produce a graph of the other files.
        location = f" (line {exc.lineno})" if exc.lineno else ""
        return ParsedFile(path=path, module=module, error=f"SyntaxError: {exc.msg}{location}")
    except ValueError as exc:  # embedded nulls, absurd nesting depth
        return ParsedFile(path=path, module=module, error=f"{type(exc).__name__}: {exc}")

    extractor = _Extractor(path=path, module=module)
    extractor.visit_module(tree)

    return ParsedFile(
        path=path,
        module=module,
        language=Language.PYTHON,
        symbols=tuple(extractor.symbols),
        imports=tuple(extractor.imports),
        calls=tuple(extractor.calls),
        docstring=ast.get_docstring(tree),
    )


class _Extractor:
    """Walks a module, tracking the scope stack so qualnames stay accurate."""

    def __init__(self, *, path: str, module: str | None) -> None:
        self.path = path
        self.module = module
        self.symbols: list[Symbol] = []
        self.imports: list[ImportRef] = []
        self.calls: list[CallRef] = []
        # Enclosing definitions, outermost first, e.g. ["Repository", "save"].
        self._scope: list[str] = []
        # Nearest enclosing function/method — the caller of any call site.
        self._current_function: str | None = None
        # Whether the immediate parent scope is a class, which is what makes a
        # function a method.
        self._in_class = False

    def visit_module(self, tree: ast.Module) -> None:
        for node in tree.body:
            self._visit(node)

    def _visit(self, node: ast.AST) -> None:
        if isinstance(node, ast.ClassDef):
            self._visit_class(node)
        elif isinstance(node, _FUNCTION_NODES):
            self._visit_function(node)
        elif isinstance(node, ast.Import):
            self._visit_import(node)
        elif isinstance(node, ast.ImportFrom):
            self._visit_import_from(node)
        else:
            self._visit_generic(node)

    def _visit_generic(self, node: ast.AST) -> None:
        """Recurse into statements that do not open a new scope."""
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Call):
                self._record_call(child)
            self._visit(child)

    def _qualname(self, name: str) -> str:
        return ".".join([*self._scope, name])

    def _visit_class(self, node: ast.ClassDef) -> None:
        qualname = self._qualname(node.name)
        self.symbols.append(
            Symbol(
                name=node.name,
                qualname=qualname,
                kind=SymbolKind.CLASS,
                file_path=self.path,
                line_start=node.lineno,
                line_end=node.end_lineno or node.lineno,
                module=self.module,
                parent=".".join(self._scope) or None,
                docstring=ast.get_docstring(node),
                decorators=self._decorators(node),
                base_classes=tuple(
                    text for base in node.bases if (text := _expression_text(base))
                ),
                complexity=cyclomatic_complexity(node),
            )
        )

        # Decorator and base-class expressions live in the enclosing scope.
        for expr in (*node.decorator_list, *node.bases, *(kw.value for kw in node.keywords)):
            self._record_calls_in(expr)

        was_in_class, self._in_class = self._in_class, True
        self._scope.append(node.name)
        for child in node.body:
            self._visit(child)
        self._scope.pop()
        self._in_class = was_in_class

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        qualname = self._qualname(node.name)
        self.symbols.append(
            Symbol(
                name=node.name,
                qualname=qualname,
                kind=SymbolKind.METHOD if self._in_class else SymbolKind.FUNCTION,
                file_path=self.path,
                line_start=node.lineno,
                line_end=node.end_lineno or node.lineno,
                module=self.module,
                parent=".".join(self._scope) or None,
                docstring=ast.get_docstring(node),
                decorators=self._decorators(node),
                parameters=_parameters(node.args),
                returns=_expression_text(node.returns),
                is_async=isinstance(node, ast.AsyncFunctionDef),
                complexity=cyclomatic_complexity(node),
            )
        )

        # Decorators and defaults are evaluated by the *enclosing* scope, so
        # `@app.get(...)` is attributed to the module, not to the handler.
        for expr in (*node.decorator_list, *node.args.defaults):
            self._record_calls_in(expr)

        outer_function, self._current_function = self._current_function, qualname
        was_in_class, self._in_class = self._in_class, False
        self._scope.append(node.name)
        for child in node.body:
            self._visit(child)
        self._scope.pop()
        self._in_class = was_in_class
        self._current_function = outer_function

    def _visit_import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append(
                ImportRef(module=alias.name, alias=alias.asname, line=node.lineno)
            )

    def _visit_import_from(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            self.imports.append(
                ImportRef(
                    module=node.module,
                    name=alias.name,
                    alias=alias.asname,
                    line=node.lineno,
                    level=node.level or 0,
                    is_from=True,
                )
            )

    def _decorators(
        self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
    ) -> tuple[str, ...]:
        return tuple(text for d in node.decorator_list if (text := _expression_text(d)))

    def _record_calls_in(self, node: ast.AST | None) -> None:
        """Record every call inside an expression evaluated in the current scope."""
        if node is None:
            return
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                self._record_call(child)

    def _record_call(self, node: ast.Call) -> None:
        callee = _callee_name(node.func)
        if callee is None:
            # e.g. `handlers[0]()` or `(lambda: 1)()` — nothing nameable to
            # resolve later, so recording it would only add noise.
            return
        self.calls.append(
            CallRef(
                callee=callee,
                line=node.lineno,
                caller=self._current_function,
                file_path=self.path,
                module=self.module,
                arg_count=len(node.args) + len(node.keywords),
            )
        )


def _parameters(args: ast.arguments) -> tuple[Parameter, ...]:
    """Flatten an `ast.arguments` into ordered `Parameter`s with their defaults."""
    params: list[Parameter] = []

    positional = [*args.posonlyargs, *args.args]
    # Defaults align to the *end* of the positional list.
    padding = len(positional) - len(args.defaults)
    for index, arg in enumerate(positional):
        default = args.defaults[index - padding] if index >= padding else None
        params.append(
            Parameter(
                name=arg.arg,
                kind=(
                    ParameterKind.POSITIONAL_ONLY
                    if index < len(args.posonlyargs)
                    else ParameterKind.POSITIONAL_OR_KEYWORD
                ),
                annotation=_expression_text(arg.annotation),
                default=_expression_text(default),
            )
        )

    if args.vararg:
        params.append(
            Parameter(
                name=args.vararg.arg,
                kind=ParameterKind.VAR_POSITIONAL,
                annotation=_expression_text(args.vararg.annotation),
            )
        )

    for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        params.append(
            Parameter(
                name=arg.arg,
                kind=ParameterKind.KEYWORD_ONLY,
                annotation=_expression_text(arg.annotation),
                default=_expression_text(default),
            )
        )

    if args.kwarg:
        params.append(
            Parameter(
                name=args.kwarg.arg,
                kind=ParameterKind.VAR_KEYWORD,
                annotation=_expression_text(args.kwarg.annotation),
            )
        )

    return tuple(params)


def _expression_text(node: ast.AST | None) -> str | None:
    """Source text for an annotation, default or base class."""
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001 - unparse is best-effort metadata
        return None


def cyclomatic_complexity(node: ast.AST) -> int:
    """McCabe complexity of a definition's own body.

    Public because Week 5's debt detectors are specified in terms of it, and a
    metric nobody can recompute is a metric nobody will trust.
    """
    complexity = 1
    for child in _own_nodes(node):
        if isinstance(child, ast.comprehension):
            # The clause itself, plus each filter: `[x for x in xs if x]` has two
            # decision points, and scoring it the same as an unfiltered
            # comprehension would hide the branch.
            complexity += 1 + len(child.ifs)
        elif isinstance(child, _BRANCH_NODES):
            complexity += 1
        elif isinstance(child, ast.BoolOp):
            # `a and b and c` is two extra paths, not one: short-circuiting can
            # stop at either operand.
            complexity += len(child.values) - 1
    return complexity


def _own_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """Walk a definition's body without descending into nested definitions."""
    stack: list[ast.AST] = list(getattr(node, "body", []))
    while stack:
        current = stack.pop()
        yield current
        # A nested def or class owns its own complexity, so its branches are not
        # counted again here.
        if isinstance(current, (*_FUNCTION_NODES, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(current))


def _callee_name(node: ast.AST) -> str | None:
    """Dotted text naming what is being called, or None if it is not nameable.

    `os.path.join(...)` -> `os.path.join`, `self.run()` -> `self.run`,
    `factory()()` -> `factory`.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _callee_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return _callee_name(node.func)
    return None
