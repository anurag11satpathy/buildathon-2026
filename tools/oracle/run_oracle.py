"""Runs inside the oracle image. For every /mut/commits/<id>.patch not yet in
/mut/oracle.json: copy pristine sqlglot, git-apply the patch, run the full
pytest suite and record the failing test numbers. Resumable: re-running skips
commits already recorded.

Status per commit: ok | apply_failed | crash (whole suite broken: every test
counts as failing) | timeout (excluded from scoring).
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

MUT = Path("/mut")
PRISTINE = Path("/pristine")
WORK = Path("/work")
FAILOUT = Path("/tmp/fail.json")
WORKERS = os.environ.get("ORACLE_WORKERS", "12")
SUITE_TIMEOUT = int(os.environ.get("ORACLE_TIMEOUT", "900"))


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def main():
    tests = json.loads((MUT / "tests.json").read_text())["tests"]
    number_of = {t["id"]: t["number"] for t in tests}
    by_file: dict[str, list[int]] = {}
    for t in tests:
        by_file.setdefault(t["file"], []).append(t["number"])

    out_path = MUT / "oracle.json"
    results = json.loads(out_path.read_text()) if out_path.exists() else {}
    only = set(filter(None, os.environ.get("ONLY", "").split(",")))
    patches = sorted((MUT / "commits").glob("*.patch"))
    todo = [p for p in patches if p.stem not in results and (not only or p.stem in only)]
    log(f"{len(todo)} patches to run ({len(patches)} total)")

    for i, patch in enumerate(todo, 1):
        t0 = time.time()
        if WORK.exists():
            shutil.rmtree(WORK)
        shutil.copytree(PRISTINE, WORK, symlinks=True)
        ap = subprocess.run(["git", "apply", "--whitespace=nowarn", str(patch)],
                            cwd=WORK, capture_output=True, text=True)
        if ap.returncode != 0:
            results[patch.stem] = {"status": "apply_failed", "failing": [], "error": ap.stderr[-500:]}
            log(f"[{i}/{len(todo)}] {patch.stem}: APPLY FAILED {ap.stderr.strip()[:200]}")
            out_path.write_text(json.dumps(results, indent=1))
            continue

        FAILOUT.unlink(missing_ok=True)
        # PYTHON_CPU_COUNT caps the ProcessPoolExecutors that test_optimizer/test_executor
        # start (default size = CPU count, per xdist worker) so they cannot exhaust the VM.
        env = dict(os.environ, PYTHONPATH=f"{WORK}:/oracle", FAILOUT=str(FAILOUT), PYTHON_CPU_COUNT="4")
        cmd = [sys.executable, "-m", "pytest", "tests", "-n", WORKERS, "--dist", "loadfile", "-p", "failplugin",
               "-p", "no:cacheprovider", "-q", "-o", "timeout=300", "--no-header", "-rN"]
        try:
            r = subprocess.run(cmd, cwd=WORK, env=env, capture_output=True, text=True,
                               timeout=SUITE_TIMEOUT)
        except subprocess.TimeoutExpired:
            results[patch.stem] = {"status": "timeout", "failing": []}
            log(f"[{i}/{len(todo)}] {patch.stem}: SUITE TIMEOUT")
            out_path.write_text(json.dumps(results, indent=1))
            continue

        rep = json.loads(FAILOUT.read_text()) if FAILOUT.exists() else None
        failing, unknown, status = set(), [], "ok"
        if rep is None or rep["exit"] not in (0, 1):
            status, failing = "crash", set(number_of.values())
            log(r.stdout[-1500:])
        else:
            for nid in rep["failed"]:
                if nid in number_of:
                    failing.add(number_of[nid])
                else:
                    unknown.append(nid)
            for nid in rep["collect_errors"]:  # a file that no longer imports: all its tests fail
                failing |= set(by_file.get(nid.split("::", 1)[0], []))
        results[patch.stem] = {"status": status, "failing": sorted(failing),
                               "unknown_ids": unknown, "sec": round(time.time() - t0, 1)}
        log(f"[{i}/{len(todo)}] {patch.stem}: {status}, {len(failing)} failing, "
            f"{time.time() - t0:.0f}s" + (f", unmapped ids {unknown[:3]}" if unknown else ""))
        out_path.write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
