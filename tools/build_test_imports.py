"""Offline only: static import graph between files under tests/ (pinned sqlglot).

A test file can be a library for other test files: tests/dialects/test_dialect.py
defines the Validator base class that 33 other dialect test files import and
subclass. An edit to such a file can break tests in every importer, so the
selector needs, per test file:

  importers  : every file under tests/ that imports it, transitively
  subclassed : its classes that some importing file uses as a base class
               (their test* methods are inherited, so they run in importers too)

Writes data/test_imports.json:  {"tests/x.py": {"importers": [...], "subclassed": [...]}}
(only files that have at least one importer). Usage, from the repo root:
    python tools/build_test_imports.py [sqlglot-src] [data/test_imports.json]
"""

import ast
import json
import sys
from pathlib import Path


def module_to_file(src: Path, module: str):
    base = src / Path(*module.split("."))
    for cand in (base.with_suffix(".py"), base / "__init__.py"):
        if cand.exists():
            return cand.relative_to(src).as_posix()
    return None


def main():
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "sqlglot-src")
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "data/test_imports.json")
    files = sorted(p.relative_to(src).as_posix() for p in (src / "tests").rglob("*.py"))

    imports = {}  # file -> set of test files it imports
    bases = {}    # file -> {imported name -> source file} used as class bases
    for rel in files:
        tree = ast.parse((src / rel).read_text(encoding="utf-8"))
        pkg = rel.rsplit("/", 1)[0].replace("/", ".")
        deps, names = set(), {}
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.ImportFrom):
                if node.level:  # relative import
                    parts = pkg.split(".")[: len(pkg.split(".")) - node.level + 1]
                    mod = ".".join(parts + ([node.module] if node.module else []))
                else:
                    mod = node.module or ""
                if mod.startswith("tests"):
                    target = module_to_file(src, mod)
                    if target:
                        mods.append(target)
                        for a in node.names:
                            names[a.asname or a.name] = target
                    for a in node.names:  # "from tests.dialects import test_dialect"
                        sub = module_to_file(src, f"{mod}.{a.name}")
                        if sub:
                            mods.append(sub)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith("tests"):
                        target = module_to_file(src, a.name)
                        if target:
                            mods.append(target)
            deps.update(m for m in mods if m != rel)
        imports[rel] = deps
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for b in node.bases:
                    if isinstance(b, ast.Name) and b.id in names:
                        used.add((names[b.id], b.id))
        bases[rel] = used

    # reverse + transitive closure: who (directly or indirectly) imports each file
    direct = {f: {g for g in files if f in imports[g]} for f in files}
    result = {}
    for f in files:
        seen, stack = set(), list(direct[f])
        while stack:
            g = stack.pop()
            if g not in seen:
                seen.add(g)
                stack.extend(direct[g])
        if seen:
            sub = sorted({name for g in seen for (srcf, name) in bases[g] if srcf == f})
            result[f] = {"importers": sorted(seen), "subclassed": sub}
    out.write_text(json.dumps(result, separators=(",", ":"), sort_keys=True))
    for f, v in result.items():
        print(f"{f}: {len(v['importers'])} importers, subclassed={v['subclassed']}", file=sys.stderr)


if __name__ == "__main__":
    main()
