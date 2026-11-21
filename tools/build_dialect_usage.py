"""Offline only: which tests use which dialect (static text scan of pinned tests).

A class-body edit in a dialect module (e.g. Snowflake.Tokenizer.NUMERIC_ESCAPES)
only changes behaviour for code running with that dialect (or a dialect that
inherits from / reuses it). Coverage cannot tell us which tests *use* a dialect
(most dialect behaviour runs in shared base-class code), so we scan the pinned
tests' source text for the dialect's name. Over-inclusion is harmless.

A test uses dialect D if its name (word, case-insensitive) appears in:
  - the test method's own lines, or
  - its class body outside test methods (e.g. `dialect = "snowflake"`), or
  - module-level code of its test file, or
  - a fixture file the test opens (optimizer fixtures carry `# dialect: ...`).
Tests that iterate over every dialect (`Dialects`, `Dialect.classes`) are
recorded under "__all__".

Writes data/dialect_usage.json: {"snowflake": [map test index, ...], "__all__": [...]}
where indices refer to data/map.json.gz "tests" order.
Usage: python tools/build_dialect_usage.py [sqlglot-src] [data/map.json.gz] [data/dialect_usage.json]
"""

import ast
import gzip
import json
import re
import sys
from pathlib import Path

ALL_PATTERN = re.compile(r"\bDialects\b|Dialect\.classes|\._classes\b")


def main():
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "sqlglot-src")
    m = json.loads(gzip.decompress(Path(sys.argv[2] if len(sys.argv) > 2 else "data/map.json.gz").read_bytes()))
    out_path = Path(sys.argv[3] if len(sys.argv) > 3 else "data/dialect_usage.json")

    names = sorted(p.stem for p in (src / "sqlglot" / "dialects").glob("*.py") if p.stem not in ("__init__", "dialect"))
    patterns = {d: re.compile(rf"\b{re.escape(d)}\b", re.I) for d in names}
    index = {tid: i for i, tid in enumerate(m["tests"])}

    def dialects_in(text):
        found = {d for d, p in patterns.items() if p.search(text)}
        if ALL_PATTERN.search(text):
            found.add("__all__")
        return found

    usage = {d: set() for d in names + ["__all__"]}
    by_file = {}
    for tid in m["tests"]:
        by_file.setdefault(tid.split("::", 1)[0], []).append(tid)

    for rel, tids in by_file.items():
        path = src / rel
        if not path.exists():
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        spans = m["testspans"].get(rel, [])
        tree = ast.parse("\n".join(lines))
        in_test = set()
        for s, e, qual in spans:
            if qual.rsplit("::", 1)[-1].startswith("test"):
                in_test.update(range(s, e + 1))
        # class-level text (outside test methods) per top-level class; rest is module-level
        class_text, class_lines = {}, set()
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                rng = range(node.lineno, node.end_lineno + 1)
                class_lines.update(rng)
                class_text[node.name] = "\n".join(lines[i - 1] for i in rng if i not in in_test)
        module_text = "\n".join(l for i, l in enumerate(lines, 1) if i not in class_lines)
        file_wide = dialects_in(module_text)
        for tid in tids:
            parts = tid.split("::")
            cls = parts[1] if len(parts) > 2 else None
            qual = "::".join(parts[1:3]) if len(parts) > 2 else parts[1]
            found = set(file_wide) | dialects_in(class_text.get(cls, ""))
            for s, e, q in spans:
                if q == qual:
                    found |= dialects_in("\n".join(lines[s - 1:e]))
            for d in found:
                usage[d].add(index[tid])

    # fixture files: tests that open a fixture mentioning the dialect
    for rel, sid in m["fixtures"].items():
        p = src / rel
        if not p.exists() or p.suffix == ".gz":
            continue
        found = dialects_in(p.read_text(encoding="utf-8", errors="replace"))
        if not found:
            continue
        b = int(m["sets"][sid], 16)
        members = {i for i in range(len(m["tests"])) if b >> i & 1}
        for d in found:
            usage[d] |= members

    out = {d: sorted(v) for d, v in usage.items() if v}
    out_path.write_text(json.dumps(out, separators=(",", ":"), sort_keys=True))
    for d, v in sorted(out.items(), key=lambda kv: -len(kv[1]))[:8]:
        print(f"{d}: {len(v)} tests", file=sys.stderr)
    print(f"wrote {out_path} ({out_path.stat().st_size} bytes, {len(out)} keys)", file=sys.stderr)


if __name__ == "__main__":
    main()
