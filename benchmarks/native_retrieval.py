"""Compare native retrieval on an external, private scenario manifest.

Run each revision in a fresh process against the same already-refreshed indexes.
Manifest: [{"id": "case-1", "project": "Project A", "root": "/path/to/repo",
            "question": "Where is authentication checked?",
            "truth": ["src/auth.py"]}]
Output omits project paths, queries, source, and filenames; matched references
are represented by their positions in the manifest. No index refresh or API
request is performed. Warmup is excluded from timing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
from pathlib import Path

from snapctx.api import context, search_code


def _files(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ('file', 'defined_in') and isinstance(child, str):
                yield child
            else:
                yield from _files(child)
    elif isinstance(value, list):
        for child in value:
            yield from _files(child)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--related', action='store_true')
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error('--repeats must be positive')
    cases = json.loads(args.manifest.read_text())
    rows = []
    modes = ['search', 'context'] + (['related'] if args.related else [])
    for project in dict.fromkeys(c['project'] for c in cases):
        subset = [c for c in cases if c['project'] == project]
        roots = {str(Path(c['root']).resolve()) for c in subset}
        if len(roots) != 1:
            raise ValueError('Each project label must identify one root')
        root = Path(roots.pop())
        for case in subset:
            if not case['truth']:
                raise ValueError('Reference sets must not be empty')
            for file in case['truth']:
                path = (root / file).resolve()
                if not path.is_relative_to(root) or not path.is_file():
                    raise ValueError('Reference file missing or outside root')
        search_code('warmup', root=root, k=1)
        for repeat in range(args.repeats):
            for case in subset:
                for mode in modes:
                    start = time.perf_counter()
                    if mode == 'search':
                        result = search_code(case['question'], root=root, k=12)
                    else:
                        options = {'related_file_limit': 4} if mode == 'related' else {}
                        result = context(case['question'], root=root, **options)
                    elapsed = (time.perf_counter() - start) * 1000
                    files = set(_files(result))
                    matched = [i for i, f in enumerate(case['truth']) if str(root / f) in files]
                    encoded = json.dumps(result, sort_keys=True).encode()
                    rows.append({
                        'project': project, 'id': case['id'], 'repeat': repeat, 'mode': mode,
                        'ms': elapsed, 'bytes': len(json.dumps(result).encode()),
                        'recall': len(matched) / len(case['truth']),
                        'matched_reference_indices': matched,
                        'payload_sha256': hashlib.sha256(encoded).hexdigest(),
                    })
        args.output.write_text(json.dumps(rows, indent=2) + '\n')
    for project in dict.fromkeys(c['project'] for c in cases):
        for mode in modes:
            group = [r for r in rows if r['project'] == project and r['mode'] == mode]
            print(json.dumps({
                'project': project, 'mode': mode,
                'median_ms': statistics.median(r['ms'] for r in group),
                'mean_recall': statistics.mean(r['recall'] for r in group),
                'mean_bytes': statistics.mean(r['bytes'] for r in group),
            }))


if __name__ == '__main__':
    main()
