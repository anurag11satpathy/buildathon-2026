"""Offline only: exact sqlglot lines executed by each of the slowest tests.

Function-level coverage selects a slow test whenever it ran *any* line of an edited
function. For the ~25 slowest tests (>= 0.45 s) we record the exact lines instead,
so a function-body edit on lines such a test never executes can drop it.

Each test runs in its own fresh process (so lazy initialisation it depends on is
traced under its own context). Tracing (sys.settrace, sqlglot/ frames only) is on only
while that test runs, in every process: the main process / pytest-xdist workers (a pytest
plugin around each test, setUp/setUpClass included) and spawned ProcessPoolExecutor
workers (jobs carry the test id). Writes data/heavy_lines.json.gz:
{"tests": {test_id: {rel_path: [line ranges]}}}.
Usage (repo root; runs inside the blast-oracle image, ~2 min):
    python tools/build_heavy_lines.py
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ORACLE_IMAGE = "blast-oracle"
MIN_SECONDS = 0.45

SITECUSTOMIZE = r'''
import atexit, json, os, sys, threading, uuid
OUT = os.environ.get("HEAVY_OUT")
HEAVY = set(filter(None, os.environ.get("HEAVY_TESTS", "").split("|")))
PKG = os.environ.get("HEAVY_PKG", "")
CTX = ""
LINES = {}  # ctx -> set((file, line))


def _local(frame, event, arg):
    if event == "line":
        LINES.setdefault(CTX, set()).add((frame.f_code.co_filename, frame.f_lineno))
    return _local


def _tracer(frame, event, arg):
    return _local if frame.f_code.co_filename.startswith(PKG) else None


def start(ctx):
    global CTX
    CTX = ctx
    if ctx in HEAVY:
        sys.settrace(_tracer)
        threading.settrace(_tracer)


def stop():
    global CTX
    sys.settrace(None)
    threading.settrace(None)
    CTX = ""


def _run_ctx(ctx, payload):  # inside pool workers
    # unpickling the job and pickling its result (sqlglot serde) happen inside the
    # traced region, exactly like a real worker does them around the call
    import pickle
    start(ctx)
    try:
        fn, args, kwargs = pickle.loads(payload)
        result = fn(*args, **kwargs)
        pickle.loads(pickle.dumps(result))
        return result
    finally:
        stop()


def _flush():
    if OUT and LINES:
        data = {ctx: sorted([f[len(PKG) - len("sqlglot/"):], l] for f, l in v) for ctx, v in LINES.items() if ctx}
        with open(os.path.join(OUT, f"{os.getpid()}-{uuid.uuid4().hex[:8]}.json"), "w") as fh:
            json.dump(data, fh)


if OUT:
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
    import pickle
    payload = pickle.dumps((fn, args, kwargs))  # pickled here, under the test's (traced) context
    return _orig_submit(self, sc._run_ctx, sc.CTX, payload)


concurrent.futures.ProcessPoolExecutor.submit = _submit


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    sc.start(item.nodeid)
    yield
    sc.stop()
'''

INSIDE = r'''
import glob, gzip, json, os, shutil, subprocess, sys, time
OUT, PROBE = "/tmp/heavyout", "/tmp/heavyprobe"
for d in (OUT, PROBE):
    shutil.rmtree(d, ignore_errors=True); os.makedirs(d)
shutil.copy("/tools_probe/sitecustomize.py", PROBE); shutil.copy("/tools_probe/heavy_plugin.py", PROBE)
tests = json.load(open("/data_sample/tests.json"))["tests"]
heavy = sorted(t["id"] for t in tests if t["seconds"] >= float(sys.argv[1]))
ONLY = [x for x in sys.argv[3].split("|") if x] if len(sys.argv) > 3 else []
JOBS = int(sys.argv[2]) if len(sys.argv) > 2 else 4
if ONLY:
    heavy = [t for t in heavy if t in ONLY]
env = dict(os.environ, HEAVY_OUT=OUT, HEAVY_TESTS="|".join(heavy), HEAVY_PKG="/pristine/sqlglot/",
           PYTHONPATH=f"{PROBE}:/pristine", PYTHONDONTWRITEBYTECODE="1", SKIP_INTEGRATION="0", PYTHON_CPU_COUNT="4")
t = time.time()
# each heavy test in its OWN fresh process (lazy initialisation it relies on - caches,
# tries, first-use setup - then runs under its own context), 4 at a time
from concurrent.futures import ThreadPoolExecutor
def one(tid):
    # each test writes its trace into its own directory, so a failed run's partial
    # trace can be discarded instead of merged
    d = os.path.join(OUT, str(abs(hash(tid))))
    os.makedirs(d, exist_ok=True)
    return tid, subprocess.run([sys.executable, "-m", "pytest", tid, "-p", "heavy_plugin", "-p", "no:cacheprovider",
                                "-q", "-o", "timeout=0", "--no-header"], cwd="/pristine", env=dict(env, HEAVY_OUT=d),
                               capture_output=True, text=True)
with ThreadPoolExecutor(JOBS) as ex:
    results = list(ex.map(one, heavy))
rc = {tid: res.returncode for tid, res in results}
bad = {tid: (res.returncode, res.stdout.strip().splitlines()[-2:]) for tid, res in results if res.returncode != 0}
class R: pass
r = R(); r.returncode = max(rc.values())
r.stdout = f"{sum(v == 0 for v in rc.values())}/{len(rc)} fresh-process runs exited 0"
merged = {}
for f in glob.glob(f"{OUT}/*/*.json"):
    for ctx, pairs in json.load(open(f)).items():
        if rc.get(ctx, 1) != 0:
            continue  # only traces from runs that exited cleanly
        m = merged.setdefault(ctx, {})
        for rel, ln in pairs:
            m.setdefault(rel, set()).add(ln)
def ranges(lines):
    out = []
    for ln in sorted(lines):
        if out and ln == out[-1][1] + 1:
            out[-1][1] = ln
        else:
            out.append([ln, ln])
    return out
data = {"tests": {ctx: {rel: ranges(v) for rel, v in sorted(files.items())} for ctx, files in sorted(merged.items())}}
if ONLY and os.path.exists("/data_cur/heavy_lines.json.gz"):  # merge re-traced tests into the existing file
    cur = json.loads(gzip.decompress(open("/data_cur/heavy_lines.json.gz", "rb").read()))
    cur["tests"].update(data["tests"])
    data = cur
with gzip.open("/out/heavy_lines.json.gz", "wt") as f:
    json.dump(data, f, separators=(",", ":"))
print(json.dumps({"pytest_exit": r.returncode, "tail": r.stdout, "nonzero_runs": bad, "heavy": len(heavy),
                  "traced": len(merged), "missing": sorted(set(heavy) - set(merged)),
                  "minutes": round((time.time() - t) / 60, 1),
                  "lines_per_test": {k.split("::")[-1]: sum(len(v) for v in files.values()) for k, files in merged.items()}}, indent=1))
'''


def main():
    # optional: python tools/build_heavy_lines.py <jobs> "<test id>|<test id>"  (re-trace + merge)
    jobs = sys.argv[1] if len(sys.argv) > 1 else "4"
    only = sys.argv[2] if len(sys.argv) > 2 else ""
    out = ROOT / "tools" / "out" / "heavy"
    out.mkdir(parents=True, exist_ok=True)
    (out / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
    (out / "heavy_plugin.py").write_text(PLUGIN, encoding="utf-8")
    (out / "inside.py").write_text(INSIDE, encoding="utf-8")
    cmd = ["docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none",
           "--memory", "5g", "--memory-swap", "5g", "--cpus", "8", "--pids-limit", "512",
           "-v", f"{out}:/tools_probe:ro", "-v", f"{out}:/out", "-v", f"{ROOT / 'sample'}:/data_sample:ro",
           "-v", f"{ROOT / 'data'}:/data_cur:ro",
           ORACLE_IMAGE, "python", "/tools_probe/inside.py", str(MIN_SECONDS), jobs, only]
    print("$ " + " ".join(cmd), file=sys.stderr)
    rc = subprocess.run(cmd).returncode
    src = out / "heavy_lines.json.gz"
    if src.exists():
        dst = ROOT / "data" / "heavy_lines.json.gz"
        dst.write_bytes(src.read_bytes())
        print(f"wrote {dst} ({dst.stat().st_size} bytes)", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
