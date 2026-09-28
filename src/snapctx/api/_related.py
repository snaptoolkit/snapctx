"""Small, source-verified import and route supplements for selected symbols.

This is one hop, not a recursive dependency dump. Unresolved/ambiguous imports
are omitted. No names are guessed globally and no external packages are read.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from snapctx.api._common import open_index
from snapctx.index import sha_bytes

_EXTENSIONS = ('.py', '.pyi', '.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs')
_JSON_STRING = r'"(?:\\.|[^"\\])*"'


def _json_config(path: Path) -> dict:
    """Read common JSONC configs without treating comment text in strings as comments."""
    text = path.read_text()
    text = re.sub(_JSON_STRING + r'|//[^\n]*|/\*[\s\S]*?\*/',
                  lambda m: m[0] if m[0].startswith('"') else '', text)
    text = re.sub(_JSON_STRING + r'|,\s*([}\]])',
                  lambda m: m[1] if m[1] else m[0], text)
    return json.loads(text)


def _ts_alias_bases(origin: Path, module: str, root: Path) -> list[Path]:
    # A referenced app config can hold aliases while tsconfig.json only lists
    # project references. Follow local configs; never resolve package extends.
    seen: set[Path] = set()

    def visit(config: Path, depth: int = 0) -> list[Path]:
        config = config.resolve()
        if depth > 5 or config in seen or not config.is_relative_to(root):
            return []
        seen.add(config)
        try:
            if config.stat().st_size > 256_000:
                return []
            data = _json_config(config)
            options = data.get('compilerOptions', {})
            base = config.parent / options.get('baseUrl', '.')
            matches = []
            for pattern, targets in options.get('paths', {}).items():
                if not isinstance(targets, list) or pattern.count('*') > 1:
                    continue
                before, star, after = pattern.partition('*')
                if (not star and module != pattern) or (star and not (
                    module.startswith(before) and module.endswith(after)
                    and len(module) >= len(before) + len(after)
                )):
                    continue
                middle = module[len(before):len(module)-len(after) if after else None] if star else ''
                matches.extend(base / target.replace('*', middle) for target in targets if isinstance(target, str))
            if matches:
                return matches
            parent = data.get('extends', '')
            if isinstance(parent, str) and parent.startswith('.'):
                parent_path = config.parent / parent
                if not parent_path.suffix:
                    parent_path = parent_path.with_suffix('.json')
                matches.extend(visit(parent_path, depth + 1))
            for reference in data.get('references', []):
                ref = reference.get('path')
                if isinstance(ref, str):
                    target = config.parent / ref
                    if target.suffix != '.json':
                        target = target / 'tsconfig.json'
                    matches.extend(visit(target, depth + 1))
            return matches
        except (OSError, ValueError, TypeError, AttributeError):
            return []

    for directory in (origin.parent, *origin.parent.parents):
        if not directory.is_relative_to(root):
            break
        config = directory / 'tsconfig.json'
        if config.is_file():
            return visit(config)
    return []


def _resolve_module(origin: Path, module: str, level: int, root: Path,
                    indexed: set[str], python: bool) -> Path | None:
    if python:
        suffix = Path(*module.split('.')) if module else Path('.')
        if level:
            parent = origin.parent
            for _ in range(level - 1):
                parent = parent.parent
            bases = [parent / suffix]
        else:
            bases = [p / suffix for p in (origin.parent, *origin.parent.parents)
                     if p.is_relative_to(root)]
        extensions = ('.py', '.pyi')
        index_names = ('__init__.py',)
    else:
        bases = [origin.parent / module] if module.startswith('.') else _ts_alias_bases(origin, module, root)
        extensions = _EXTENSIONS[2:]
        index_names = tuple('index' + ext for ext in extensions)
    found = set()
    for base in bases:
        candidates = [base] if base.suffix in extensions else []
        candidates += [Path(str(base) + ext) for ext in extensions]
        candidates += [base / name for name in index_names]
        # TS source often uses emitted .js extensions in import specifiers.
        if not python and base.suffix in ('.js', '.jsx', '.mjs', '.cjs'):
            candidates += [base.with_suffix(ext) for ext in ('.ts', '.tsx')]
        for target in candidates:
            target = target.resolve()
            if target.is_relative_to(root) and str(target) in indexed:
                found.add(target)
    return next(iter(found)) if len(found) == 1 else None


def related_files(qnames: list[str], *, root: str | Path = '.', query: str = '',
                  limit: int = 4, scope: str | None = None) -> dict:
    """Return up to ``limit`` additional files justified by imports or routes.

    Files must be indexed, inside ``root``, and match their indexed SHA. Each
    result records its origin symbol and the import/registration line. Unknown
    qnames and ambiguous imports are skipped. This does not refresh the index.
    """
    if not isinstance(qnames, (list, tuple)) or len(qnames) > 36 or any(not isinstance(q, str) for q in qnames):
        raise ValueError('qnames must contain at most 36 strings')
    _validate_limit(limit)
    root = Path(root).resolve()
    idx = open_index(root, scope=scope)
    try:
        seeds = [dict(row) for q in qnames if (row := idx.get_symbol(q)) is not None]
        return {'related_files': collect_related_files(idx, seeds, root, query, limit)}
    finally:
        idx.close()


def _validate_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 16:
        raise ValueError('related file limit must be an integer between 0 and 16')


def collect_related_files(idx, seeds: list[dict], root: Path, query: str, limit: int) -> list[dict]:
    _validate_limit(limit)
    if not limit or not seeds:
        return []
    indexed = {r['path']: r['sha'] for r in idx.conn.execute('SELECT path, sha FROM files')}
    excluded = {s['file'] for s in seeds}
    indexed_paths = set(indexed)
    sources: dict[str, str | None] = {}
    import_cache: dict[str, list] = {}
    resolution_cache: dict[tuple, Path | None] = {}
    candidates: dict[str, dict] = {}
    terms = set(re.findall(r'[a-z0-9]{3,}', query.lower())) - {
        'the', 'and', 'how', 'does', 'where', 'which', 'from', 'with', 'for', 'are',
        'that', 'this', 'through', 'frontend', 'backend', 'file', 'files',
    }

    def source(file):
        if file not in sources:
            p = Path(file)
            sources[file] = None
            try:
                if (p.resolve().is_relative_to(root) and file in indexed
                        and p.stat().st_size <= 512_000):
                    data = p.read_bytes()
                    if sha_bytes(data) == indexed[file]:
                        sources[file] = data.decode('utf-8')
            except (OSError, UnicodeError):
                pass
        return sources[file]

    def add(file, seed, relation, line, name, weight):
        if file in excluded or source(file) is None:
            return
        overlap = sum(t in name.lower() for t in terms)
        evidence = {'relation': relation, 'from': seed['qname'], 'line': line, 'name': name}
        score = weight + min(overlap, 3) * 2
        item = candidates.setdefault(file, {'file': file, 'evidence': [], '_score': score})
        item['_score'] = max(item['_score'], score)
        if evidence not in item['evidence'] and len(item['evidence']) < 2:
            item['evidence'].append(evidence)

    for seed in seeds[:36]:
        row = idx.get_symbol(seed['qname'])
        if row is None:
            continue
        file = row['file']
        text = source(file)
        if text is None:
            continue
        body = '\n'.join(text.splitlines()[max(0, row['line_start']-1):row['line_end']])
        python = Path(file).suffix in ('.py', '.pyi')
        imports = import_cache.get(file, [])
        if file in import_cache:
            pass
        elif python:
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            top_imports = {id(node) for node in tree.body}
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        imports.append((node.module or '', node.level, alias.name, alias.asname or alias.name, node.lineno, id(node) in top_imports))
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.append((alias.name, 0, alias.name, alias.asname or alias.name.split('.')[0], node.lineno, id(node) in top_imports))
        else:
            for imp in idx.imports_for_file(file):
                name = imp['alias'] or imp['name']
                if name:
                    imports.append((imp['module'], 0, imp['name'] or '', name, imp['line'], True))
        import_cache[file] = imports
        for module, level, name, local, line, top_level in imports:
            if not top_level and not row['line_start'] <= line <= row['line_end']:
                continue
            if local == '*' or not re.search(r'(?<![\w$])' + re.escape(local) + r'(?![\w$])', body):
                continue
            key = (file, module, level, name if not module else '')
            if key not in resolution_cache:
                imported_module = name if python and module == '' and name != '*' else module
                target = _resolve_module(Path(file), imported_module, level, root, indexed_paths, python)
                resolution_cache[key] = target
            target = resolution_cache[key]
            if target is not None:
                add(str(target), seed, 'import', line, name, 2)
        for route in idx.conn.execute('SELECT defined_in, line, path FROM routes WHERE handler_qname = ?', (row['qname'],)):
            add(route['defined_in'], seed, 'route', route['line'], route['path'], 3)

    ranked = sorted(candidates.values(), key=lambda x: (-x['_score'], x['file']))[:limit]
    for item in ranked:
        del item['_score']
    return ranked
