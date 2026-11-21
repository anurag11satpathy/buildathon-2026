"""Offline only: build-time comparison profile -> data/safe_cmps.json.gz.

Proves, for the fixed pinned test suite, which comparison sites in sqlglot/ can
swap operator (is<->==, is not<->!=, <<->=, ><->=) without changing ANY test:
every 2-operand comparison is instrumented (in memory, at import time, keyed by
its pinned (path, line, col)), the full suite runs, and each execution records
whether the swapped operator gives the identical bool. Only sites that ran at
least once, always agreed, and only ever saw side-effect-free operand types are
kept. The suite is deterministic (no random/hypothesis/uuid inputs), so an edit
that is exactly such a swap at such a site leaves every test's execution identical.

Every process records: a sitecustomize.py on PYTHONPATH installs the import hook
in the main process, pytest-xdist workers, spawned ProcessPoolExecutor workers
and the `python -c "import sqlglot"` subprocess of test_lazy_load; each writes
its stats at exit (atexit + multiprocessing Finalize).

Usage (from the repo root; runs inside the blast-oracle image, ~15-30 min):
    python tools/build_cmp_profile.py
"""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ORACLE_IMAGE = "blast-oracle"

PROBE = r'''
import ast, atexit, builtins, enum, json, operator, os, sys, uuid
import importlib.machinery as _m

ROOT = os.environ["CMP_ROOT"].rstrip("/") + "/"
PKG = ROOT + "sqlglot/"
OUT = os.environ["CMP_OUT"]
OPS = {"Is": operator.is_, "IsNot": operator.is_not, "Eq": operator.eq, "NotEq": operator.ne,
       "Lt": operator.lt, "LtE": operator.le, "Gt": operator.gt, "GtE": operator.ge}
SWAP = {"Is": "Eq", "Eq": "Is", "IsNot": "NotEq", "NotEq": "IsNot",
        "Lt": "LtE", "LtE": "Lt", "Gt": "GtE", "GtE": "Gt"}
BASIC = {type(None), bool, int, float, str, bytes}
ORDER_OK = {(int, int), (float, float), (int, float), (float, int), (str, str)}
STATS = {}  # site -> [count, equiv_ok, types_safe, {type pairs}, singleton_ok]


def _singleton(t):
    # identity == equality for these regardless of interning: None, bools, enum members, classes
    return t is type(None) or t is bool or t is type or (isinstance(t, type) and issubclass(t, enum.Enum))


def _eq_safe(t):
    if t in BASIC or t is type:
        return True
    mod = getattr(t, "__module__", "") or ""
    if mod.split(".")[0] in ("numpy", "pandas", "unittest", "mock"):
        return False
    if isinstance(t, type) and issubclass(t, enum.Enum):
        return True
    return getattr(t, "__eq__", None) is object.__eq__ and getattr(t, "__ne__", None) is object.__ne__


def _pair_safe(op, tl, tr):
    if op in ("Lt", "LtE", "Gt", "GtE"):
        return (tl, tr) in ORDER_OK
    return _eq_safe(tl) and _eq_safe(tr)


def probe(site, op, left, right):
    res = OPS[op](left, right)
    st = STATS.get(site)
    if st is None:
        st = STATS[site] = [0, True, True, set(), True]
    st[0] += 1
    if st[1] and st[2]:
        key = (type(left), type(right))
        if key not in st[3]:
            st[3].add(key)
            if not _pair_safe(op, *key):
                st[2] = False
            if op in ("Eq", "NotEq") and not (_singleton(key[0]) or _singleton(key[1])):
                st[4] = False  # value->identity swap would depend on str/int interning
        if st[2]:
            try:
                alt = OPS[SWAP[op]](left, right)
                if not (type(res) is bool and type(alt) is bool and res is alt):
                    st[1] = False
            except BaseException:
                st[1] = False
    return res


builtins.__cmp_probe__ = probe


class _Instrument(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) == 1 and len(node.comparators) == 1 and type(node.ops[0]).__name__ in OPS:
            site = f"{self.rel}:{node.lineno}:{node.col_offset}"
            call = ast.Call(func=ast.Name(id="__cmp_probe__", ctx=ast.Load()),
                            args=[ast.Constant(site), ast.Constant(type(node.ops[0]).__name__),
                                  node.left, node.comparators[0]], keywords=[])
            return ast.copy_location(call, node)
        return node


_orig = _m.SourceFileLoader.source_to_code


def _source_to_code(self, data, path, *a, **k):
    p = os.fspath(path)
    if p.startswith(PKG) and p.endswith(".py"):
        tree = ast.parse(data, filename=p)
        tree = ast.fix_missing_locations(_Instrument(p[len(ROOT):]).visit(tree))
        return compile(tree, p, "exec", dont_inherit=True)
    return _orig(self, data, path, *a, **k)


_m.SourceFileLoader.source_to_code = _source_to_code
_written = False


def _flush():
    global _written
    if _written or not STATS:
        return
    _written = True
    data = {s: [c, ok, ts, sorted(f"{a.__module__}.{a.__qualname__}|{b.__module__}.{b.__qualname__}" for a, b in tp)[:6], so]
            for s, (c, ok, ts, tp, so) in STATS.items()}
    with open(os.path.join(OUT, f"{os.getpid()}-{uuid.uuid4().hex[:8]}.json"), "w") as f:
        json.dump(data, f)


atexit.register(_flush)
try:
    from multiprocessing.util import Finalize
    Finalize(None, _flush, exitpriority=100)
except Exception:
    pass
'''

INSIDE = r'''
import ast, glob, gzip, json, os, shutil, subprocess, sys, time
BUILD, OUT, PROBE_DIR = "/tmp/cmpbuild", "/tmp/cmpout", "/tmp/cmpprobe"
for d in (BUILD, OUT, PROBE_DIR):
    shutil.rmtree(d, ignore_errors=True)
shutil.copytree("/pristine", BUILD, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
os.makedirs(OUT); os.makedirs(PROBE_DIR)
open(f"{PROBE_DIR}/sitecustomize.py", "w").write(open("/tools_probe/probe.py").read())
env = dict(os.environ, CMP_ROOT=BUILD, CMP_OUT=OUT, PYTHONPATH=f"{PROBE_DIR}:{BUILD}",
           PYTHONDONTWRITEBYTECODE="1", SKIP_INTEGRATION="0", PYTHON_CPU_COUNT="4")
t = time.time()
r = subprocess.run([sys.executable, "-m", "pytest", "tests", "-n", "3", "--dist", "loadfile",
                    "-p", "no:cacheprovider", "-q", "-o", "timeout=0", "--no-header", "-rfE"],
                   cwd=BUILD, env=env, capture_output=True, text=True)
tail = r.stdout.strip().splitlines()[-15:]
merged = {}
files = glob.glob(f"{OUT}/*.json")
for f in files:
    for site, (c, ok, ts, tps, so) in json.load(open(f)).items():
        m = merged.setdefault(site, [0, True, True, set(), True])
        m[0] += c; m[1] = m[1] and ok; m[2] = m[2] and ts; m[3].update(tps); m[4] = m[4] and so
safe = {}
for site, (c, ok, ts, _, so) in merged.items():
    if c > 0 and ok and ts:
        rel, line, col = site.rsplit(":", 2)
        safe[site] = None
# op name per safe site, from the pinned AST (selector checks old op -> its swap partner)
ops = {}
for site in safe:
    rel, line, col = site.rsplit(":", 2)
    ops.setdefault(rel, set()).add((int(line), int(col)))
result = {}
for rel, pos in ops.items():
    for node in ast.walk(ast.parse(open(f"/pristine/{rel}", encoding="utf-8").read())):
        if isinstance(node, ast.Compare) and (node.lineno, node.col_offset) in pos and len(node.ops) == 1:
            op = type(node.ops[0]).__name__
            key = f"{rel}:{node.lineno}:{node.col_offset}"
            if op in ("Eq", "NotEq") and not merged[key][4]:
                continue  # ==/!= -> is/is not only where a singleton is always involved
            result[key] = op
def sites_in(rel, first, last):
    return {s: merged[s][:3] for s in merged if s.startswith(rel + ":") and first <= int(s.rsplit(":", 2)[1]) <= last}
report = {
    "pytest_exit": r.returncode, "pytest_tail": tail, "minutes": round((time.time() - t) / 60, 1),
    "process_files": len(files), "sites_executed": len(merged), "sites_safe": len(result),
    "case_sensitive_sites(2haz)": sites_in("sqlglot/dialects/dialect.py", 1271, 1283),
    "context_table_sites(66ks)": sites_in("sqlglot/executor/context.py", 52, 62),
}
with gzip.open("/out/safe_cmps.json.gz", "wt") as f:
    json.dump(result, f, separators=(",", ":"), sort_keys=True)
json.dump(report, open("/out/cmp_report.json", "w"), indent=1)
print(json.dumps(report, indent=1))
'''


def main():
    out = ROOT / "tools" / "out" / "cmp"
    out.mkdir(parents=True, exist_ok=True)
    (out / "probe.py").write_text(PROBE, encoding="utf-8")
    (out / "inside.py").write_text(INSIDE, encoding="utf-8")
    cmd = ["docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none",
           "--memory", "5g", "--memory-swap", "5g", "--cpus", "8", "--pids-limit", "512",
           "-v", f"{out}:/tools_probe:ro", "-v", f"{out}:/out", ORACLE_IMAGE, "python", "/tools_probe/inside.py"]
    print("$ " + " ".join(cmd), file=sys.stderr)
    rc = subprocess.run(cmd).returncode
    if (out / "safe_cmps.json.gz").exists():
        target = ROOT / "data" / "safe_cmps.json.gz"
        target.write_bytes((out / "safe_cmps.json.gz").read_bytes())
        print(f"wrote {target} ({target.stat().st_size} bytes)", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
