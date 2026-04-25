"""TypeScript / TSX parser using tree-sitter-typescript.

Extracts the same kinds of records as the Python parser: Symbol (function,
method, class, interface, type, constant), Call, Import. The shape is
language-agnostic so downstream indexing and search work identically.

Qnames use slash-separated module paths to mirror TS import semantics:

    src/auth/session:SessionManager.refresh
    app/[locale]/page:default
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import tree_sitter_typescript
from tree_sitter import Language, Parser

from snapctx.qname import make_qname, typescript_module_path
from snapctx.schema import Call, Import, ParseResult, Symbol

_TS_LANG = Language(tree_sitter_typescript.language_typescript())
_TSX_LANG = Language(tree_sitter_typescript.language_tsx())


# File extensions we route through the TSX grammar (handles JSX) vs. the
# plain TS grammar. Using TSX for every .jsx/.js file is safe: TSX is a strict
# superset of both JS and JSX.
_TSX_EXTENSIONS = (".tsx", ".jsx", ".js", ".mjs", ".cjs")


class TypeScriptParser:
    language = "typescript"
    extensions = (".ts", ".tsx", ".mts", ".cts", ".jsx", ".js", ".mjs", ".cjs")

    def parse(self, path: Path, root: Path) -> ParseResult:
        source_bytes = path.read_bytes()
        source = source_bytes.decode("utf-8", errors="replace")
        module = typescript_module_path(path, root)
        lang = _TSX_LANG if path.suffix in _TSX_EXTENSIONS else _TS_LANG
        parser = Parser(lang)
        tree = parser.parse(source_bytes)
        visitor = _Visitor(
            module=module,
            file=str(path.resolve()),
            source=source,
            source_bytes=source_bytes,
        )
        visitor.walk(tree.root_node)
        return ParseResult(
            symbols=visitor.symbols,
            calls=visitor.calls,
            imports=visitor.imports,
            language=self.language,
        )


class _Visitor:
    def __init__(self, *, module: str, file: str, source: str, source_bytes: bytes) -> None:
        self.module = module
        self.file = file
        self.source = source
        self.source_bytes = source_bytes
        self.lines = source.splitlines()
        self.symbols: list[Symbol] = []
        self.calls: list[Call] = []
        self.imports: list[Import] = []
        self._stack: list[str] = []                 # member path
        self._current_fn_qname: str | None = None   # enclosing function for call attribution
        self._import_table: dict[str, str] = {}     # local_name -> "<source_module>:<imported_name>"
        self._local_symbol_names: set[str] = set()  # for local call resolution fallback

    # ---------- top-level walk ----------

    def walk(self, node) -> None:
        # Two-pass: first collect top-level symbol names for local-resolution;
        # then do the full walk. Collecting names is cheap and lets us resolve
        # calls to locally-defined sibling symbols regardless of source order.
        self._collect_local_names(node)
        self._emit_module(node)
        self._visit(node)

    def _emit_module(self, root) -> None:
        """Emit Symbol(kind='module') if the file opens with a /** … */ block.

        Convention: in TS/JS, the file-level "docstring" is a JSDoc-style block
        comment at the very top of the program. We only accept /** … */ (not
        plain /* … */, which is usually a license header) to avoid indexing
        noise.
        """
        first_comment = None
        for child in root.children:
            if child.type == "comment":
                first_comment = child
                break
            if child.type in ("hash_bang_line",):
                continue
            break
        if first_comment is None:
            return
        text = self._text(first_comment).strip()
        if not text.startswith("/**"):
            return
        body = text[3:-2] if text.endswith("*/") else text[3:]
        cleaned = "\n".join(line.lstrip(" *") for line in body.splitlines()).strip()
        if not cleaned:
            return
        qname = make_qname(self.module, [])
        signature = f"module {self.module}" if self.module else "module"
        end_line = root.end_point[0] + 1
        self.symbols.append(
            Symbol(
                qname=qname,
                kind="module",
                language="typescript",
                signature=signature,
                docstring=cleaned,
                file=self.file,
                line_start=1,
                line_end=end_line,
                parent_qname=None,
                decorators=[],
                bases=[],
                source_sha=self._sha_of_node(root),
            )
        )

    def _collect_local_names(self, root) -> None:
        for child in root.children:
            self._collect_from(child)

    def _collect_from(self, node) -> None:
        t = node.type
        if t == "export_statement":
            # recurse into the wrapped declaration
            for c in node.children:
                self._collect_from(c)
            return
        if t in ("function_declaration", "class_declaration", "interface_declaration", "type_alias_declaration", "enum_declaration"):
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                self._local_symbol_names.add(self._text(name_node))
        elif t == "lexical_declaration":
            for decl in node.children:
                if decl.type == "variable_declarator":
                    name_node = decl.child_by_field_name("name")
                    if name_node is not None and name_node.type == "identifier":
                        self._local_symbol_names.add(self._text(name_node))

    # ---------- main visitor ----------

    def _visit(self, node, *, in_class: bool = False) -> None:
        t = node.type
        if t == "program":
            for c in node.children:
                self._visit(c)
        elif t == "export_statement":
            for c in node.children:
                self._visit(c, in_class=in_class)
        elif t == "import_statement":
            self._handle_import(node)
        elif t == "function_declaration":
            self._emit_function(node, kind="function")
        elif t == "class_declaration":
            self._emit_class(node)
        elif t == "interface_declaration":
            self._emit_simple(node, kind="interface")
        elif t == "type_alias_declaration":
            self._emit_simple(node, kind="type")
        elif t == "enum_declaration":
            self._emit_simple(node, kind="class")   # treat enum like class
        elif t == "lexical_declaration":
            self._handle_lexical(node)
        elif t == "call_expression" and self._current_fn_qname:
            self._handle_call(node)
        elif t in ("jsx_opening_element", "jsx_self_closing_element") and self._current_fn_qname:
            self._handle_jsx(node)
        else:
            for c in node.children:
                self._visit(c, in_class=in_class)

    # ---------- specific handlers ----------

    def _handle_import(self, node) -> None:
        source_node = node.child_by_field_name("source")
        if source_node is None:
            return
        src = self._text(source_node).strip("'\"")
        # Collect the names introduced by this import.
        # Two common shapes:
        #   import defaultName from '...'
        #   import { a, b as c } from '...'
        #   import * as ns from '...'
        # tree-sitter wraps these in an `import_clause`.
        clause = None
        for c in node.children:
            if c.type == "import_clause":
                clause = c
                break
        if clause is None:
            return
        for ch in clause.children:
            if ch.type == "identifier":
                # default import:  import Foo from '…'
                name = self._text(ch)
                self._import_table[name] = f"{src}:default"
                self.imports.append(
                    Import(file=self.file, module=src, name="default", alias=name, line=node.start_point[0] + 1)
                )
            elif ch.type == "named_imports":
                for spec in ch.children:
                    if spec.type == "import_specifier":
                        orig_node = spec.child_by_field_name("name")
                        alias_node = spec.child_by_field_name("alias")
                        orig = self._text(orig_node) if orig_node else None
                        local = self._text(alias_node) if alias_node else orig
                        if orig and local:
                            self._import_table[local] = f"{src}:{orig}"
                            self.imports.append(
                                Import(
                                    file=self.file,
                                    module=src,
                                    name=orig,
                                    alias=local if local != orig else None,
                                    line=node.start_point[0] + 1,
                                )
                            )
            elif ch.type == "namespace_import":
                # import * as ns from '…'
                for inner in ch.children:
                    if inner.type == "identifier":
                        name = self._text(inner)
                        self._import_table[name] = f"{src}:*"
                        self.imports.append(
                            Import(file=self.file, module=src, name="*", alias=name, line=node.start_point[0] + 1)
                        )

    def _emit_function(self, node, *, kind: str) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self._text(name_node)
        qname = make_qname(self.module, self._stack + [name])
        parent_qname = make_qname(self.module, self._stack) if self._stack else None
        signature = self._header_signature(node, name=name)
        docstring = self._jsdoc_before(node)

        sym_kind = "method" if self._stack_top_is_class() else kind
        self.symbols.append(
            Symbol(
                qname=qname,
                kind=sym_kind,
                language="typescript",
                signature=signature,
                docstring=docstring,
                file=self.file,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                parent_qname=parent_qname,
                decorators=[],
                bases=[],
                source_sha=self._sha_of_node(node),
            )
        )

        self._stack.append(name)
        prev = self._current_fn_qname
        self._current_fn_qname = qname
        body = node.child_by_field_name("body")
        if body is not None:
            for c in body.children:
                self._visit(c)
        self._current_fn_qname = prev
        self._stack.pop()

    def _emit_class(self, node) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self._text(name_node)
        qname = make_qname(self.module, self._stack + [name])
        parent_qname = make_qname(self.module, self._stack) if self._stack else None
        signature = self._header_signature(node, name=name)
        docstring = self._jsdoc_before(node)

        # bases: from class_heritage node
        bases: list[str] = []
        heritage = None
        for c in node.children:
            if c.type == "class_heritage":
                heritage = c
                break
        if heritage is not None:
            for c in heritage.children:
                if c.type in ("extends_clause", "implements_clause"):
                    for n in c.children:
                        if n.type in ("identifier", "type_identifier"):
                            bases.append(self._text(n))
                        elif n.type == "member_expression":
                            bases.append(self._text(n))

        self.symbols.append(
            Symbol(
                qname=qname,
                kind="class",
                language="typescript",
                signature=signature,
                docstring=docstring,
                file=self.file,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                parent_qname=parent_qname,
                decorators=[],
                bases=bases,
                source_sha=self._sha_of_node(node),
            )
        )

        self._stack.append(name)
        body = node.child_by_field_name("body")
        if body is not None:
            for c in body.children:
                if c.type in ("method_definition", "abstract_method_signature"):
                    self._emit_method(c)
                elif c.type == "public_field_definition":
                    # e.g. class-level constants: `static DEFAULT = 3;`
                    self._maybe_emit_class_field(c)
        self._stack.pop()

    def _emit_method(self, node) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self._text(name_node)
        qname = make_qname(self.module, self._stack + [name])
        parent_qname = make_qname(self.module, self._stack)
        signature = self._header_signature(node, name=name)
        docstring = self._jsdoc_before(node)
        self.symbols.append(
            Symbol(
                qname=qname,
                kind="method",
                language="typescript",
                signature=signature,
                docstring=docstring,
                file=self.file,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                parent_qname=parent_qname,
                decorators=[],
                bases=[],
                source_sha=self._sha_of_node(node),
            )
        )
        self._stack.append(name)
        prev = self._current_fn_qname
        self._current_fn_qname = qname
        body = node.child_by_field_name("body")
        if body is not None:
            for c in body.children:
                self._visit(c)
        self._current_fn_qname = prev
        self._stack.pop()

    def _maybe_emit_class_field(self, node) -> None:
        """Class-level constant: UPPER_CASE readonly field with a literal init."""
        name_node = node.child_by_field_name("name")
        value_node = node.child_by_field_name("value")
        if name_node is None or value_node is None:
            return
        name = self._text(name_node)
        if not name.isupper() and "_" not in name:
            return
        if value_node.type not in ("number", "string", "true", "false", "null", "array", "object"):
            return
        signature = self._text(node).strip().rstrip(";").strip()
        self.symbols.append(
            Symbol(
                qname=make_qname(self.module, self._stack + [name]),
                kind="constant",
                language="typescript",
                signature=signature,
                docstring=self._jsdoc_before(node),
                file=self.file,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                parent_qname=make_qname(self.module, self._stack),
                decorators=[],
                bases=[],
                source_sha=self._sha_of_node(node),
            )
        )

    def _emit_simple(self, node, *, kind: str) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = self._text(name_node)
        qname = make_qname(self.module, self._stack + [name])
        parent_qname = make_qname(self.module, self._stack) if self._stack else None
        signature = self._header_signature(node, name=name)
        docstring = self._jsdoc_before(node)
        self.symbols.append(
            Symbol(
                qname=qname,
                kind=kind,
                language="typescript",
                signature=signature,
                docstring=docstring,
                file=self.file,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                parent_qname=parent_qname,
                decorators=[],
                bases=[],
                source_sha=self._sha_of_node(node),
            )
        )

    def _handle_lexical(self, node) -> None:
        """`const/let X = ...`  — depending on the RHS, emit function or constant."""
        for decl in node.children:
            if decl.type != "variable_declarator":
                continue
            name_node = decl.child_by_field_name("name")
            value_node = decl.child_by_field_name("value")
            if name_node is None or name_node.type != "identifier":
                continue
            name = self._text(name_node)

            if value_node is None:
                continue

            # arrow function / function expression  -> kind=function (or component)
            if value_node.type in ("arrow_function", "function_expression", "function"):
                kind = "component" if _looks_like_component(name, value_node) else "function"
                qname = make_qname(self.module, self._stack + [name])
                parent_qname = make_qname(self.module, self._stack) if self._stack else None
                signature = self._lexical_function_signature(name, node, value_node)
                docstring = self._jsdoc_before(node)
                self.symbols.append(
                    Symbol(
                        qname=qname,
                        kind=kind,
                        language="typescript",
                        signature=signature,
                        docstring=docstring,
                        file=self.file,
                        line_start=node.start_point[0] + 1,
                        line_end=node.end_point[0] + 1,
                        parent_qname=parent_qname,
                        decorators=[],
                        bases=[],
                        source_sha=self._sha_of_node(node),
                    )
                )
                self._stack.append(name)
                prev = self._current_fn_qname
                self._current_fn_qname = qname
                body = value_node.child_by_field_name("body")
                if body is not None:
                    self._visit(body)
                self._current_fn_qname = prev
                self._stack.pop()
                continue

            # constant literal at module scope
            if _is_module_scope(node) and _is_constant_like(value_node):
                if name.isupper() or _has_type_annotation(name_node.parent):
                    signature = self._text(decl).strip()
                    # Large typed configs (e.g. `const cols: ColumnDef<T>[] = [...]`
                    # in a React table) can have multi-KB RHS values. Truncate so
                    # the stored signature and its FTS/embedding text stay small.
                    if len(signature) > 240:
                        signature = signature[:240].rstrip() + " …"
                    self.symbols.append(
                        Symbol(
                            qname=make_qname(self.module, self._stack + [name]),
                            kind="constant",
                            language="typescript",
                            signature=signature,
                            docstring=self._jsdoc_before(node),
                            file=self.file,
                            line_start=node.start_point[0] + 1,
                            line_end=node.end_point[0] + 1,
                            parent_qname=make_qname(self.module, self._stack) if self._stack else None,
                            decorators=[],
                            bases=[],
                            source_sha=self._sha_of_node(node),
                        )
                    )
                    continue

    def _handle_call(self, node) -> None:
        func_node = node.child_by_field_name("function")
        if func_node is None:
            return
        raw = self._render_callee(func_node)
        if raw is None:
            return
        resolved = self._resolve_call(raw)
        self.calls.append(
            Call(
                caller_qname=self._current_fn_qname,
                callee_name=raw,
                callee_qname=resolved,
                file=self.file,
                line=node.start_point[0] + 1,
            )
        )
        for c in node.children:
            self._visit(c)

    def _handle_jsx(self, node) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        raw = self._render_jsx_name(name_node)
        if raw is None:
            return
        # Only treat capitalized names as component uses (lowercase is HTML)
        first = raw.split(".")[0]
        if not first or not first[0].isupper():
            return
        resolved = self._resolve_call(raw)
        self.calls.append(
            Call(
                caller_qname=self._current_fn_qname,
                callee_name=f"<{raw}>",
                callee_qname=resolved,
                file=self.file,
                line=node.start_point[0] + 1,
            )
        )

    # ---------- resolution ----------

    def _resolve_call(self, raw: str) -> str | None:
        """Resolve a callee string to a qname, using the import table and local defs."""
        head, _, tail = raw.partition(".")
        # this.X() and super.X() → optimistic guess against the enclosing class.
        # Like Python's self.X, the actual method may be defined later in the
        # class body than the caller; the demote pass nulls bogus guesses and
        # the promote pass then re-resolves forward references.
        if head == "this":
            if "." in tail:
                return None  # this.obj.method — attribute chain, don't guess
            cls_qname = self._enclosing_class_qname()
            if cls_qname is None or not tail:
                return None
            return f"{cls_qname}.{tail}"
        if head == "super":
            if "." in tail:
                return None
            cls_qname = self._enclosing_class_qname()
            if cls_qname is None or not tail:
                return None
            # Find the class symbol to walk its bases.
            cls_sym = next(
                (s for s in self.symbols if s.qname == cls_qname and s.kind == "class"),
                None,
            )
            if cls_sym and cls_sym.bases:
                base = cls_sym.bases[0]
                base_head, _, base_tail = base.partition(".")
                if base_head in self._import_table:
                    origin = self._import_table[base_head]
                    src_mod, _, src_name = origin.partition(":")
                    if src_mod.startswith("./") or src_mod.startswith("../"):
                        src_mod = _resolve_relative_module(self.module, src_mod)
                    parts = [src_name] + (base_tail.split(".") if base_tail else []) + [tail]
                    return make_qname(src_mod, parts)
                # Locally-defined base class.
                return make_qname(self.module, [base_head, tail])
            return None
        if head in self._import_table:
            origin = self._import_table[head]  # "<src_module>:<orig_name>"
            src_mod, _, src_name = origin.partition(":")
            # Best-effort module path: if src starts with './', resolve relative
            # to this file's module dir.
            if src_mod.startswith("./") or src_mod.startswith("../"):
                src_mod = _resolve_relative_module(self.module, src_mod)
            if src_name == "*":
                # `ns.foo()`  → src_mod:foo
                if tail:
                    return make_qname(src_mod, tail.split("."))
                return None
            if src_name == "default":
                # `import Foo from '…'; Foo.bar()` — resolve to src_mod:bar
                # (we don't know the "default export" qname without cross-file
                # knowledge; point at src_mod:default as a placeholder).
                if tail:
                    return make_qname(src_mod, tail.split("."))
                return f"{src_mod}:default"
            # Named import:  `foo()` -> src_mod:foo;  `foo.bar()` -> src_mod:foo.bar
            parts = [src_name] + (tail.split(".") if tail else [])
            return make_qname(src_mod, parts)
        # locally defined symbol
        if head in self._local_symbol_names:
            parts = [head] + (tail.split(".") if tail else [])
            return make_qname(self.module, parts)
        return None

    # ---------- helpers ----------

    def _text(self, node) -> str:
        return self.source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")

    def _header_signature(self, node, *, name: str) -> str:
        """Return the declaration header (up to body/`{`/`;`), single-lined."""
        body = node.child_by_field_name("body")
        if body is not None:
            header_bytes = self.source_bytes[node.start_byte : body.start_byte]
        else:
            header_bytes = self.source_bytes[node.start_byte : node.end_byte]
        text = header_bytes.decode("utf-8", errors="replace").strip().rstrip("{").strip()
        # Collapse whitespace for a clean one-line signature.
        return " ".join(text.split())

    def _lexical_function_signature(self, name: str, lexical_node, value_node) -> str:
        """Build a signature like ``const foo = (x: number): string =>``."""
        body = value_node.child_by_field_name("body")
        if body is not None:
            end = body.start_byte
        else:
            end = value_node.end_byte
        header = self.source_bytes[lexical_node.start_byte : end].decode("utf-8", errors="replace").strip()
        header = header.rstrip("{").rstrip().rstrip("=>").rstrip()
        return " ".join(header.split())

    def _jsdoc_before(self, node) -> str | None:
        """If the IMMEDIATELY-preceding sibling is a /** … */ comment, return its cleaned text.

        Only the immediate sibling counts — we must not walk past a prior
        declaration to pick up a stray JSDoc, or every symbol would inherit
        the nearest older doc.
        """
        cursor = node
        # export_statement wraps the declaration; the comment sits before that wrapper.
        while cursor.parent is not None and cursor.parent.type == "export_statement":
            cursor = cursor.parent
        prev = cursor.prev_sibling
        if prev is None or prev.type != "comment":
            return None
        text = self._text(prev).strip()
        if not text.startswith("/**"):
            return None
        body = text[3:-2] if text.endswith("*/") else text[3:]
        cleaned = "\n".join(line.lstrip(" *") for line in body.splitlines()).strip()
        return cleaned or None

    def _sha_of_node(self, node) -> str:
        return hashlib.sha1(self.source_bytes[node.start_byte : node.end_byte]).hexdigest()

    def _stack_top_is_class(self) -> bool:
        if not self._stack:
            return False
        top_qname = make_qname(self.module, self._stack)
        for s in reversed(self.symbols):
            if s.qname == top_qname:
                return s.kind == "class"
        return False

    def _enclosing_class_qname(self) -> str | None:
        """Return the qname of the nearest enclosing class frame, or None."""
        for i in range(len(self._stack), 0, -1):
            cand = make_qname(self.module, self._stack[:i])
            for s in self.symbols:
                if s.qname == cand and s.kind == "class":
                    return cand
        return None

    def _render_callee(self, func_node) -> str | None:
        if func_node.type == "identifier":
            return self._text(func_node)
        if func_node.type == "member_expression":
            parts: list[str] = []
            cur = func_node
            while cur.type == "member_expression":
                prop = cur.child_by_field_name("property")
                if prop is None:
                    return None
                parts.append(self._text(prop))
                cur = cur.child_by_field_name("object")
                if cur is None:
                    return None
            if cur.type == "identifier" or cur.type == "this":
                parts.append(self._text(cur))
                return ".".join(reversed(parts))
            return None
        return None

    def _render_jsx_name(self, name_node) -> str | None:
        if name_node.type == "identifier":
            return self._text(name_node)
        if name_node.type == "member_expression":
            return self._render_callee(name_node)
        return None


# ---------- free helpers ----------


def _is_module_scope(node) -> bool:
    """True if `node` is a direct child of program (possibly through export_statement)."""
    p = node.parent
    while p is not None and p.type == "export_statement":
        p = p.parent
    return p is not None and p.type == "program"


def _is_constant_like(value_node) -> bool:
    t = value_node.type
    if t in ("number", "string", "template_string", "true", "false", "null", "undefined", "regex"):
        return True
    if t == "unary_expression":
        return True
    if t in ("array", "object"):
        return True
    if t in ("identifier", "member_expression"):
        return True
    return False


def _has_type_annotation(parent) -> bool:
    if parent is None:
        return False
    return any(c.type == "type_annotation" for c in parent.children)


def _looks_like_component(name: str, value_node) -> bool:
    """Heuristic: a function is a React component if its name starts with a
    capital letter AND either its type annotation mentions a React type, or the
    body contains a JSX expression."""
    if not name or not name[0].isupper():
        return False
    # Scan body for JSX
    body = value_node.child_by_field_name("body")
    if body is None:
        return False
    return _contains_jsx(body)


def _contains_jsx(node, limit: int = 50) -> bool:
    stack = [node]
    seen = 0
    while stack and seen < limit:
        n = stack.pop()
        seen += 1
        if n.type in ("jsx_element", "jsx_self_closing_element", "jsx_fragment"):
            return True
        stack.extend(n.children)
    return False


def _resolve_relative_module(current_module: str, rel: str) -> str:
    """Resolve './foo' or '../foo/bar' against the current module path.

    current_module='app/components/header', rel='./Button' -> 'app/components/Button'
    current_module='app/components/header', rel='../utils'  -> 'app/utils'
    """
    cur_parts = current_module.split("/")
    # current_module points at the file itself; drop its basename
    cur_dir = cur_parts[:-1]
    rel_parts = rel.split("/")
    i = 0
    while i < len(rel_parts):
        p = rel_parts[i]
        if p == ".":
            pass
        elif p == "..":
            if cur_dir:
                cur_dir.pop()
        else:
            cur_dir.append(p)
        i += 1
    return "/".join(cur_dir)
