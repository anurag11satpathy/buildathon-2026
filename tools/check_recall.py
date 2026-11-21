"""Offline only: check data/map.json.gz against the sample answers.

For every sample patch, map changed lines -> functions / fixtures / test
functions using the map (a prototype of the step-3 selector logic) and report
whether every known failing test is selected, how many tests are selected,
and the packed cost.

Usage: python3 tools/check_recall.py
"""

import gzip
import json
import keyword
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from selector import best_split  # noqa: E402

SAMPLE = ROOT / "sample"
IDENT = re.compile(r"[A-Za-z_]\w*")


def parse_patch(text):
    """Return {path: {"old": set(old line numbers touched), "text": [changed lines]}}."""
    out, cur, old_ln = {}, None, 0
    for line in text.splitlines():
        if line.startswith("diff --git"):
            cur = None
        elif line.startswith("--- "):
            continue
        elif line.startswith("+++ "):
            path = line[4:].strip()
            path = path[2:] if path.startswith("b/") else path
            cur = out.setdefault(path, {"old": set(), "inserts": set(), "text": []})
        elif line.startswith("@@") and cur is not None:
            old_ln = int(re.match(r"@@ -(\d+)", line).group(1))
        elif cur is None:
            continue
        elif line.startswith("-"):
            cur["old"].add(old_ln)
            cur["text"].append(line[1:])
            old_ln += 1
        elif line.startswith("+"):
            cur["inserts"].add(old_ln)  # inserted before old line old_ln
            cur["text"].append(line[1:])
        else:
            old_ln += 1
    return out


def main():
    m = json.loads(gzip.decompress((ROOT / "data" / "map.json.gz").read_bytes()))
    sets = [int(h, 16) for h in m["sets"]]
    tests = json.loads((SAMPLE / "tests.json").read_text())["tests"]
    by_id = {t["id"]: t for t in tests}
    map_ids = m["tests"]
    answers = json.loads((SAMPLE / "answers.json").read_text())["commits"]
    everything = (1 << len(map_ids)) - 1

    def func_at(rel, ln):
        for start, end, qual, s in m["files"][rel]["funcs"]:
            if start <= ln <= end:
                return qual, s
        return None

    total, misses = 0.0, 0
    for patch in sorted((SAMPLE / "commits").glob("*.patch")):
        cid = patch.stem
        sel, why = 0, []
        for rel, ch in parse_patch(patch.read_text()).items():
            if rel.startswith("sqlglot/") and rel in m["files"]:
                if rel in m.get("sources", {}):
                    sel |= sets[m["sources"][rel]]
                hit_module_level = False
                for ln in ch["old"]:
                    f = func_at(rel, ln)
                    if f:
                        sel |= sets[f[1]]; why.append(f[0])
                    else:
                        hit_module_level = True
                for ln in ch["inserts"]:
                    fs = [f for f in (func_at(rel, ln - 1), func_at(rel, ln)) if f]
                    for f in fs:
                        sel |= sets[f[1]]; why.append(f[0])
                    if not fs:
                        hit_module_level = True
                if hit_module_level:
                    names = {n for t in ch["text"] for n in IDENT.findall(t.split("#")[0])
                             if not keyword.iskeyword(n)}
                    for n in names:
                        if n in m["refs"]:
                            sel |= sets[m["refs"][n]]
                    sel |= sets[m["files"][rel]["import"]]
                    why.append(f"module-level{sorted(names)}")
            elif rel in m["fixtures"]:
                sel |= sets[m["fixtures"][rel]]; why.append("fixture")
            elif rel in m["testspans"]:
                for start, end, qual in m["testspans"][rel]:
                    if any(start <= ln <= end for ln in ch["old"] | ch["inserts"]):
                        prefix = f"{rel}::{qual}"
                        for i, tid in enumerate(map_ids):
                            if tid == prefix or tid.startswith(prefix + "::"):
                                sel |= 1 << i
                why.append("testfile")
            else:
                sel = everything; why.append(f"UNKNOWN {rel} -> all")

        ids = [map_ids[i] for i in range(len(map_ids)) if sel >> i & 1]
        picked = [(round(by_id[t]["seconds"] * 1000), by_id[t]["number"]) for t in ids if t in by_id]
        numbers = {n for _, n in picked}
        workers = best_split(picked)
        loads = [sum(round(tests[n]["seconds"] * 1000) for n in w) for w in workers]
        cost = (max(loads) + 2000 * len(workers)) / 1000 if workers else 0.0
        failing = answers[cid]["failing"]
        missed = [tests[n]["id"] for n in failing if n not in numbers]
        if missed:
            misses += 1
            cost += 300
        total += cost
        flag = "MISS" if missed else "ok  "
        print(f"{flag} {cid} {answers[cid]['kind']:8} fail={len(failing):2} sel={len(ids):4} "
              f"k={len(workers)} cost={cost:6.2f}  {sorted(set(why))[:4]}")
        for t in missed:
            print(f"       missed: {t}")
    print(f"\nTOTAL simulated {total:.1f} s, misses {misses}/{len(answers)}")


if __name__ == "__main__":
    main()
