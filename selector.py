"""Blast Radius selector.

For each patch, pick the tests that could fail using a coverage map built from
the pinned sqlglot commit (data/map.json.gz, see tools/build_map.py), then pack
them onto the cheapest number of workers.

Safety rules (a miss costs 300 s, running everything costs ~37 s):
- Any file we cannot reason about, syntax errors, import-line changes,
  new/deleted/renamed files, or any unexpected exception -> run everything.
- Tests the map has no coverage for (subprocess/source-reading tests) are
  always included when a commit changes anything; they cost ~0.5 s.
All logging goes to stderr; stdout carries only one JSON line per commit.
"""

import ast
import gzip
import json
import keyword
import re
import sys
from pathlib import Path

DATA = Path("/data")
APP = Path(__file__).resolve().parent
MAP_PATH = APP / "data" / "map.json.gz"
SRC = APP / "src"  # pinned sqlglot checkout, downloaded at docker build time

MAX_WORKERS = 8
WORKER_COST_MS = 2000

IDENT = re.compile(r"[A-Za-z_]\w*")
HARMLESS_PREFIXES = ("docs/", "posts/", "benchmarks/", "pdoc/", ".github/")
HARMLESS_SUFFIXES = (".md", ".rst", ".png", ".jpg", ".svg", ".gif")
HARMLESS_FILES = {"LICENSE", ".gitignore", ".gitattributes", "CHANGELOG.md"}


def log(*a):
    print(*a, file=sys.stderr)


# ----------------------------------------------------------------- packing --

def pack(tests: list[tuple[int, int]], k: int) -> tuple[int, list[list[int]]]:
    """Longest test first, each onto the least-loaded worker. tests = (ms, number)."""
    workers: list[list[int]] = [[] for _ in range(k)]
    loads = [0] * k
    for ms, number in tests:
        lightest = loads.index(min(loads))
        workers[lightest].append(number)
        loads[lightest] += ms
    return max(loads), workers


def best_split(tests: list[tuple[int, int]]) -> list[list[int]]:
    """Try every worker count and keep the cheapest. Empty selection -> no workers."""
    if not tests:
        return []
    tests = sorted(tests, reverse=True)
    best_cost, best_workers = None, []
    for k in range(1, min(MAX_WORKERS, len(tests)) + 1):
        makespan, workers = pack(tests, k)
        cost = makespan + WORKER_COST_MS * k
        if best_cost is None or cost < best_cost:
            best_cost, best_workers = cost, workers
    return [w for w in best_workers if w]


# ------------------------------------------------------------ patch parsing --

class FileChange:
    def __init__(self, old_path, new_path):
        self.old_path = old_path  # None for a new file
        self.new_path = new_path  # None for a deleted file
        self.hunks = []  # [(old_start, [lines with ' ', '-', '+' prefix])]
        self.binary = False
        self.renamed = False

    @property
    def path(self):
        return self.new_path or self.old_path

    def touched(self):
        """(old line numbers removed, old line numbers before which lines were inserted, changed texts)."""
        removed, inserts, texts = set(), set(), []
        for start, lines in self.hunks:
            ln = start
            for line in lines:
                tag, body = line[:1], line[1:]
                if tag == "-":
                    removed.add(ln)
                    texts.append(body)
                    ln += 1
                elif tag == "+":
                    inserts.add(ln)
                    texts.append(body)
                elif tag == " ":
                    ln += 1
        return removed, inserts, texts

    def apply(self, old_text):
        """Return the patched file text (best effort, by old line numbers)."""
        old = old_text.splitlines(keepends=True)
        out, pos = [], 0  # pos = 0-based index into old
        for start, lines in self.hunks:
            begin = max(start - 1, 0) if start > 0 else 0
            out.extend(old[pos:begin])
            pos = begin
            for line in lines:
                tag, body = line[:1], line[1:]
                if tag == " ":
                    out.append(old[pos] if pos < len(old) else body + "\n")
                    pos += 1
                elif tag == "-":
                    pos += 1
                elif tag == "+":
                    out.append(body + "\n")
        out.extend(old[pos:])
        return "".join(out)


def _strip_prefix(p):
    p = p.strip().split("\t")[0]
    if p == "/dev/null":
        return None
    return p[2:] if p[:2] in ("a/", "b/") else p


def parse_patch(text):
    changes, cur, hunk = [], None, None
    for line in text.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            cur = FileChange(m.group(1), m.group(2)) if m else FileChange(None, None)
            changes.append(cur)
            hunk = None
        elif cur is None:
            continue
        elif hunk is None and line.startswith("--- "):
            cur.old_path = _strip_prefix(line[4:])
        elif hunk is None and line.startswith("+++ "):
            cur.new_path = _strip_prefix(line[4:])
        elif line.startswith("@@"):
            m = re.match(r"@@ -(\d+)", line)
            hunk = (int(m.group(1)), [])
            cur.hunks.append(hunk)
        elif hunk is not None and line[:1] in (" ", "-", "+"):
            hunk[1].append(line)
        elif hunk is not None and line == "":
            hunk[1].append(" ")  # some tools drop the space on blank context lines
        elif line.startswith(("Binary files", "GIT binary patch")):
            cur.binary = True
        elif line.startswith(("rename from", "rename to", "copy from", "copy to")):
            cur.renamed = True
        elif line.startswith("new file mode"):
            cur.old_path = None
        elif line.startswith("deleted file mode"):
            cur.new_path = None
    return changes


# ------------------------------------------------------------ source tools --

def read_source(rel):
    try:
        return (SRC / rel).read_text(encoding="utf-8")
    except OSError:
        return None


def compiles(text, rel):
    try:
        compile(text, rel, "exec", dont_inherit=True)
        return True
    except (SyntaxError, ValueError):
        return False


def module_level_context(tree, line):
    """For a line outside function bodies: enclosing class names, names the
    statement assigns/defines, whether it is an import, and the qualname of a
    function whose header (decorators/def/signature) holds the line."""
    classes, targets, is_import, header = [], set(), False, None

    def visit(body, prefix):
        nonlocal is_import, header
        for node in body:
            start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
            end = node.end_lineno or node.lineno
            if not (start <= line <= end):
                continue
            if isinstance(node, ast.ClassDef):
                classes.append(node.name)
                targets.add(node.name)
                visit(node.body, prefix + node.name + ".")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if line < node.body[0].lineno:
                    header = prefix + node.name
                    targets.add(node.name)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                is_import = True
            elif isinstance(node, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                for n in ast.walk(node):
                    if isinstance(n, (ast.Import, ast.ImportFrom)) and n.lineno <= line <= (n.end_lineno or n.lineno):
                        is_import = True
                for field in ("body", "orelse", "finalbody"):
                    visit(getattr(node, field, []), prefix)
                for h in getattr(node, "handlers", []):
                    visit(h.body, prefix)
            else:
                for n in ast.walk(node):
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                        targets.add(n.id)
                    elif isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store):
                        targets.add(n.attr)

    visit(tree.body, "")
    return classes, targets, is_import, header


# ---------------------------------------------------------------- selector --

class Selector:
    def __init__(self, tests):
        m = json.loads(gzip.decompress(MAP_PATH.read_bytes()))
        self.m = m
        self._sets_hex = m["sets"]
        self._sets = {}
        number_of = {t["id"]: t["number"] for t in tests}
        self.ms_of = {t["number"]: round(t["seconds"] * 1000) for t in tests}
        # map bit position -> runtime test number (tests may be renumbered)
        self.bit_to_number = [number_of.get(tid) for tid in m["tests"]]
        self.all_numbers = set(self.ms_of)
        mapped = {n for n in self.bit_to_number if n is not None}
        # tests with no coverage evidence: never seen by coverage, or unknown to the map
        covered = 0
        for h in self._sets_hex:
            covered |= int(h, 16)
        uncovered = {self.bit_to_number[i] for i in range(len(m["tests"]))
                     if not covered >> i & 1 and self.bit_to_number[i] is not None}
        nocov = {number_of[tid] for tid in m.get("nocov", []) if tid in number_of}
        self.always = uncovered | nocov | (self.all_numbers - mapped)
        log(f"map: {len(m['tests'])} tests, always-run (no coverage): {sorted(self.always)}")
        self.file_tests = {}
        for tid, n in number_of.items():
            self.file_tests.setdefault(tid.split("::", 1)[0], set()).add(n)

    def set_numbers(self, sid):
        if sid not in self._sets:
            b = int(self._sets_hex[sid], 16)
            out, i = set(), 0
            while b:
                if b & 1:
                    n = self.bit_to_number[i]
                    if n is not None:
                        out.add(n)
                b >>= 1
                i += 1
            self._sets[sid] = out
        return self._sets[sid]

    def refs(self, name):
        sid = self.m["refs"].get(name)
        return self.set_numbers(sid) if sid is not None else set()

    # -- per file kind --

    def select_source(self, ch, why):
        rel = ch.path
        info = self.m["files"][rel]
        funcs = info["funcs"]
        old_text = read_source(rel)
        if old_text is None:
            why.append(f"no source for {rel}")
            return None
        new_text = ch.apply(old_text)
        if not compiles(new_text, rel):
            why.append(f"syntax error in {rel}")
            return None

        sel = set()
        if rel in self.m.get("sources", {}):
            sel |= self.set_numbers(self.m["sources"][rel])

        def func_at(ln):
            for start, end, qual, sid in funcs:
                if start <= ln <= end:
                    return qual, sid
            return None

        removed, inserts, texts = ch.touched()
        module_lines = set()
        for ln in removed:
            f = func_at(ln)
            if f:
                sel |= self.set_numbers(f[1]); why.append(f[0])
            else:
                module_lines.add(ln)
        for ln in inserts:
            fs = [f for f in (func_at(ln - 1), func_at(ln)) if f]
            for f in fs:
                sel |= self.set_numbers(f[1]); why.append(f[0])
            if not fs:
                module_lines.add(ln)

        if module_lines:
            tree = ast.parse(old_text)
            sel |= self.set_numbers(info["import"])
            if ast.dump(tree) == ast.dump(ast.parse(new_text)):
                # comments/blank lines only: code is identical, only line numbers
                # and source text move (source readers are already in `sel`)
                why.append(f"module-level comment-only in {rel}")
                return sel
            names = {n for t in texts for n in IDENT.findall(t.split("#", 1)[0]) if not keyword.iskeyword(n)}
            if any(t.lstrip().startswith(("import ", "from ")) for t in texts):
                why.append(f"import change in {rel}")
                return None
            for ln in module_lines:
                ctx_here = module_level_context(tree, ln)
                ctx_prev = module_level_context(tree, ln - 1)
                # a removed line inside an import, or an insert between two lines
                # of the same (parenthesised) import statement
                if (ln in removed and ctx_here[2]) or (ln in inserts and ctx_here[2] and ctx_prev[2]):
                    why.append(f"import change in {rel}")
                    return None
                for classes, targets, _, header in (ctx_here, ctx_prev):
                    names |= targets | set(classes)
                    if header:
                        for start, end, qual, sid in funcs:
                            if qual == header:
                                sel |= self.set_numbers(sid)
                # nearest following function (covers def/decorator lines)
                nxt = min((f for f in funcs if f[0] > ln), key=lambda f: f[0], default=None)
                if nxt:
                    sel |= self.set_numbers(nxt[3])
            for n in names:
                sel |= self.refs(n)
            why.append(f"module-level{sorted(names)[:6]}")
        return sel

    def select_test_file(self, ch, why):
        rel = ch.path
        whole = self.file_tests.get(rel, set())
        old_text = read_source(rel)
        if old_text is None or not compiles(ch.apply(old_text), rel):
            why.append(f"test file {rel}: whole file")
            return set(whole)
        removed, inserts, _ = ch.touched()
        lines = removed | inserts | {ln - 1 for ln in inserts}
        spans = self.m["testspans"].get(rel, [])
        sel = set()
        for ln in lines:
            hit = [s for s in spans if s[0] <= ln <= s[1]]
            if not hit:
                why.append(f"test file {rel}: module/class level -> whole file")
                return set(whole)
            for start, end, qual in hit:
                if not qual.rsplit("::", 1)[-1].startswith("test"):
                    why.append(f"test file {rel}: helper {qual} -> whole file")
                    return set(whole)
                prefix = f"{rel}::{qual}"
                sel |= {n for tid, n in self._ids_in(rel) if tid == prefix or tid.startswith(prefix + "::")}
        why.append(f"testfile {rel}")
        return sel

    def _ids_in(self, rel):
        if not hasattr(self, "_ids_by_file"):
            self._ids_by_file = {}
            for i, tid in enumerate(self.m["tests"]):
                n = self.bit_to_number[i]
                if n is not None:
                    self._ids_by_file.setdefault(tid.split("::", 1)[0], []).append((tid, n))
        return self._ids_by_file.get(rel, [])

    def select_commit(self, text):
        """Return (set of test numbers, reasons). None in place of the set = run all."""
        changes = parse_patch(text)
        sel, why, any_change = set(), [], False
        for ch in changes:
            rel = ch.path
            if rel is None:
                continue
            if not ch.hunks and not ch.binary and not ch.renamed and ch.old_path and ch.new_path:
                continue  # mode-only change
            any_change = True
            name = rel.rsplit("/", 1)[-1]
            if ch.renamed or ch.old_path is None or ch.new_path is None:
                if rel.startswith(("sqlglot/", "tests/")) or rel.endswith(".py"):
                    why.append(f"added/deleted/renamed {rel}")
                    return None, why
                continue
            if ch.binary:
                if rel.startswith(HARMLESS_PREFIXES) or rel.endswith(HARMLESS_SUFFIXES):
                    continue
                why.append(f"binary {rel}")
                return None, why
            if rel in self.m["files"]:
                s = self.select_source(ch, why)
            elif rel in self.m["fixtures"]:
                s = self.set_numbers(self.m["fixtures"][rel]); why.append(f"fixture {name}")
            elif rel in self.m["testspans"]:
                s = self.select_test_file(ch, why)
            elif rel.startswith("sqlglotc/"):
                s = {n for tid, n in self._ids_in("tests/test_sqlglotc.py")}; why.append("sqlglotc")
            elif rel.startswith(HARMLESS_PREFIXES) or rel.endswith(HARMLESS_SUFFIXES) or rel in HARMLESS_FILES:
                why.append(f"harmless {rel}")
                continue
            else:
                why.append(f"unknown {rel}")
                return None, why
            if s is None:
                return None, why
            sel |= s
        if any_change:
            sel |= self.always
        return sel, why


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    all_numbers = [(round(t["seconds"] * 1000), t["number"]) for t in tests]
    all_workers = best_split(all_numbers)
    try:
        selector = Selector(tests)
    except Exception as e:  # map missing/corrupt: never miss, run everything
        log(f"map load failed ({e!r}); running all tests on every commit")
        selector = None

    for patch in sorted((DATA / "commits").glob("*.patch")):
        workers = all_workers
        if selector is not None:
            try:
                sel, why = selector.select_commit(patch.read_text(encoding="utf-8", errors="replace"))
                if sel is not None:
                    workers = best_split([(selector.ms_of[n], n) for n in sel])
                log(f"{patch.stem}: {'ALL' if sel is None else len(sel)} tests, "
                    f"{len(workers)} workers; {sorted(set(why))[:5]}")
            except Exception as e:
                log(f"{patch.stem}: error {e!r}; running all tests")
        print(json.dumps({"commit": patch.stem, "workers": workers}, separators=(",", ":")))


if __name__ == "__main__":
    main()
