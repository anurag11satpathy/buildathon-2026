"""Offline only: local mutation suite for measuring selector recall and cost.

Generates synthetic patches against pinned sqlglot, finds the tests that
REALLY fail on each one (full pytest run in the scorer's base image = the
"oracle"), then runs our real selector image on them exactly like the scorer
and reports misses per change category.

Everything generated lives in tools/out/ (git-ignored). Needs the pinned
source in sqlglot-src/ (git-ignored) for generating patches.

Usage (PowerShell or bash, from the repo root):
    python tools/mutation_suite.py build      # build the oracle image (once)
    python tools/mutation_suite.py validate   # oracle vs sample/answers.json on 6 sample commits
    python tools/mutation_suite.py gen        # write tools/out/mutants/
    python tools/mutation_suite.py oracle     # ground truth for the mutants (resumable)
    python tools/mutation_suite.py select     # run selector image + score + per-commit report
Options: --suite <dir under tools/out>, --only id1,id2
"""

import argparse
import ast
import difflib
import json
import random
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tools" / "out"
SRC = ROOT / "sqlglot-src"
ORACLE_IMAGE = "blast-oracle"
SELECTOR_IMAGE = "blast-radius-entry"
# hard caps so a runaway test run is OOM-killed inside the container, not the Docker VM
ORACLE_LIMITS = ["--memory", "5g", "--memory-swap", "5g", "--cpus", "8", "--pids-limit", "512"]
VALIDATE_IDS = ["3utz", "6ze8", "2vdj", "rszg", "f4xa", "4d63"]

RUN_FLAGS = ["--rm", "--platform", "linux/amd64", "--network", "none", "--cpus", "2",
             "--memory", "4096m", "--memory-swap", "4096m", "--pids-limit", "256", "--read-only",
             "--tmpfs", "/tmp:rw,size=64m,mode=1777", "--cap-drop", "ALL",
             "--security-opt", "no-new-privileges", "--user", "65534:65534", "--env", "HOME=/tmp"]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def run(cmd, **kw):
    log("$ " + " ".join(str(c) for c in cmd))
    return subprocess.run([str(c) for c in cmd], **kw)


# ------------------------------------------------------------------ helpers --

def read(rel):
    return (SRC / rel).read_text(encoding="utf-8")


def make_diff(rel, old, new):
    body = difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                f"a/{rel}", f"b/{rel}", n=3)
    return f"diff --git a/{rel} b/{rel}\nindex 1111111..2222222 100644\n" + "".join(body)


def lines_of(text):
    return text.splitlines(keepends=True)


def find_class(tree, pred):
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and pred(node):
            return node
    raise LookupError("class not found")


def methods(cls):
    return [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def node_start(n):
    return min([n.lineno] + [d.lineno for d in getattr(n, "decorator_list", [])])


def indent_of(line):
    return line[: len(line) - len(line.lstrip())]


def insert_after(text, lineno, block_lines):
    ls = lines_of(text)
    return "".join(ls[:lineno] + block_lines + ls[lineno:])


def delete_lines(text, start, end):
    ls = lines_of(text)
    return "".join(ls[: start - 1] + ls[end:])


def replace_once(text, old, new):
    assert text.count(old) == 1, f"anchor not unique/absent: {old!r} ({text.count(old)})"
    return text.replace(old, new)


# ----------------------------------------------------------- mutant makers --
# Each returns (rel_path, new_text). Asserts guarantee the edit is what we meant.

def m_new_method_generator(rel):
    text = read(rel); tree = ast.parse(text)
    cls = find_class(tree, lambda c: c.name.endswith("Generator"))
    have = {m.name for m in methods(cls)}
    base = read("sqlglot/generator.py")
    name = next(n for n in ["column_sql", "alias_sql", "from_sql", "where_sql", "table_sql", "paren_sql"]
                if n not in have and f"def {n}(" in base)
    first = methods(cls)[0]
    ind = indent_of(lines_of(text)[first.lineno - 1])
    block = ["\n", f"{ind}def {name}(self, expression):\n",
             f"{ind}    return super().{name}(expression) + \" \"\n"]
    return rel, insert_after(text, first.end_lineno, block)


def m_new_method_parser(rel):
    text = read(rel); tree = ast.parse(text)
    cls = find_class(tree, lambda c: c.name.endswith("Parser"))
    assert "_parse_where" not in {m.name for m in methods(cls)}
    assert "def _parse_where(" in read("sqlglot/parser.py")
    first = methods(cls)[0]
    ind = indent_of(lines_of(text)[first.lineno - 1])
    block = ["\n", f"{ind}def _parse_where(self, *args, **kwargs):\n", f"{ind}    return None\n"]
    return rel, insert_after(text, first.end_lineno, block)


def _sql_methods(rel, suffix):
    text = read(rel); tree = ast.parse(text)
    cls = find_class(tree, lambda c: c.name.endswith(suffix))
    ms = [m for m in methods(cls) if m.name.endswith("_sql") and m.end_lineno - m.lineno >= 3]
    return text, ms


def m_delete_method(rel, k):
    text, ms = _sql_methods(rel, "Generator")
    m = ms[k % len(ms)]
    end = m.end_lineno + (1 if lines_of(text)[m.end_lineno].strip() == "" else 0)
    return rel, delete_lines(text, node_start(m), end)


def m_rename_method(rel, k):
    text, ms = _sql_methods(rel, "Generator")
    m = ms[k % len(ms)]
    ls = lines_of(text)
    line = ls[m.lineno - 1]
    assert f"def {m.name}(" in line
    ls[m.lineno - 1] = line.replace(f"def {m.name}(", f"def {m.name}_old(", 1)
    return rel, "".join(ls)


def m_anchor(rel, old, new):
    return rel, replace_once(read(rel), old, new)


def m_module_comment(rel):
    text = read(rel); tree = ast.parse(text)
    first_cls = next(n for n in tree.body if isinstance(n, (ast.ClassDef, ast.FunctionDef)))
    return rel, insert_after(text, node_start(first_cls) - 1, ["# NOTE: reviewed for consistency\n"])


def m_function_comment(rel, k):
    text = read(rel); tree = ast.parse(text)
    fs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and len(n.body) > 2]
    f = fs[k % len(fs)]
    stmt = f.body[1]
    ind = indent_of(lines_of(text)[stmt.lineno - 1])
    return rel, insert_after(text, stmt.lineno - 1, [f"{ind}# keep this in sync with the docs\n"])


def m_class_comment(rel):
    text = read(rel); tree = ast.parse(text)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
    stmt = cls.body[0]
    ind = indent_of(lines_of(text)[stmt.lineno - 1])
    return rel, insert_after(text, stmt.lineno - 1, [f"{ind}# dialect settings\n"])


def m_inline_comment(rel, k):
    text = read(rel); tree = ast.parse(text)
    stmts = [s for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)
             for s in f.body if isinstance(s, (ast.Assign, ast.Return)) and s.lineno == s.end_lineno
             and "#" not in lines_of(text)[s.lineno - 1]]
    s = stmts[k % len(stmts)]
    ls = lines_of(text)
    ls[s.lineno - 1] = ls[s.lineno - 1].rstrip("\n") + "  # noqa\n"
    return rel, "".join(ls)


def _docstring_node(node):
    if node.body and isinstance(node.body[0], ast.Expr) and isinstance(getattr(node.body[0], "value", None), ast.Constant) \
            and isinstance(node.body[0].value.value, str):
        return node.body[0]
    return None


def m_docstring(rel, kind, k):
    text = read(rel); tree = ast.parse(text)
    if kind == "module":
        cands = [tree]
    else:
        typ = ast.ClassDef if kind == "class" else ast.FunctionDef
        cands = [n for n in ast.walk(tree) if isinstance(n, typ)]
    cands = [n for n in cands if _docstring_node(n)]
    d = _docstring_node(cands[k % len(cands)])
    ls = lines_of(text)
    ln = d.lineno - 1
    line = ls[ln]
    q = '"""' if '"""' in line else "'''" if "'''" in line else None
    assert q, line
    i = line.index(q) + 3
    ls[ln] = line[:i] + "Note: " + line[i:] if line[i:].strip() not in ("", "\n") else line.rstrip("\n") + "Note:\n"
    return rel, "".join(ls)


def m_text_insert(rel, after_line, new_line):
    return rel, insert_after(read(rel), after_line, [new_line])


def m_dict_entry_delete(rel, cls_pred, attr, k):
    text = read(rel); tree = ast.parse(text)
    cls = find_class(tree, cls_pred)
    for node in cls.body:
        tgt = node.targets[0] if isinstance(node, ast.Assign) else getattr(node, "target", None)
        if isinstance(tgt, ast.Name) and tgt.id == attr and isinstance(node.value, (ast.Dict, ast.Call, ast.BinOp)):
            d = node.value
            while not isinstance(d, ast.Dict):  # {**base, ...} or dict(...) / a | {...}
                d = d.right if isinstance(d, ast.BinOp) else (d.args[0] if d.args else None)
                if d is None:
                    raise LookupError("no dict literal")
            entries = [(kk, v) for kk, v in zip(d.keys, d.values) if kk is not None]
            own = [(kk, v) for kk, v in entries
                   if sum(1 for k2, v2 in entries if k2.lineno <= v.end_lineno and v2.end_lineno >= kk.lineno) == 1]
            kk, v = own[k % len(own)]
            return rel, delete_lines(text, kk.lineno, v.end_lineno)
    raise LookupError(f"{attr} not in {cls.name}")


def m_bool_flag_flip(rel, cls_pred, k):
    text = read(rel); tree = ast.parse(text)
    cls = find_class(tree, cls_pred)
    flags = [n for n in cls.body if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
             and isinstance(n.value.value, bool) and n.lineno == n.end_lineno]
    n = flags[k % len(flags)]
    ls = lines_of(text)
    old = repr(n.value.value)
    line = ls[n.lineno - 1]
    i = line.rindex(old)
    ls[n.lineno - 1] = line[:i] + repr(not n.value.value) + line[i + len(old):]
    return rel, "".join(ls)


def m_arg_types_flip(rel, k):
    text = read(rel); tree = ast.parse(text)
    hits = []
    for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
        for node in cls.body:
            if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "arg_types" \
                    and isinstance(node.value, ast.Dict):
                for kk, v in zip(node.value.keys, node.value.values):
                    if isinstance(v, ast.Constant) and v.value is False and v.lineno == v.end_lineno:
                        hits.append(v)
    v = hits[k % len(hits)]
    ls = lines_of(text)
    line = ls[v.lineno - 1]
    ls[v.lineno - 1] = line[:v.col_offset] + "True" + line[v.end_col_offset:]
    return rel, "".join(ls)


def m_compare_flip(rel, k):
    text = read(rel); tree = ast.parse(text)
    cmps = [c for f in ast.walk(tree) if isinstance(f, ast.FunctionDef) for c in ast.walk(f)
            if isinstance(c, ast.Compare) and len(c.ops) == 1 and isinstance(c.ops[0], (ast.Eq, ast.NotEq, ast.Is, ast.IsNot))
            and c.lineno == c.end_lineno]
    c = cmps[k % len(cmps)]
    ls = lines_of(text)
    line = ls[c.lineno - 1]
    seg = line[c.col_offset:c.end_col_offset]
    swaps = [(" == ", " != "), (" != ", " == "), (" is not ", " is "), (" is ", " is not ")]
    for a, b in swaps:
        if a in seg:
            seg2 = seg.replace(a, b, 1)
            break
    else:
        raise LookupError(seg)
    ls[c.lineno - 1] = line[:c.col_offset] + seg2 + line[c.end_col_offset:]
    return rel, "".join(ls)


def ends_gen(c):
    return c.name.endswith("Generator")


def ends_parser(c):
    return c.name.endswith("Parser")


def named(n):
    return lambda c: c.name == n


def mutant_specs(rng):
    """(id, category, maker) — ids are stable so oracle results can be resumed."""
    r = lambda: rng.randrange(1000)  # noqa: E731
    return [
        # Phase 1 item 1: new / removed / renamed methods
        ("nm01", "new_method", lambda: m_new_method_generator("sqlglot/generators/duckdb.py")),
        ("nm02", "new_method", lambda: m_new_method_generator("sqlglot/generators/snowflake.py")),
        ("nm03", "new_method", lambda: m_new_method_generator("sqlglot/generators/postgres.py")),
        ("nm04", "new_method", lambda: m_new_method_generator("sqlglot/generators/tsql.py")),
        ("nm05", "new_method", lambda: m_new_method_parser("sqlglot/parsers/bigquery.py")),
        ("nm06", "new_method", lambda: m_new_method_parser("sqlglot/parsers/clickhouse.py")),
        ("dm01", "delete_method", lambda: m_delete_method("sqlglot/generators/duckdb.py", r())),
        ("dm02", "delete_method", lambda: m_delete_method("sqlglot/generators/bigquery.py", r())),
        ("rn01", "rename_method", lambda: m_rename_method("sqlglot/generators/snowflake.py", r())),
        ("rn02", "rename_method", lambda: m_rename_method("sqlglot/generators/spark.py", r())),
        # Phase 1 item 2: import-time / class-creation hooks
        ("ih01", "import_hook", lambda: m_anchor("sqlglot/expressions/core.py",
                                                 "cls.key = cls.__name__.lower()", "cls.key = cls.__name__.lower()[:12]")),
        ("ih02", "import_hook", lambda: m_anchor("sqlglot/tokens.py",
                                                 'for p in ("n", "N")', 'for p in ("N",)')),
        ("ih03", "import_hook", lambda: m_anchor("sqlglot/dialects/dialect.py",
                                                 "new_trie(klass.FORMAT_MAPPING) if klass.FORMAT_MAPPING else klass.TIME_TRIE",
                                                 "klass.TIME_TRIE")),
        ("ih04", "import_hook", lambda: m_anchor("sqlglot/dialects/dialect.py",
                                                 "| (klass.__dict__.get(\"INVERSE_TIME_MAPPING\") or {})", "")),
        # Phase 1 item 3: comments / docstrings
        ("cm01", "comment", lambda: m_module_comment("sqlglot/generator.py")),
        ("cm02", "comment", lambda: m_function_comment("sqlglot/optimizer/simplify.py", r())),
        ("cm03", "comment", lambda: m_class_comment("sqlglot/dialects/duckdb.py")),
        ("cm04", "comment", lambda: m_inline_comment("sqlglot/parser.py", r())),
        ("cm05", "comment", lambda: m_module_comment("tests/dialects/test_duckdb.py")),
        ("cm06", "comment", lambda: m_text_insert("tests/fixtures/optimizer/simplify.sql", 0, "-- reviewed\n")),
        ("cm07", "comment", lambda: m_text_insert("tests/fixtures/identity.sql", 0, "-- reviewed\n")),
        ("ds01", "docstring", lambda: m_docstring("sqlglot/optimizer/qualify_columns.py", "function", r())),
        ("ds02", "docstring", lambda: m_docstring("sqlglot/expressions/query.py", "class", r())),
        ("ds03", "docstring", lambda: m_docstring("sqlglot/__init__.py", "module", 0)),
        ("ds04", "docstring", lambda: m_docstring("sqlglot/expressions/core.py", "function", r())),
        # Phase 2 validation: class-body value edits
        ("ca01", "class_attr", lambda: m_dict_entry_delete("sqlglot/generators/duckdb.py", ends_gen, "TRANSFORMS", r())),
        ("ca02", "class_attr", lambda: m_dict_entry_delete("sqlglot/parsers/bigquery.py", ends_parser, "FUNCTIONS", r())),
        ("ca03", "class_attr", lambda: m_dict_entry_delete("sqlglot/dialects/snowflake.py", named("Tokenizer"), "KEYWORDS", r())),
        ("ca04", "class_attr", lambda: m_dict_entry_delete("sqlglot/generators/postgres.py", ends_gen, "TYPE_MAPPING", r())),
        ("ca05", "class_attr", lambda: m_dict_entry_delete("sqlglot/generators/tsql.py", ends_gen, "TRANSFORMS", r())),
        ("ca06", "class_attr", lambda: m_dict_entry_delete("sqlglot/parsers/mysql.py", ends_parser, "FUNCTIONS", r())),
        ("ca07", "class_attr", lambda: m_dict_entry_delete("sqlglot/generator.py", named("Generator"), "TRANSFORMS", r())),
        ("ca08", "class_attr", lambda: m_arg_types_flip("sqlglot/expressions/ddl.py", r())),
        ("ca09", "class_attr", lambda: m_arg_types_flip("sqlglot/expressions/functions.py", r())),
        ("ca10", "class_attr", lambda: m_bool_flag_flip("sqlglot/dialects/presto.py", named("Presto"), r())),
        ("ca11", "class_attr", lambda: m_bool_flag_flip("sqlglot/dialects/dialect.py", named("Dialect"), r())),
        ("ca12", "class_attr", lambda: m_bool_flag_flip("sqlglot/generator.py", named("Generator"), r())),
        # Sanity: plain function-body edits (what the map handles best)
        ("fb01", "func_body", lambda: m_compare_flip("sqlglot/optimizer/simplify.py", r())),
        ("fb02", "func_body", lambda: m_compare_flip("sqlglot/generator.py", r())),
        ("fb03", "func_body", lambda: m_compare_flip("sqlglot/parser.py", r())),
        ("fb04", "func_body", lambda: m_compare_flip("sqlglot/optimizer/qualify_columns.py", r())),
        ("fb05", "func_body", lambda: m_compare_flip("sqlglot/helper.py", r())),
        ("fb06", "func_body", lambda: m_compare_flip("sqlglot/transforms.py", r())),
    ]


# --------------------------------------------------------------- commands --

def suite_dir(name):
    return OUT / name


def cmd_build(_):
    ctx = OUT / "oracle_ctx"
    ctx.mkdir(parents=True, exist_ok=True)
    for f in ["failplugin.py", "run_oracle.py", "Dockerfile"]:
        shutil.copy(ROOT / "tools" / "oracle" / f, ctx / f)
    shutil.copy(ROOT / "requirements.lock", ctx / "requirements.lock")
    return run(["docker", "build", "--platform", "linux/amd64", "-t", ORACLE_IMAGE, str(ctx)]).returncode


def cmd_gen(args):
    if not (SRC / "sqlglot" / "__init__.py").exists():
        sys.exit("sqlglot-src/ missing: extract the pinned source first")
    d = suite_dir(args.suite)
    (d / "commits").mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "sample" / "tests.json", d / "tests.json")
    rng = random.Random(2026)
    manifest = {}
    for mid, cat, maker in mutant_specs(rng):
        try:
            rel, new = maker()
            old = read(rel)
            assert new != old, "no-op edit"
            if rel.endswith(".py"):
                compile(new, rel, "exec")
        except Exception as e:  # skip a mutant whose target moved; report it
            log(f"SKIP {mid} ({cat}): {e!r}")
            continue
        patch = make_diff(rel, old, new)
        (d / "commits" / f"{mid}.patch").write_text(patch, encoding="utf-8", newline="\n")
        changed = [l for l in patch.splitlines() if l[:1] in "+-" and not l.startswith(("+++", "---"))]
        manifest[mid] = {"kind": cat, "file": rel, "change": changed[:4]}
    # patch-format variant: same edit as fb01 with CRLF line endings (ground truth copied from fb01)
    if "fb01" in manifest:
        p = (d / "commits" / "fb01.patch").read_text(encoding="utf-8")
        (d / "commits" / "fx01.patch").write_bytes(p.replace("\n", "\r\n").encode())
        manifest["fx01"] = dict(manifest["fb01"], kind="format_crlf", answer_from="fb01")
    (d / "manifest.json").write_text(json.dumps(manifest, indent=1))
    log(f"wrote {len(manifest)} patches to {d}")


def docker_path(p):
    return str(Path(p).resolve())


def cmd_oracle(args):
    d = suite_dir(args.suite)
    manifest = json.loads((d / "manifest.json").read_text())
    only = args.only.split(",") if args.only else [m for m, v in manifest.items() if "answer_from" not in v]
    rc = run(["docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none", *ORACLE_LIMITS,
              "-v", f"{docker_path(d)}:/mut", "-e", f"ONLY={','.join(only)}",
              "-e", f"ORACLE_WORKERS={args.workers}", ORACLE_IMAGE]).returncode
    write_answers(d, manifest)
    return rc


def write_answers(d, manifest):
    oracle = json.loads((d / "oracle.json").read_text()) if (d / "oracle.json").exists() else {}
    answers = {}
    for mid, info in manifest.items():
        src = oracle.get(info.get("answer_from", mid))
        if src and src["status"] in ("ok", "crash"):
            answers[mid] = {"failing": src["failing"], "kind": info["kind"]}
    (d / "answers.json").write_text(json.dumps({"commits": answers}))
    return answers


def cmd_select(args):
    d = suite_dir(args.suite)
    manifest = json.loads((d / "manifest.json").read_text())
    answers = write_answers(d, manifest)
    # score only commits that have ground truth: stage them in a scoring dir
    stage = d / "_scored"
    if stage.exists():
        shutil.rmtree(stage)
    (stage / "commits").mkdir(parents=True)
    shutil.copy(d / "tests.json", stage / "tests.json")
    for mid in answers:
        shutil.copy(d / "commits" / f"{mid}.patch", stage / "commits" / f"{mid}.patch")
    (stage / "answers.json").write_text(json.dumps({"commits": answers}))
    missing = sorted(set(manifest) - set(answers))
    if missing:
        log(f"no ground truth yet (skipped): {missing}")

    with open(stage / "output.jsonl", "wb") as out, open(stage / "selector.log", "wb") as err:
        rc = run(["docker", "run", *RUN_FLAGS,
                  "-v", f"{docker_path(stage / 'commits')}:/data/commits:ro",
                  "-v", f"{docker_path(stage / 'tests.json')}:/data/tests.json:ro",
                  SELECTOR_IMAGE], stdout=out, stderr=err).returncode
    if rc:
        sys.exit(f"selector container failed; see {stage / 'selector.log'}")
    run([sys.executable, ROOT / "score.py", stage, stage / "answers.json", stage / "output.jsonl"])
    report(stage, answers, manifest)


def report(stage, answers, manifest):
    ms = {t["number"]: round(t["seconds"] * 1000) for t in json.loads((stage / "tests.json").read_text())["tests"]}
    rows, misses = [], []
    for line in (stage / "output.jsonl").read_text().splitlines():
        o = json.loads(line)
        w = o["workers"]
        sel = {n for x in w for n in x}
        cost = (max(sum(ms[n] for n in x) for x in w) + 2000 * len(w)) / 1000 if w else 0.0
        fail = set(answers[o["commit"]]["failing"])
        missed = sorted(fail - sel)
        rows.append((manifest[o["commit"]]["kind"], o["commit"], len(fail), len(sel), cost, missed))
        if missed:
            misses.append(o["commit"])
    print(f"\n{'kind':14s} {'id':6s} {'fail':>5s} {'sel':>5s} {'cost':>7s}  missed")
    for kind, mid, nf, ns, cost, missed in sorted(rows):
        flag = f"MISS {len(missed)}: {missed[:6]}" if missed else ""
        print(f"{kind:14s} {mid:6s} {nf:5d} {ns:5d} {cost:7.2f}  {flag}")
    print(f"\n{len(misses)} missed of {len(rows)}: {misses}")


def cmd_validate(args):
    """Oracle on a few sample commits; must reproduce sample/answers.json exactly."""
    d = suite_dir("validate")
    (d / "commits").mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "sample" / "tests.json", d / "tests.json")
    truth = json.loads((ROOT / "sample" / "answers.json").read_text())["commits"]
    ids = args.only.split(",") if args.only else VALIDATE_IDS
    for i in ids:  # git autocrlf on Windows checks these out as CRLF; the scorer's are LF
        raw = (ROOT / "sample" / "commits" / f"{i}.patch").read_bytes()
        (d / "commits" / f"{i}.patch").write_bytes(raw.replace(b"\r\n", b"\n"))
    (d / "manifest.json").write_text(json.dumps({i: {"kind": truth[i]["kind"]} for i in ids}))
    run(["docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none", *ORACLE_LIMITS,
         "-v", f"{docker_path(d)}:/mut", "-e", f"ONLY={','.join(ids)}",
         "-e", f"ORACLE_WORKERS={args.workers}", ORACLE_IMAGE])
    oracle = json.loads((d / "oracle.json").read_text())
    ok = True
    for i in ids:
        got, want = set(oracle[i]["failing"]), set(truth[i]["failing"])
        same = got == want
        ok &= same
        print(f"{i}: oracle {len(got):3d} failing, answers {len(want):3d} -> "
              f"{'MATCH' if same else f'DIFF extra={sorted(got - want)[:8]} missing={sorted(want - got)[:8]}'}")
    print("oracle validated" if ok else "ORACLE MISMATCH")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build", "gen", "oracle", "select", "validate"])
    ap.add_argument("--suite", default="mutants")
    ap.add_argument("--only", default="")
    ap.add_argument("--workers", default="6")
    args = ap.parse_args()
    sys.exit({"build": cmd_build, "gen": cmd_gen, "oracle": cmd_oracle,
              "select": cmd_select, "validate": cmd_validate}[args.cmd](args) or 0)


if __name__ == "__main__":
    main()
