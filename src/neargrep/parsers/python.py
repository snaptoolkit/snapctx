"""Python parser using the stdlib `ast` module.

Emits Symbol, Call, Import records. Calls are resolved heuristically against the
file's import table; unresolved calls keep `callee_qname=None` and still carry
the raw name so BM25 search can match them.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

from neargrep.qname import make_qname, python_module_path
from neargrep.schema import Call, Import, ParseResult, Symbol


class PythonParser:
    language = "python"
    extensions = (".py", ".pyi")

    def parse(self, path: Path, root: Path) -> ParseResult:
        source = path.read_text(encoding="utf-8", errors="replace")
        module = python_module_path(path, root)
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            return ParseResult([], [], [], self.language)

        visitor = _Visitor(
            module=module,
            file=str(path.resolve()),
            source=source,
        )
        visitor.emit_module(tree)
        visitor.visit(tree)
        return ParseResult(
            symbols=visitor.symbols,
            calls=visitor.calls,
            imports=visitor.imports,
            language=self.language,
        )


class _Visitor(ast.NodeVisitor):
    def __init__(self, *, module: str, file: str, source: str) -> None:
        self.module = module
        self.file = file
        self.source_lines = source.splitlines()
        self.symbols: list[Symbol] = []
        self.calls: list[Call] = []
        self.imports: list[Import] = []
        self._stack: list[str] = []         # member path (class/function names)
        self._current_fn_qname: str | None = None  # enclosing function for call attribution
        self._import_table: dict[str, str] = {}  # local_name -> fully qualified origin

    # ---------- module ----------

    def emit_module(self, tree: ast.Module) -> None:
        """Emit a Symbol(kind='module') iff the file has a module-level docstring.

        Module docstrings often hold the architectural prose — the "why" a
        file exists — that isn't repeated on any single class/function. Making
        the module a first-class symbol lets FTS and vector search surface it.
        """
        docstring = ast.get_docstring(tree, clean=True)
        if not docstring:
            return
        qname = make_qname(self.module, [])
        end_line = max(
            (getattr(n, "end_lineno", None) or getattr(n, "lineno", 1) for n in tree.body),
            default=1,
        )
        signature = f"module {self.module}" if self.module else "module"
        self.symbols.append(
            Symbol(
                qname=qname,
                kind="module",
                language="python",
                signature=signature,
                docstring=docstring,
                file=self.file,
                line_start=1,
                line_end=end_line,
                parent_qname=None,
                decorators=[],
                bases=[],
                source_sha=_sha_of_lines(self.source_lines, 1, end_line),
            )
        )

    # ---------- imports ----------

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            mod = alias.name
            local = alias.asname or alias.name.split(".")[0]
            self._import_table[local] = mod
            self.imports.append(
                Import(file=self.file, module=mod, name=None, alias=alias.asname, line=node.lineno)
            )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        mod = node.module or ""
        # relative imports — we don't resolve them against package layout in v1
        for alias in node.names:
            local = alias.asname or alias.name
            self._import_table[local] = f"{mod}.{alias.name}" if mod else alias.name
            self.imports.append(
                Import(
                    file=self.file,
                    module=mod,
                    name=alias.name,
                    alias=alias.asname,
                    line=node.lineno,
                )
            )
        self.generic_visit(node)

    # ---------- definitions ----------

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._handle_function(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._handle_function(node, is_async=True)

    def visit_Module(self, node: ast.Module) -> None:
        # Emit module-level constants before descending.
        for child in node.body:
            self._maybe_emit_constant(child, parent_qname=None, inside_stack=[])
        self.generic_visit(node)

    def _maybe_emit_constant(
        self, node: ast.AST, *, parent_qname: str | None, inside_stack: list[str]
    ) -> None:
        """Emit Symbol(kind='constant') for top-level or class-level NAME=literal assignments.

        Accepts:
          - Assign:    NAME = <literal>
          - AnnAssign: NAME: type = <literal>
        Where <literal> is a constant, list/tuple/set of constants, or a Name
        (often referencing another constant — we record the ref string).
        """
        targets: list[ast.AST] = []
        value: ast.AST | None = None
        annotation: ast.AST | None = None
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value
            annotation = node.annotation
        else:
            return

        for tgt in targets:
            if not isinstance(tgt, ast.Name):
                continue
            name = tgt.id
            # UPPER_CASE names OR annotated module-level names are indexed.
            # (Heuristic: `foo = bar()` at module scope isn't a constant.)
            if not (name.isupper() or "_" in name and name.replace("_", "").isalpha() and name.isupper()) and annotation is None:
                # allow single-word UPPER like "DEBUG", also mixed like "Pi" — but usually constants are UPPER
                if not name.isupper():
                    continue
            if not _is_literal_like(value):
                continue
            rendered_value = _render_annotation(value)
            if len(rendered_value) > 120:
                rendered_value = rendered_value[:117] + "..."
            ann = f": {_render_annotation(annotation)}" if annotation is not None else ""
            signature = f"{name}{ann} = {rendered_value}"
            qname = make_qname(self.module, inside_stack + [name])
            self.symbols.append(
                Symbol(
                    qname=qname,
                    kind="constant",
                    language="python",
                    signature=signature,
                    docstring=None,
                    file=self.file,
                    line_start=node.lineno,
                    line_end=_end_line(node),
                    parent_qname=parent_qname,
                    decorators=[],
                    bases=[],
                    source_sha=_sha_of_lines(self.source_lines, node.lineno, _end_line(node)),
                )
            )

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qname = make_qname(self.module, self._stack + [node.name])
        parent_qname = make_qname(self.module, self._stack) if self._stack else None
        signature = self._class_signature(node)
        docstring = ast.get_docstring(node, clean=True)
        decorators = [_render_decorator(d) for d in node.decorator_list]
        bases = [b for b in (_render_base(base) for base in node.bases) if b]
        self.symbols.append(
            Symbol(
                qname=qname,
                kind="class",
                language="python",
                signature=signature,
                docstring=docstring,
                file=self.file,
                line_start=node.lineno,
                line_end=_end_line(node),
                parent_qname=parent_qname,
                decorators=decorators,
                bases=bases,
                source_sha=_sha_of_lines(self.source_lines, node.lineno, _end_line(node)),
            )
        )
        # Emit class-level constants (UPPER_CASE class vars) before descending.
        class_qname = qname
        for child in node.body:
            self._maybe_emit_constant(
                child, parent_qname=class_qname, inside_stack=self._stack + [node.name]
            )
        self._stack.append(node.name)
        prev = self._current_fn_qname
        self._current_fn_qname = None  # methods inside will set their own
        self.generic_visit(node)
        self._current_fn_qname = prev
        self._stack.pop()

    def _handle_function(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, *, is_async: bool
    ) -> None:
        qname = make_qname(self.module, self._stack + [node.name])
        parent_qname = make_qname(self.module, self._stack) if self._stack else None
        # kind: method if the nearest enclosing scope is a class, else function
        # (we can't tell from _stack alone whether the parent is class vs function;
        # we approximate: if parent_qname ends with a class-like frame, classify as method)
        kind = "method" if self._parent_is_class() else "function"
        signature = self._function_signature(node, is_async=is_async)
        docstring = ast.get_docstring(node, clean=True)
        decorators = [_render_decorator(d) for d in node.decorator_list]
        self.symbols.append(
            Symbol(
                qname=qname,
                kind=kind,
                language="python",
                signature=signature,
                docstring=docstring,
                file=self.file,
                line_start=node.lineno,
                line_end=_end_line(node),
                parent_qname=parent_qname,
                decorators=decorators,
                source_sha=_sha_of_lines(self.source_lines, node.lineno, _end_line(node)),
            )
        )
        self._stack.append(node.name)
        prev = self._current_fn_qname
        self._current_fn_qname = qname
        self.generic_visit(node)
        self._current_fn_qname = prev
        self._stack.pop()

    # ---------- calls ----------

    def visit_Call(self, node: ast.Call) -> None:
        if self._current_fn_qname is None:
            self.generic_visit(node)
            return

        raw_name = _render_call_target(node.func)
        if raw_name is None:
            self.generic_visit(node)
            return

        resolved = self._resolve_call(raw_name)
        self.calls.append(
            Call(
                caller_qname=self._current_fn_qname,
                callee_name=raw_name,
                callee_qname=resolved,
                file=self.file,
                line=node.lineno,
            )
        )
        self.generic_visit(node)

    # ---------- helpers ----------

    def _parent_is_class(self) -> bool:
        """True if the most recently pushed _stack frame was a class (vs function).

        We track this by convention: functions push themselves as they recurse,
        but only `visit_ClassDef` sets a class frame. Since we can't distinguish
        frame type from the stack alone, we look it up by scanning recorded symbols.
        """
        if not self._stack:
            return False
        parent_qname = make_qname(self.module, self._stack)
        for sym in reversed(self.symbols):
            if sym.qname == parent_qname:
                return sym.kind == "class"
        return False

    def _resolve_call(self, raw: str) -> str | None:
        """Resolve a call-site name to a qname, or None if it can't be resolved.

        Handles:
          - `self.method()`   → <enclosing class qname>.method, if method is defined
          - `foo()`           → resolve 'foo' via import table or local symbol table
          - `mod.bar()`       → resolve 'mod' via import table, append '.bar'
          - `self.x.y`        → unresolved (attribute chain, not a direct method call)
        """
        if raw.startswith("self."):
            remainder = raw[len("self."):]
            if "." in remainder:
                return None  # self.attr.x — attribute chain, don't guess
            # Find nearest enclosing class in _stack.
            for i in range(len(self._stack), 0, -1):
                cand_class = make_qname(self.module, self._stack[:i])
                cls_sym = next(
                    (s for s in self.symbols if s.qname == cand_class and s.kind == "class"),
                    None,
                )
                if cls_sym is None:
                    continue
                direct = f"{cand_class}.{remainder}"
                if any(s.qname == direct for s in self.symbols):
                    return direct
                # Walk declared bases in order — optimistic: return the first base
                # that the import table points to. The post-ingest demotion pass
                # will null out any candidate that doesn't exist as a symbol.
                for base in cls_sym.bases:
                    base_head, _, base_tail = base.partition(".")
                    if base_head in self._import_table:
                        origin = self._import_table[base_head]
                        if not base_tail:
                            # `from X import Base` -> origin is 'X.Base'
                            if "." in origin:
                                base_mod, _, base_name = origin.rpartition(".")
                            else:
                                base_mod, base_name = origin, base
                            return make_qname(base_mod, [base_name, remainder])
                        # `import X; class C(X.Base):` -> origin='X', tail='Base'
                        return make_qname(origin, base_tail.split(".") + [remainder])
                    # Base defined locally in this module
                    local_base_qname = make_qname(self.module, [base_head])
                    if any(s.qname == local_base_qname for s in self.symbols):
                        return f"{local_base_qname}.{remainder}"
                return None
            return None

        head, _, tail = raw.partition(".")
        if head in self._import_table:
            origin = self._import_table[head]
            if not tail:
                if "." in origin:
                    mod, _, name = origin.rpartition(".")
                    return make_qname(mod, [name])
                return make_qname(origin, [])
            return make_qname(origin, tail.split("."))

        # Fallback: locally-defined top-level name in this module.
        local_qname = make_qname(self.module, [head])
        for sym in self.symbols:
            if sym.qname == local_qname:
                return f"{local_qname}.{tail}" if tail else local_qname
        return None

    def _function_signature(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, *, is_async: bool
    ) -> str:
        args = _render_arguments(node.args)
        ret = ""
        if node.returns is not None:
            ret = f" -> {_render_annotation(node.returns)}"
        prefix = "async def " if is_async else "def "
        return f"{prefix}{node.name}({args}){ret}"

    def _class_signature(self, node: ast.ClassDef) -> str:
        bases = [_render_annotation(b) for b in node.bases]
        kw = [f"{kw.arg}={_render_annotation(kw.value)}" for kw in node.keywords if kw.arg]
        inside = ", ".join(bases + kw)
        return f"class {node.name}({inside})" if inside else f"class {node.name}"


# ---------- rendering helpers ----------


def _render_annotation(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return "<?>"


def _render_decorator(node: ast.AST) -> str:
    try:
        return "@" + ast.unparse(node)
    except Exception:
        return "@<?>"


def _is_literal_like(node: ast.AST | None) -> bool:
    """Accepts constants, collections of constants, references to other names, and simple calls.

    We're generous: anything with an obvious literal or a name/attribute reference
    counts. This is because many 'constants' reference other module constants
    (DEFAULT_X = OTHER_DEFAULT) or are calls to simple factory functions.
    """
    if node is None:
        return False
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.Name, ast.Attribute)):
        return True
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(_is_literal_like(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(_is_literal_like(k) and _is_literal_like(v) for k, v in zip(node.keys, node.values))
    if isinstance(node, ast.UnaryOp) and isinstance(node.operand, ast.Constant):
        return True
    return False


def _render_base(node: ast.AST) -> str | None:
    """Render a base-class expression as a dotted string, or None if too complex."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parts: list[str] = [node.attr]
        cur: ast.AST = node.value
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
            return ".".join(reversed(parts))
    return None


def _render_call_target(func: ast.AST) -> str | None:
    """Render the callee expression of a Call node as a dotted name, if possible."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        parts: list[str] = [func.attr]
        node: ast.AST = func.value
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
            return ".".join(reversed(parts))
    return None


def _render_arguments(args: ast.arguments) -> str:
    """Render a function's parameters as a signature string."""
    parts: list[str] = []
    posonly = args.posonlyargs or []
    pos = args.args or []
    kwonly = args.kwonlyargs or []
    defaults = args.defaults or []
    kwdefaults = args.kw_defaults or []

    # positional (posonly + pos) defaults align to the tail
    all_positional = posonly + pos
    offset = len(all_positional) - len(defaults)
    for i, a in enumerate(all_positional):
        part = a.arg
        if a.annotation is not None:
            part += f": {_render_annotation(a.annotation)}"
        if i >= offset:
            default = defaults[i - offset]
            part += f" = {_render_annotation(default)}"
        parts.append(part)
        if posonly and i == len(posonly) - 1:
            parts.append("/")

    if args.vararg is not None:
        v = f"*{args.vararg.arg}"
        if args.vararg.annotation is not None:
            v += f": {_render_annotation(args.vararg.annotation)}"
        parts.append(v)
    elif kwonly:
        parts.append("*")

    for i, a in enumerate(kwonly):
        part = a.arg
        if a.annotation is not None:
            part += f": {_render_annotation(a.annotation)}"
        if i < len(kwdefaults) and kwdefaults[i] is not None:
            part += f" = {_render_annotation(kwdefaults[i])}"
        parts.append(part)

    if args.kwarg is not None:
        k = f"**{args.kwarg.arg}"
        if args.kwarg.annotation is not None:
            k += f": {_render_annotation(args.kwarg.annotation)}"
        parts.append(k)

    return ", ".join(parts)


def _end_line(node: ast.AST) -> int:
    return getattr(node, "end_lineno", None) or getattr(node, "lineno", 1)


def _sha_of_lines(lines: list[str], start: int, end: int) -> str:
    chunk = "\n".join(lines[start - 1 : end])
    return hashlib.sha1(chunk.encode("utf-8")).hexdigest()
