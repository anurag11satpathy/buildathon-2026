"""Offline only: `refs` restricted to code OUTSIDE the dialect packages.

data/map.json.gz's `refs` maps a name to tests that ran any function mentioning
it -- including dialect-module functions that ran only because a test's process
imported that dialect (class-body builder calls at import). For a class-body edit
in a dialect we want the other kind of reference: generic code reading a dialect's
attribute at run time (e.g. generator.py reads TSQL.CONVERT_FORMAT_MAPPING). This
table unions, per dialect-module class name X, the coverage of functions defined
outside the dialect modules that read `X.attr` / getattr(X, ...).

Derived from data/map.json.gz + the pinned source (no coverage re-run).
Writes data/refs_external.json.gz: {name: hex bitmask over map test order}.
Usage: python tools/build_refs_external.py [sqlglot-src] [data/map.json.gz] [data/refs_external.json.gz]
"""

import ast
import gzip
import json
import sys
from pathlib import Path

PKGS = ("dialects", "parsers", "generators", "typing")
SHARED = {"__init__", "dialect", "base"}


def is_dialect_module(rel):
    parts = rel.split("/")
    return len(parts) == 3 and parts[1] in PKGS and parts[2][:-3] not in SHARED


def top_level_defs(tree):  # same walk as tools/build_map.py
    def walk(body, prefix):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield prefix + node.name, node
            elif isinstance(node, ast.ClassDef):
                yield from walk(node.body, prefix + node.name + ".")
            elif isinstance(node, (ast.If, ast.Try, ast.With)):
                for field in ("body", "orelse", "finalbody", "handlers"):
                    for st in getattr(node, field, []):
                        yield from walk(getattr(st, "body", [st]) if isinstance(st, ast.ExceptHandler) else [st], prefix)
    yield from walk(tree.body, "")


def class_attr_reads(node):
    """Names X used as `X.attr` or `getattr(X, ...)`: reading a class's attributes.
    Bare uses (isinstance(d, (Athena, Presto)), imports, type checks) do not depend
    on the class body, so they are not counted."""
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
            out.add(n.value.id)
        elif isinstance(n, ast.Attribute) and isinstance(n.value, ast.Attribute):
            out.add(n.value.attr)  # dotted path: sqlglot.dialects.tsql.TSQL.CONVERT_FORMAT_MAPPING
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "getattr"                 and n.args and isinstance(n.args[0], ast.Name):
            out.add(n.args[0].id)
    return out


def main():
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "sqlglot-src")
    m = json.loads(gzip.decompress(Path(sys.argv[2] if len(sys.argv) > 2 else "data/map.json.gz").read_bytes()))
    out_path = Path(sys.argv[3] if len(sys.argv) > 3 else "data/refs_external.json.gz")

    # names worth storing: class names and assigned names inside dialect-module classes
    wanted = set()
    for rel in m["files"]:
        if is_dialect_module(rel):
            for node in ast.walk(ast.parse((src / rel).read_text(encoding="utf-8"))):
                if isinstance(node, ast.ClassDef):
                    wanted.add(node.name)
    bits = {}
    for rel, info in m["files"].items():
        # skip dialect modules and the packages' lazy-loading __init__.py files (they only
        # import modules and hand back classes; running them is "importing", not reading)
        parts = rel.split("/")
        if is_dialect_module(rel) or (len(parts) == 3 and parts[1] in PKGS and parts[2] == "__init__.py"):
            continue
        tree = ast.parse((src / rel).read_text(encoding="utf-8"))
        defs = dict(top_level_defs(tree))
        for _start, _end, qual, sid in info["funcs"]:
            node = defs.get(qual)
            if node is None:
                continue
            b = int(m["sets"][sid], 16)
            if not b:
                continue
            for name in class_attr_reads(node) & wanted:
                bits[name] = bits.get(name, 0) | b
    out = {k: format(v, "x") for k, v in sorted(bits.items())}
    with gzip.open(out_path, "wt") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"wrote {out_path} ({out_path.stat().st_size} bytes, {len(out)} names of {len(wanted)} wanted)", file=sys.stderr)


if __name__ == "__main__":
    main()
