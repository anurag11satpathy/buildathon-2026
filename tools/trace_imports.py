"""Offline only: record which lines of sqlglot functions run at IMPORT time.

Coverage credits import-time execution to no test (or to whichever test first
imported a module), so a change to a helper that runs while modules/classes are
being built (e.g. _convert_quotes, new_trie, builder factories called in class
bodies) looks untested in data/map.json.gz. This script traces, on the pinned
sqlglot source:

  core     : function-body lines executed while importing every non-dialect
             module of the sqlglot package
  dialects : for each sqlglot/dialects/<name>.py, function-body lines executed
             while importing that dialect (which pulls in its parser,
             generator and typing modules) on top of the already-imported core

Only lines inside function bodies known to the map are kept (module and class
body lines are handled by the selector's module-level rule). Output is a small
JSON of line ranges: data/import_funcs.json.

Run inside the selector image (same Python as the scorer):
  docker run --rm -v "<repo>/tools:/tools:ro" -v "<repo>/data:/out" blast-radius-entry \
      python /tools/trace_imports.py --src /app/src --map /out/map.json.gz --out /out/import_funcs.json
"""

import argparse
import gzip
import importlib
import json
import subprocess
import sys
from pathlib import Path

DIALECT_PKGS = ("dialects", "parsers", "generators", "typing")
SHARED = {"__init__", "dialect", "base"}  # shared modules inside those packages


def module_names(pkg_dir: Path):
    core, dialects = [], []
    for p in sorted(pkg_dir.rglob("*.py")):
        rel = p.relative_to(pkg_dir.parent).with_suffix("")
        parts = rel.parts  # ('sqlglot', 'dialects', 'duckdb')
        if parts[-1] == "__main__":
            continue
        if len(parts) == 3 and parts[1] in DIALECT_PKGS and parts[2] not in SHARED:
            if parts[1] == "dialects":
                dialects.append(parts[2])
            continue
        mod = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
        core.append(mod)
    return core, dialects


def run_unit(src: Path, unit: str):
    """In a fresh interpreter: import core (traced if unit == 'core'), then the dialect."""
    sys.path.insert(0, str(src))
    pkg = str(src / "sqlglot")
    core, _ = module_names(src / "sqlglot")
    hits = set()

    def local(frame, event, arg):
        if event == "line":
            hits.add((frame.f_code.co_filename, frame.f_lineno))
        return local

    def tracer(frame, event, arg):
        co = frame.f_code
        if co.co_name == "<module>" or not co.co_filename.startswith(pkg):
            return None
        return local

    def import_all(mods):
        for mod in mods:
            try:
                importlib.import_module(mod)
            except Exception as e:  # optional deps etc.: note it, keep going
                print(f"warn: import {mod}: {e!r}", file=sys.stderr)

    if unit == "core":
        sys.settrace(tracer)
        import_all(core)
        sys.settrace(None)
    else:
        import_all(core)
        sys.settrace(tracer)
        import_all([f"sqlglot.dialects.{unit}"])
        sys.settrace(None)
    out = {}
    for fn, ln in hits:
        out.setdefault(str(Path(fn).relative_to(src)).replace("\\", "/"), []).append(ln)
    json.dump(out, sys.stdout)


def ranges(lines):
    out, lines = [], sorted(lines)
    for ln in lines:
        if out and ln == out[-1][1] + 1:
            out[-1][1] = ln
        else:
            out.append([ln, ln])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--map")
    ap.add_argument("--out")
    ap.add_argument("--unit")
    a = ap.parse_args()
    src = Path(a.src)
    if a.unit:
        return run_unit(src, a.unit)

    m = json.loads(gzip.decompress(Path(a.map).read_bytes()))
    spans = {rel: [(s, e) for s, e, _, _ in info["funcs"]] for rel, info in m["files"].items()}

    def in_func(rel, ln):
        return any(s <= ln <= e for s, e in spans.get(rel, ()))

    def unit_lines(unit):
        r = subprocess.run([sys.executable, __file__, "--src", str(src), "--unit", unit],
                           capture_output=True, text=True, check=True)
        if r.stderr.strip():
            print(f"[{unit}] {r.stderr.strip()[:300]}", file=sys.stderr)
        raw = json.loads(r.stdout)
        return {rel: {ln for ln in lns if in_func(rel, ln)} for rel, lns in raw.items()}

    _, dialects = module_names(src / "sqlglot")
    core = unit_lines("core")
    per_line = {}  # rel -> line -> set(dialects)
    for d in dialects:
        for rel, lns in unit_lines(d).items():
            for ln in lns - core.get(rel, set()):
                per_line.setdefault(rel, {}).setdefault(ln, set()).add(d)
        print(f"traced dialect {d}", file=sys.stderr)

    out = {"core": {rel: ranges(l) for rel, l in sorted(core.items()) if l}, "dialects": {}}
    for rel, lines in sorted(per_line.items()):
        groups = {}
        for ln, ds in lines.items():
            groups.setdefault(",".join(sorted(ds)), []).append(ln)
        out["dialects"][rel] = [[s, e, key.split(",")] for key, lns in groups.items() for s, e in ranges(lns)]
    Path(a.out).write_text(json.dumps(out, separators=(",", ":"), sort_keys=True))
    n_core = sum(len(v) for v in out["core"].values())
    print(f"core: {len(out['core'])} files, {n_core} ranges; dialect-specific: {len(out['dialects'])} files; "
          f"{len(dialects)} dialects", file=sys.stderr)


if __name__ == "__main__":
    main()
