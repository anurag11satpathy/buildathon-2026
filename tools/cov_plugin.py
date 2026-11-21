"""pytest plugin used only offline (tools/collect.sh) to build the coverage map.

- Starts coverage as soon as this plugin is imported, so import-time lines are
  recorded too (under the empty context "").
- Switches the coverage context to the test's node id around every test, so
  each covered line knows which tests ran it.
- sqlglot's optimizer/executor tests do their work in spawned
  ProcessPoolExecutor workers. We wrap ProcessPoolExecutor.submit so each job
  carries the submitting test's id; workers run their own coverage (one data
  file per process, .coverage.<host>.<pid>.<rand>) and switch to that context.
- Records every file each test opens (fixtures, SQL files, ...) via an audit
  hook, written to $OUT/opens.json.
"""

import json
import multiprocessing
import os
import sys

import coverage

OUT = os.environ.get("OUT", "/out")
SRC = os.environ.get("SRC", "/src")
IS_WORKER = multiprocessing.parent_process() is not None


def _new_cov(suffix):
    cov = coverage.Coverage(
        data_file=os.path.join(OUT, ".coverage"),
        data_suffix=suffix,
        source=[os.path.join(SRC, "sqlglot")],
        branch=False,
    )
    cov.start()
    return cov


_cov = _new_cov(True if IS_WORKER else None)

if IS_WORKER:
    from multiprocessing.util import Finalize

    _saved = False

    def _save():
        global _saved
        if not _saved:
            _saved = True
            _cov.stop()
            _cov.save()

    # Runs from multiprocessing's exit handler in the child, like coverage's own
    # multiprocessing support; atexit as a backup.
    Finalize(None, _save, exitpriority=100)
    import atexit

    atexit.register(_save)


def _run_in_context(ctx, payload):
    """Executed inside a pool worker: attribute this job's lines to `ctx`.

    The job arrives pre-pickled and the result is round-tripped through pickle
    while the context is set, so sqlglot's pickling code (serde.dump/load via
    Expression.__reduce__) is attributed to the test too.
    """
    import pickle

    _cov.switch_context(ctx)
    try:
        fn, args, kwargs = pickle.loads(payload)
        result = fn(*args, **kwargs)
        pickle.loads(pickle.dumps(result))
        return result
    finally:
        _cov.switch_context("")


_current = ""  # node id of the running test, "" outside tests

if not IS_WORKER:
    import concurrent.futures

    import pytest

    _orig_submit = concurrent.futures.ProcessPoolExecutor.submit

    def _submit(self, fn, /, *args, **kwargs):
        import pickle

        # pickle here, in the parent, while the test's context is active
        payload = pickle.dumps((fn, args, kwargs))
        return _orig_submit(self, _run_in_context, _current, payload)

    concurrent.futures.ProcessPoolExecutor.submit = _submit

    _opens: dict[str, set[str]] = {}

    def _audit(event, args):
        if event != "open" or not args:
            return
        path = args[0]
        if isinstance(path, bytes):
            path = path.decode(errors="replace")
        if not isinstance(path, str):
            return
        path = os.path.abspath(path)
        if not path.startswith(SRC + os.sep) or path.endswith(".pyc"):
            return
        # .py opens outside a test are just normal imports; inside a test they
        # are lazy imports or tests that read source text (e.g. test_typing).
        if path.endswith(".py") and not _current:
            return
        _opens.setdefault(os.path.relpath(path, SRC), set()).add(_current)

    sys.addaudithook(_audit)

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(item, nextitem):
        global _current
        _current = item.nodeid
        _cov.switch_context(item.nodeid)
        yield
        _current = ""
        _cov.switch_context("")

    def pytest_unconfigure(config):
        _cov.stop()
        _cov.save()
        with open(os.path.join(OUT, "opens.json"), "w") as f:
            json.dump({k: sorted(v) for k, v in _opens.items()}, f)
