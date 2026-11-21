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
import math
import re
import sys
from collections import Counter
from pathlib import Path

DATA = Path("/data")
APP = Path(__file__).resolve().parent
MAP_PATH = APP / "data" / "map.json.gz"
IMPORT_MAP_PATH = APP / "data" / "import_funcs.json"  # tools/trace_imports.py
TEST_IMPORTS_PATH = APP / "data" / "test_imports.json"  # tools/build_test_imports.py
DIALECT_CHILDREN_PATH = APP / "data" / "dialect_children.json"  # tools/build_dialect_children.py
DIALECT_PKGS = ("dialects", "parsers", "generators", "typing")
SHARED_DIALECT_MODULES = {"__init__", "dialect", "base"}
# base classes every dialect inherits from: an attribute the map has no reader
# for (read only in lambdas, via strings, or at class creation) -> run all
# test classes with setUpClass at the pinned commit (tests/ has no setUpModule)
SETUP_CLASS_CLASSES = ("tests/test_executor.py::TestExecutor", "tests/test_optimizer.py::TestOptimizer",
                       "tests/test_lineage.py::TestLineage", "tests/test_sqlglotc.py::TestSQLGlotC")
BASE_FILES = {"sqlglot/parser.py", "sqlglot/generator.py", "sqlglot/tokens.py",
              "sqlglot/dialects/dialect.py", "sqlglot/expressions/core.py"}
MAX_GUARDED_DIALECTS = 3
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


def duration_ms(seconds):
    """Exactly score.py's rounding (halves up), so packing math matches the scorer."""
    return math.floor(seconds * 1000 + 0.5)


def fill_slack(workers, candidates, ms_of):
    """Add unselected tests into idle worker time for free: a test goes onto the
    worker with the most room only if that worker's load stays <= the current
    busiest load. Integer milliseconds, so the commit's cost is exactly unchanged.
    Never adds a worker. `candidates` is in priority order."""
    if len(workers) < 2:
        return workers  # one worker (or none) has no idle time
    workers = [list(w) for w in workers]
    loads = [sum(ms_of[n] for n in w) for w in workers]
    makespan = max(loads)
    room = sum(makespan - x for x in loads)
    for n in candidates:
        if room <= 0:
            break
        d = ms_of[n]
        i = loads.index(min(loads))  # most idle worker
        if loads[i] + d <= makespan:
            workers[i].append(n)
            loads[i] += d
            room -= d
    assert max(loads) == makespan
    return workers


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


SIGNATURE = chr(10) + "-- " + chr(10)
DIFF_MARK = chr(10) + "diff --git"


def _strip_prefix(p):
    p = p.strip().split("\t")[0]
    if p == "/dev/null":
        return None
    return p[2:] if p[:2] in ("a/", "b/") else p


def parse_patch(text):
    # `git format-patch` appends a signature line '-- ' then the git version;
    # it is not part of any hunk (its '-' would read as a removed line)
    # Only strip when nothing after it could be a hunk line (a removed "- " line
    # also reads "-- "; a version string like "2.43.0" never starts with " +-").
    sig = text.rfind(SIGNATURE)
    if sig != -1:
        tail = text[sig + len(SIGNATURE):].splitlines()
        if all(not line or line[0] not in " +-@" for line in tail) and DIFF_MARK not in text[sig:]:
            text = text[: sig + 1]
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


CLASS_HOOKS = {"__init_subclass__", "__set_name__", "__class_getitem__"}
METACLASS_BASES = {"type", "ABCMeta", "EnumMeta", "EnumType"}


def ast_equal(a, b):
    """Same answer as ast.dump(a) == ast.dump(b) (fields only, no line numbers),
    but stops at the first difference instead of rendering both trees as text."""
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.AST):
        return all(ast_equal(getattr(a, f, None), getattr(b, f, None)) for f in a._fields)
    if isinstance(a, list):
        return len(a) == len(b) and all(ast_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, (float, complex)):
        return repr(a) == repr(b)  # dump distinguishes -0.0 from 0.0
    return a == b


def changed_hooks(old, new):
    return sorted(k for k in old.keys() | new.keys()
                  if k not in old or k not in new or not ast_equal(old[k], new[k]))


def class_creation_hooks(tree):
    """{qualname: FunctionDef node} of every function that runs when a class is created:
    __init_subclass__/__set_name__/__class_getitem__ anywhere, and every method
    of a metaclass (a class deriving from `type`, directly or through another
    metaclass in this module). Coverage credits these to whichever test first
    imported the class (or to none), so the map cannot be trusted for them."""
    classes = []  # (qualname, ClassDef)

    def collect(body, prefix):
        for node in body:
            if isinstance(node, ast.ClassDef):
                classes.append((prefix + node.name, node))
                collect(node.body, prefix + node.name + ".")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                collect(node.body, prefix + node.name + ".")

    collect(tree.body, "")

    def base_name(b):
        return b.id if isinstance(b, ast.Name) else b.attr if isinstance(b, ast.Attribute) else None

    metas, grew = set(), True
    while grew:  # fixpoint: metaclasses deriving from metaclasses defined here
        grew = False
        for _, c in classes:
            if c.name not in metas and any(base_name(b) in METACLASS_BASES | metas for b in c.bases):
                metas.add(c.name)
                grew = True

    hooks = {}
    for qual, c in classes:
        for f in c.body:
            if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and (c.name in metas or f.name in CLASS_HOOKS):
                hooks[f"{qual}.{f.name}"] = f
    return hooks


def defined_functions(tree):
    """Counter of qualnames of functions/methods defined at module or class scope
    (functions nested inside functions are part of their parent's body)."""
    out = Counter()

    def walk(body, prefix):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out[prefix + node.name] += 1
            elif isinstance(node, ast.ClassDef):
                walk(node.body, prefix + node.name + ".")
            elif isinstance(node, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                for field in ("body", "orelse", "finalbody"):
                    walk(getattr(node, field, []), prefix)
                for h in getattr(node, "handlers", []):
                    walk(h.body, prefix)

    walk(tree.body, "")
    return out


# ---------------------------------------------------------------- selector --

class Selector:
    def __init__(self, tests):
        m = json.loads(gzip.decompress(MAP_PATH.read_bytes()))
        self.m = m
        self._sets_hex = m["sets"]
        self._sets = {}
        number_of = {t["id"]: t["number"] for t in tests}
        self.ms_of = {t["number"]: duration_ms(t["seconds"]) for t in tests}
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
        # function-body lines that run at import time (core, or per dialect)
        try:
            self.imp = json.loads(IMPORT_MAP_PATH.read_text())
        except (OSError, ValueError) as e:
            log(f"WARNING: import map unavailable ({e!r}); import-time helper guard disabled")
            self.imp = {"core": {}, "dialects": {}}
        self._core_lines, self._dialect_lines = {}, {}
        self._base, self._dialect_tests = {}, {}
        # cov_plugin switches context per test, so whatever a class's setUpClass
        # opens is credited to the class's first test (unittest runs test methods
        # in alphabetical order): map that first test -> all tests of the class
        self.setup_class_fanout = {}
        for cls in SETUP_CLASS_CLASSES:
            members = sorted(tid for tid in number_of if tid.startswith(cls + "::"))
            if members:
                self.setup_class_fanout[number_of[members[0]]] = {number_of[t] for t in members}
        try:  # tools/build_test_imports.py
            self.test_imports = json.loads(TEST_IMPORTS_PATH.read_text())
        except (OSError, ValueError) as e:
            log(f"WARNING: test import graph unavailable ({e!r}); helper edits select own file only")
            self.test_imports = {}
        try:
            self.dialect_children = json.loads(DIALECT_CHILDREN_PATH.read_text())
        except (OSError, ValueError) as e:
            log(f"WARNING: dialect children map unavailable ({e!r}); parent edits select own dialect only")
            self.dialect_children = {}

    def core_import_lines(self, rel):
        if rel not in self._core_lines:
            self._core_lines[rel] = {ln for s, e in self.imp["core"].get(rel, ()) for ln in range(s, e + 1)}
        return self._core_lines[rel]

    def dialect_import_lines(self, rel):
        if rel not in self._dialect_lines:
            d = {}
            for s, e, names in self.imp["dialects"].get(rel, ()):
                for ln in range(s, e + 1):
                    d.setdefault(ln, set()).update(names)
            self._dialect_lines[rel] = d
        return self._dialect_lines[rel]

    def base_file(self, rel):
        """(text, tree, class-creation hooks, defined functions) of the pinned
        file, computed once per run: every commit patches the same originals."""
        if rel not in self._base:
            text = read_source(rel)
            if text is None:
                self._base[rel] = None
            else:
                tree = ast.parse(text)
                self._base[rel] = (text, tree, class_creation_hooks(tree), defined_functions(tree))
        return self._base[rel]

    def dialect_tests(self, name):
        """Tests that use a dialect: anything that ran its dialect/parser/generator/
        typing module, plus its own test file. Cached: it is the same every commit."""
        if name not in self._dialect_tests:
            self._dialect_tests[name] = frozenset(self._dialect_tests_uncached(name))
        return self._dialect_tests[name]

    def _dialect_tests_uncached(self, name):
        out = set(self.file_tests.get(f"tests/dialects/test_{name}.py", ()))
        for pkg in ("dialects", "parsers", "generators", "typing"):
            info = self.m["files"].get(f"sqlglot/{pkg}/{name}.py")
            if info:
                out |= self.set_numbers(info["any"])
        return out

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
        base = self.base_file(rel)
        if base is None:
            why.append(f"no source for {rel}")
            return None
        old_text, old_tree, old_hooks, old_defs = base
        new_text = ch.apply(old_text)
        # the pinned file is parsed once per run (base_file); the patched one here
        try:
            new_tree = ast.parse(new_text)
            compile(new_tree, rel, "exec", dont_inherit=True)  # symtable errors too
        except (SyntaxError, ValueError):
            why.append(f"syntax error in {rel}")
            return None

        # class-creation hooks affect every class they build: run everything
        new_hooks = class_creation_hooks(new_tree)
        changed = changed_hooks(old_hooks, new_hooks)
        if changed:
            why.append(f"class-creation hook changed in {rel}: {changed[:3]}")
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

        # helpers whose edited lines run while modules/classes are imported: the
        # coverage map credits import-time work to no test
        near = removed | inserts | {ln - 1 for ln in inserts}
        if near & self.core_import_lines(rel):
            why.append(f"import-time helper (core) changed in {rel}")
            return None
        dialect_lines = self.dialect_import_lines(rel)
        dialects = set().union(*(dialect_lines.get(ln, ()) for ln in near))
        if len(dialects) > MAX_GUARDED_DIALECTS:
            why.append(f"import-time helper for {len(dialects)} dialects in {rel}")
            return None
        for d in sorted(dialects):
            sel |= self.dialect_tests(d)
        if dialects:
            why.append(f"import-time helper for {sorted(dialects)} in {rel}")

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

        # an added/removed/renamed function or method changes what the class
        # offers (e.g. a new *_sql override changes generator dispatch), which the
        # neighbouring function's coverage says nothing about: go class-level
        new_defs = defined_functions(new_tree)
        def_names = set()
        if old_defs != new_defs:
            diff = (old_defs - new_defs) + (new_defs - old_defs)
            for qual in diff:
                def_names |= set(qual.split("."))
            why.append(f"defs added/removed in {rel}: {sorted(diff)[:3]}")

        if module_lines or def_names:
            tree = old_tree
            sel |= self.set_numbers(info["import"])
            if not def_names and ast_equal(tree, new_tree):
                # comments/blank lines only: code is identical, only line numbers
                # and source text move (source readers are already in `sel`)
                why.append(f"module-level comment-only in {rel}")
                return sel
            names = {n for t in texts for n in IDENT.findall(t.split("#", 1)[0]) if not keyword.iskeyword(n)}
            names |= def_names
            if any(t.lstrip().startswith(("import ", "from ")) for t in texts):
                why.append(f"import change in {rel}")
                return None
            edited_targets = set()
            for ln in module_lines:
                ctx_here = module_level_context(tree, ln)
                ctx_prev = module_level_context(tree, ln - 1)
                edited_targets |= ctx_here[1] - set(ctx_here[0])
                if ln in inserts:
                    edited_targets |= ctx_prev[1] - set(ctx_prev[0])
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
            if rel in BASE_FILES and edited_targets and not any(t in self.m["refs"] for t in edited_targets):
                why.append(f"base-file attribute with no known readers in {rel}: {sorted(edited_targets)[:4]}")
                return None
            for n in names:
                sel |= self.refs(n)
            # a class-body edit in a dialect module changes that dialect and every
            # dialect inheriting from it, whether or not the map sees the reader
            parts = rel.split("/")
            if len(parts) == 3 and parts[1] in DIALECT_PKGS and parts[2][:-3] not in SHARED_DIALECT_MODULES:
                fan = [parts[2][:-3]] + self.dialect_children.get(parts[2][:-3], [])
                for d in fan:
                    sel |= self.dialect_tests(d)
                why.append(f"dialect fanout {fan}")
            why.append(f"module-level{sorted(names)[:6]}")
        return sel

    def select_test_file(self, ch, why):
        rel = ch.path
        info = self.test_imports.get(rel, {})
        # a test file can be a library for other test files (test_dialect.py's
        # Validator is imported and subclassed by 33 dialect test files): a
        # broad edit to it can break every importer
        whole = set(self.file_tests.get(rel, set()))
        for imp in info.get("importers", ()):
            whole |= self.file_tests.get(imp, set())
        downstream = f" + {len(info.get('importers', ()))} importing test files" if info else ""
        old_text = read_source(rel)
        if old_text is None or not compiles(ch.apply(old_text), rel):
            why.append(f"test file {rel}: whole file{downstream}")
            return whole
        removed, inserts, _ = ch.touched()
        lines = removed | inserts | {ln - 1 for ln in inserts}
        spans = self.m["testspans"].get(rel, [])
        subclassed = set(info.get("subclassed", ()))
        sel = set()
        for ln in lines:
            hit = [s for s in spans if s[0] <= ln <= s[1]]
            if not hit:
                why.append(f"test file {rel}: module/class level -> whole file{downstream}")
                return whole
            for start, end, qual in hit:
                if not qual.rsplit("::", 1)[-1].startswith("test"):
                    why.append(f"test file {rel}: helper {qual} -> whole file{downstream}")
                    return whole
                if qual.split("::", 1)[0] in subclassed:
                    # a test* method of a base class runs again in every subclass
                    why.append(f"test file {rel}: inherited test {qual} -> whole file{downstream}")
                    return whole
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

    def slack_candidates(self, sel):
        """Unselected tests in the order we want them packed into idle time:
        (a) tests that ran any line of a changed file, (b) the changed dialect's
        own test file, (c) everything else; shortest first within each tier so
        each idle second holds as many extra checks as possible."""
        tier_a, tier_b = set(), set()
        for rel in getattr(self, "last_paths", []):
            info = self.m["files"].get(rel)
            if info:
                tier_a |= self.set_numbers(info["any"])
            parts = rel.split("/")
            if len(parts) == 3 and parts[0] == "sqlglot" and parts[1] in ("dialects", "parsers", "generators", "typing"):
                tier_b |= self.file_tests.get(f"tests/dialects/test_{parts[2][:-3]}.py", set())
            elif rel in self.m["testspans"]:
                tier_a |= self.file_tests.get(rel, set())
        tier_a -= sel
        tier_b -= sel | tier_a
        if not hasattr(self, "_by_duration"):
            self._by_duration = sorted(self.all_numbers, key=lambda n: (self.ms_of[n], n))
        taken = sel | tier_a | tier_b
        key = lambda n: (self.ms_of[n], n)  # noqa: E731
        return (sorted(tier_a, key=key) + sorted(tier_b, key=key)
                + [n for n in self._by_duration if n not in taken])

    def select_commit(self, text):
        """Return (set of test numbers, reasons). None in place of the set = run all."""
        changes = parse_patch(text)
        sel, why, any_change = set(), [], False
        self.last_paths = [ch.path for ch in changes if ch.path]
        if text.strip() and not self.last_paths:
            why.append("non-empty patch with no recognisable file changes")
            return None, why
        for ch in changes:
            rel = ch.path
            if rel is None:
                continue
            if not ch.hunks and not ch.binary and not ch.renamed and ch.old_path and ch.new_path:
                continue  # mode-only change
            any_change = True
            name = rel.rsplit("/", 1)[-1]
            if ch.renamed or ch.old_path is None or ch.new_path is None:
                # any added/deleted/renamed file that is not plainly harmless (incl.
                # root config like pyproject.toml / pytest.ini / setup.cfg) -> run all
                if rel.startswith(HARMLESS_PREFIXES) or rel.endswith(HARMLESS_SUFFIXES) or rel in HARMLESS_FILES:
                    continue
                why.append(f"added/deleted/renamed {rel}")
                return None, why
            if ch.binary:
                if rel.startswith(HARMLESS_PREFIXES) or rel.endswith(HARMLESS_SUFFIXES):
                    continue
                why.append(f"binary {rel}")
                return None, why
            if rel in self.m["files"]:
                s = self.select_source(ch, why)
            elif rel in self.m["fixtures"]:
                s = set(self.set_numbers(self.m["fixtures"][rel])); why.append(f"fixture {name}")
                for first, members in self.setup_class_fanout.items():
                    if first in s:  # opened in setUpClass: coverage credited only test #1
                        s |= members
                        why.append(f"setUpClass fixture -> whole class of {len(members)}")
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
    all_numbers = [(duration_ms(t["seconds"]), t["number"]) for t in tests]
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
                    if len(workers) >= 2:  # a single worker has no idle time
                        before = len(sel)
                        workers = fill_slack(workers, selector.slack_candidates(sel), selector.ms_of)
                        why.append(f"slack +{sum(map(len, workers)) - before} free tests")
                log(f"{patch.stem}: {'ALL' if sel is None else len(sel)} tests, "
                    f"{len(workers)} workers; {sorted(set(why))[:5]}")
            except Exception as e:
                log(f"{patch.stem}: error {e!r}; running all tests")
        print(json.dumps({"commit": patch.stem, "workers": workers}, separators=(",", ":")))


if __name__ == "__main__":
    main()
