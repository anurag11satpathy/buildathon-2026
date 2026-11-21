"""Offline only: which dialects inherit from which (pinned sqlglot).

A class-body edit in a parent dialect (e.g. MySQL) changes every child dialect
that inherits the attribute (SingleStore, Doris, StarRocks, ...). For each
dialect name, records all transitive children, using class inheritance across
sqlglot/{dialects,parsers,generators,typing}/<name>.py (e.g. `class
SingleStore(MySQL)`, `class SingleStoreParser(MySQLParser)`).

Writes data/dialect_children.json: {"mysql": ["doris", "singlestore", "starrocks"], ...}
Usage, from the repo root:
    python tools/build_dialect_children.py [sqlglot-src] [data/dialect_children.json]
"""

import ast
import json
import sys
from pathlib import Path

PKGS = ("dialects", "parsers", "generators", "typing")
SHARED = {"__init__", "dialect", "base"}


def main():
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "sqlglot-src")
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "data/dialect_children.json")
    defined = {}  # class name -> dialect module name that defines it
    bases = []    # (child module, base class name)
    for pkg in PKGS:
        for p in sorted((src / "sqlglot" / pkg).glob("*.py")):
            if p.stem in SHARED:
                continue
            # top-level classes only: nested Tokenizer/Parser/Generator names repeat in every module
            for node in ast.parse(p.read_text(encoding="utf-8")).body:
                if isinstance(node, ast.ClassDef):
                    defined.setdefault(node.name, p.stem)
                    for b in node.bases:
                        name = b.id if isinstance(b, ast.Name) else b.attr if isinstance(b, ast.Attribute) else None
                        if name:
                            bases.append((p.stem, name))
    direct = {}
    for child, base in bases:
        parent = defined.get(base)
        if parent and parent != child:
            direct.setdefault(parent, set()).add(child)
    result = {}
    for d in direct:
        seen, stack = set(), list(direct[d])
        while stack:
            c = stack.pop()
            if c not in seen:
                seen.add(c)
                stack.extend(direct.get(c, ()))
        result[d] = sorted(seen)
    out.write_text(json.dumps(result, separators=(",", ":"), sort_keys=True))
    for d, kids in sorted(result.items()):
        print(f"{d} -> {kids}", file=sys.stderr)


if __name__ == "__main__":
    main()
