"""Offline only: which tests instantiate each sqlglot expression class.

Build-time dynamic map from the pinned commit (allowed: per-test runtime facts,
like coverage). `Expr.__new__` is wrapped in EVERY process (sitecustomize.py on
PYTHONPATH: main, pytest-xdist workers, spawned ProcessPoolExecutor workers) and
each instantiation is credited to the running test (a pytest plugin sets the
current test id; ProcessPoolExecutor jobs carry it to the worker). A test that
instantiates a subclass also counts for every expression base class in its MRO.
Classes instantiated at import time (credited to no test) are listed separately
so the selector can stay conservative for them.

Writes data/expr_inst.json.gz: {"tests": n, "classes": {name: hex bitmask over
data/map.json.gz test order}, "import_time": [names]}.
Usage (repo root; runs inside the blast-oracle image, ~1-2 min):
    python tools/build_expr_inst.py
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ORACLE_IMAGE = "blast-oracle"

SITECUSTOMIZE = r'''
import atexit, importlib.abc, importlib.machinery, json, os, sys, uuid
OUT = os.environ.get("INST_OUT")
CTX = ""
SEEN = {}  # ctx -> set of "module.qualname"


def _record(cls):
    SEEN.setdefault(CTX, set()).add(f"{cls.__module__}.{cls.__qualname__}")


def _run_ctx(ctx, fn, *args, **kwargs):  # executed inside pool workers
    global CTX
    prev, CTX = CTX, ctx
    try:
        return fn(*args, **kwargs)
    finally:
        CTX = prev


def _hook_new(base, tag):
    def __new__(cls, *args, **kwargs):
        SEEN.setdefault(CTX, set()).add(f"{tag}{cls.__module__}.{cls.__qualname__}")
        return object.__new__(cls)
    base.__new__ = __new__


# module -> base classes whose instances we record ("" tag: expressions, "D|": dialect use)
HOOKS = {"sqlglot.expressions.core": [("Expr", "")], "sqlglot.dialects.dialect": [("Dialect", "D|")],
         "sqlglot.parser": [("Parser", "D|")], "sqlglot.generator": [("Generator", "D|")],
         "sqlglot.tokens": [("Tokenizer", "D|")]}


def _patch(module):
    for attr, tag in HOOKS[module.__name__]:
        _hook_new(getattr(module, attr), tag)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name not in HOOKS:
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is None:
            return None
        orig = spec.loader.exec_module
        def exec_module(module):
            orig(module)
            _patch(module)
        spec.loader.exec_module = exec_module
        return spec


def _flush():
    if OUT and SEEN:
        with open(os.path.join(OUT, f"{os.getpid()}-{uuid.uuid4().hex[:8]}.json"), "w") as f:
            json.dump({k: sorted(v) for k, v in SEEN.items()}, f)


if OUT:
    sys.meta_path.insert(0, _Finder())
    atexit.register(_flush)
    try:
        from multiprocessing.util import Finalize
        Finalize(None, _flush, exitpriority=100)
    except Exception:
        pass
'''

PLUGIN = r'''
import concurrent.futures
import pytest
import sitecustomize as sc

_orig_submit = concurrent.futures.ProcessPoolExecutor.submit


def _submit(self, fn, /, *args, **kwargs):
    return _orig_submit(self, sc._run_ctx, sc.CTX, fn, *args, **kwargs)


concurrent.futures.ProcessPoolExecutor.submit = _submit


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    # importing a test module can run sqlglot (module-level parse_one etc.): if that
    # raises, the whole file fails to import -> credit it to "collect:<file>"
    if isinstance(collector, pytest.Module):
        sc.CTX = "collect:" + collector.nodeid
        yield
        sc.CTX = ""
    else:
        yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    sc.CTX = item.nodeid
    yield
    sc.CTX = ""
'''

INSIDE = r'''
import glob, gzip, importlib, json, os, shutil, subprocess, sys, time
OUT, PROBE = "/tmp/instout", "/tmp/instprobe"
for d in (OUT, PROBE):
    shutil.rmtree(d, ignore_errors=True); os.makedirs(d)
shutil.copy("/tools_probe/sitecustomize.py", PROBE)
shutil.copy("/tools_probe/inst_plugin.py", PROBE)
env = dict(os.environ, INST_OUT=OUT, PYTHONPATH=f"{PROBE}:/pristine", PYTHONDONTWRITEBYTECODE="1",
           SKIP_INTEGRATION="0", PYTHON_CPU_COUNT="4")
t = time.time()
r = subprocess.run([sys.executable, "-m", "pytest", "tests", "-n", "3", "--dist", "loadfile", "-p", "inst_plugin",
                    "-p", "no:cacheprovider", "-q", "-o", "timeout=0", "--no-header"],
                   cwd="/pristine", env=env, capture_output=True, text=True)
by_test, import_time, collect = {}, set(), {}
d_test, d_import, d_collect = {}, set(), {}
files = glob.glob(f"{OUT}/*.json")
for f in files:
    for ctx, names in json.load(open(f)).items():
        dn = {n[2:] for n in names if n.startswith("D|")}
        en = {n for n in names if not n.startswith("D|")}
        if ctx.startswith("collect:"):
            collect.setdefault(ctx[len("collect:"):], set()).update(en)
            d_collect.setdefault(ctx[len("collect:"):], set()).update(dn)
        elif ctx:
            by_test.setdefault(ctx, set()).update(en)
            d_test.setdefault(ctx, set()).update(dn)
        else:
            import_time.update(en)
            d_import.update(dn)
sys.path.insert(0, "/pristine")
from sqlglot.expressions.core import Expr
def resolve(qual):
    mod, _, rest = qual.rpartition(".")
    while mod:
        try:
            obj = importlib.import_module(mod)
            for part in rest.split("."):
                obj = getattr(obj, part)
            return obj
        except Exception:
            mod, _, head = mod.rpartition(".")
            rest = head + "." + rest
    return None
tests = json.loads(gzip.decompress(open("/data/map.json.gz", "rb").read()))["tests"]
index = {t: i for i, t in enumerate(tests)}
classes, unresolved = {}, set()
def names_for(qual):
    cls = resolve(qual)
    if cls is None:
        unresolved.add(qual)
        return {qual.rsplit(".", 1)[-1]}
    return {b.__name__ for b in cls.__mro__ if isinstance(b, type) and issubclass(b, Expr)}
cache = {}
for tid, quals in by_test.items():
    if tid not in index:
        continue
    bit = 1 << index[tid]
    for q in quals:
        if q not in cache:
            cache[q] = names_for(q)
        for name in cache[q]:
            classes[name] = classes.get(name, 0) | bit
imp = set()
for q in import_time:
    imp |= names_for(q) if q not in cache else cache[q]
collected = {}  # class name (+ bases) -> test files whose import instantiated it
for path, quals in collect.items():
    for q in quals:
        if q not in cache:
            cache[q] = names_for(q)
        for name in cache[q]:
            collected.setdefault(name, set()).add(path)
DPKGS = ("sqlglot.dialects.", "sqlglot.parsers.", "sqlglot.generators.", "sqlglot.typing.")
SHARED = {"__init__", "dialect", "base"}
def dialects_of(qual):
    cls = resolve(qual)
    if cls is None:
        return set()
    out = set()
    for b in cls.__mro__:
        mod = getattr(b, "__module__", "") or ""
        if mod.startswith(DPKGS) and mod.rsplit(".", 1)[-1] not in SHARED:
            out.add(mod.rsplit(".", 1)[-1])
    return out
dcache, dialect_active = {}, {}
for tid, quals in d_test.items():
    if tid not in index:
        continue
    bit = 1 << index[tid]
    for q in quals:
        if q not in dcache:
            dcache[q] = dialects_of(q)
        for d in dcache[q]:
            dialect_active[d] = dialect_active.get(d, 0) | bit
d_collected = {}
for path, quals in d_collect.items():
    for q in quals:
        for d in dcache.get(q) or dialects_of(q):
            d_collected.setdefault(d, set()).add(path)
d_imp = set()
for q in d_import:
    d_imp |= dialects_of(q)
out = {"tests": len(tests), "classes": {k: format(v, "x") for k, v in sorted(classes.items())},
       "dialect_active": {k: format(v, "x") for k, v in sorted(dialect_active.items())},
       "dialect_import_time": sorted(d_imp), "dialect_collected_in": {k: sorted(v) for k, v in sorted(d_collected.items())},
       "import_time": sorted(imp), "collected_in": {k: sorted(v) for k, v in sorted(collected.items())}}
with gzip.open("/out/expr_inst.json.gz", "wt") as f:
    json.dump(out, f, separators=(",", ":"))
def count(name):
    return bin(classes.get(name, 0)).count("1")
print(json.dumps({"pytest_exit": r.returncode, "tail": r.stdout.strip().splitlines()[-1:],
                  "minutes": round((time.time() - t) / 60, 1), "process_files": len(files),
                  "tests_with_records": len(by_test), "classes": len(classes), "import_time_classes": len(imp),
                  "unresolved": sorted(unresolved)[:5],
                  "TriggerProperties": count("TriggerProperties"), "Join": count("Join"),
                  "Select": count("Select"), "Expression": count("Expression"),
                  "files_instantiating_at_collection": len(collect),
                  "dialect_active_counts": {d: bin(v).count("1") for d, v in sorted(dialect_active.items())},
                  "dialect_import_time": sorted(d_imp), "dialect_collected_in": {k: sorted(v) for k, v in sorted(d_collected.items())},
                  "tpcds_actively_uses": sorted(d for d, v in dialect_active.items() if v >> index["tests/test_executor.py::TestExecutor::test_execute_tpcds"] & 1),
                  "Join_collected_in": sorted(collected.get("Join", ()))[:8]}, indent=1))
'''


def main():
    out = ROOT / "tools" / "out" / "inst"
    out.mkdir(parents=True, exist_ok=True)
    (out / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
    (out / "inst_plugin.py").write_text(PLUGIN, encoding="utf-8")
    (out / "inside.py").write_text(INSIDE, encoding="utf-8")
    cmd = ["docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none",
           "--memory", "5g", "--memory-swap", "5g", "--cpus", "8", "--pids-limit", "512",
           "-v", f"{out}:/tools_probe:ro", "-v", f"{out}:/out", "-v", f"{ROOT / 'data'}:/data:ro",
           ORACLE_IMAGE, "python", "/tools_probe/inside.py"]
    print("$ " + " ".join(cmd), file=sys.stderr)
    rc = subprocess.run(cmd).returncode
    src = out / "expr_inst.json.gz"
    if src.exists():
        dst = ROOT / "data" / "expr_inst.json.gz"
        dst.write_bytes(src.read_bytes())
        print(f"wrote {dst} ({dst.stat().st_size} bytes)", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
