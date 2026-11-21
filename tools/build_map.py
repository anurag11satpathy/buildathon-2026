"""Offline only: turn raw coverage (tools/out/) into the compact data/map.json.gz.

Usage: python3 tools/build_map.py   (after tools/collect.sh)

Map layout (all test references are indexes into "tests"; sets are stored once
as hex bitsets in "sets" and referenced by position):
  tests     : [test id, ...]                       (order of sample/tests.json)
  sets      : [hex bitset, ...]
  files     : {"sqlglot/x.py": {
                  "funcs":  [[body_start, body_end, qualname, set], ...],
                  "any":    set   # every test that ran any line of the file
                  "import": set   # tests whose context saw module/class-level lines
               }}
  refs      : {name: set}   # union over functions whose body reads `name`
                            # (bare name or attribute), for class/module-level edits
  fixtures  : {"tests/fixtures/x.sql": set}        (files opened by tests)
  sources   : {"sqlglot/x.py": set}   # tests that opened the .py during the test
                                      # (lazy import or reading source text)
  fixture_unattributed :["tests/...", ...]        (opened outside any test)
  testspans : {"tests/x.py": [[start, end, "Class::func"], ...]}
"""

import ast
import gzip
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "sqlglot-src"
OUT = ROOT / "tools" / "out"
MAP = ROOT / "data" / "map.json.gz"
TESTS_JSON = ROOT / "sample" / "tests.json"
SIZE_LIMIT = 5 * 1024 * 1024


def log(*a):
    print(*a, file=sys.stderr)


def read_coverage():
    """Return {relpath: {line: set(test ids)}} from every coverage SQLite DB
    (main process .coverage plus one .coverage.* per pool worker process)."""

    def numbits_to_nums(numbits):  # coverage's format: bit i of the blob = line i
        return [i * 8 + j for i, byte in enumerate(numbits) for j in range(8) if byte >> j & 1]

    result = defaultdict(lambda: defaultdict(set))
    dbs = sorted(OUT.glob(".coverage*"))
    log(f"coverage data files: {len(dbs)}")
    for path_db in dbs:
        db = sqlite3.connect(path_db)
        files = dict(db.execute("select id, path from file"))
        contexts = dict(db.execute("select id, context from context"))
        for file_id, ctx_id, numbits in db.execute("select file_id, context_id, numbits from line_bits"):
            path = files[file_id]
            rel = path.split("/src/", 1)[1] if "/src/" in path else path
            ctx = contexts[ctx_id]
            for line in numbits_to_nums(numbits):
                result[rel][line].add(ctx)
        db.close()
    return result


def top_level_defs(tree):
    """Yield (qualname, node) for module functions and methods (any class depth)."""
    def walk(body, prefix):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield prefix + node.name, node
            elif isinstance(node, ast.ClassDef):
                yield from walk(node.body, prefix + node.name + ".")
            elif isinstance(node, (ast.If, ast.Try, ast.With)):
                # defs under `if TYPE_CHECKING:` / try-import blocks
                for field in ("body", "orelse", "finalbody", "handlers"):
                    sub = getattr(node, field, [])
                    for s in sub:
                        yield from walk(getattr(s, "body", [s]) if isinstance(s, ast.ExceptHandler) else [s], prefix)
    yield from walk(tree.body, "")


def names_read(node):
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
    return out


def main():
    test_ids = [t["id"] for t in json.loads(TESTS_JSON.read_text())["tests"]]
    index = {tid: i for i, tid in enumerate(test_ids)}
    cov = read_coverage()

    seen_ctx = {c for lines in cov.values() for ctxs in lines.values() for c in ctxs if c}
    unknown = seen_ctx - index.keys()
    log(f"contexts: {len(seen_ctx)}, in tests.json: {len(seen_ctx & index.keys())}, unknown: {len(unknown)}")
    never = [t for t in test_ids if t not in seen_ctx]
    log(f"tests.json tests with no coverage context: {len(never)} {never[:5]}")

    def bits(ctxs):
        b = 0
        for c in ctxs:
            i = index.get(c)
            if i is not None:
                b |= 1 << i
        return b

    sets: list[str] = []
    set_ids: dict[int, int] = {}

    def sid(b):
        if b not in set_ids:
            set_ids[b] = len(sets)
            sets.append(format(b, "x"))
        return set_ids[b]

    files = {}
    refs_bits = defaultdict(int)
    for path in sorted((SRC / "sqlglot").rglob("*.py")):
        rel = str(path.relative_to(SRC))
        lines = cov.get(rel, {})
        tree = ast.parse(path.read_text(), filename=rel)
        funcs = []
        in_body = set()
        for qual, node in top_level_defs(tree):
            start, end = node.body[0].lineno, node.end_lineno
            # docstring-only first statement still counts; include whole body span
            b = 0
            for ln in range(start, end + 1):
                in_body.add(ln)
                if ln in lines:
                    b |= bits(lines[ln])
            funcs.append([start, end, qual, sid(b)])
            if b:
                for name in names_read(node):
                    refs_bits[name] |= b
        any_b = 0
        import_b = 0
        for ln, ctxs in lines.items():
            b = bits(ctxs)
            any_b |= b
            if ln not in in_body:
                import_b |= b
        files[rel] = {"funcs": funcs, "any": sid(any_b), "import": sid(import_b)}

    opens = json.loads((OUT / "opens.json").read_text())
    fixtures, unattributed, sources = {}, [], {}
    for rel, ctxs in sorted(opens.items()):
        if rel.startswith("sqlglot/") and rel.endswith(".py"):
            sources[rel] = sid(bits(ctxs))  # lazy imports + tests reading source text
            continue
        if not rel.startswith("tests/") or rel.endswith(".py"):
            continue
        b = bits(ctxs)
        if "" in ctxs:
            unattributed.append(rel)
        fixtures[rel] = sid(b)

    testspans = {}
    for path in sorted((SRC / "tests").rglob("test_*.py")):
        rel = str(path.relative_to(SRC))
        tree = ast.parse(path.read_text())
        spans = []
        for qual, node in top_level_defs(tree):
            spans.append([node.lineno - len(node.decorator_list), node.end_lineno, qual.replace(".", "::")])
        testspans[rel] = spans

    out = {
        "tests": test_ids,
        "sets": sets,
        "files": files,
        "refs": {k: sid(v) for k, v in sorted(refs_bits.items())},
        "fixtures": fixtures,
        "sources": sources,
        "fixture_unattributed": unattributed,
        "nocov": never,  # tests coverage never saw (subprocess / source-reading)
        "testspans": testspans,
    }
    MAP.parent.mkdir(exist_ok=True)
    raw = json.dumps(out, separators=(",", ":")).encode()
    MAP.write_bytes(gzip.compress(raw, 9, mtime=0))
    size = MAP.stat().st_size
    nfuncs = sum(len(f["funcs"]) for f in files.values())
    log(f"files={len(files)} funcs={nfuncs} sets={len(sets)} refs={len(out['refs'])} "
        f"fixtures={len(fixtures)} unattributed_fixtures={len(unattributed)}")
    log(f"map: {len(raw)/1e6:.2f} MB raw, {size/1e6:.2f} MB gzipped -> {MAP}")
    if size > SIZE_LIMIT:
        sys.exit(f"map too big: {size} bytes > {SIZE_LIMIT}")


if __name__ == "__main__":
    main()
