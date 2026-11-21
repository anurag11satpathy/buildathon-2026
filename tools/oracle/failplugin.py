"""pytest plugin (oracle only): write failed test ids and collection errors to $FAILOUT.

Works with pytest-xdist: worker reports are replayed in the controller, which
is the only process that writes the file.
"""

import json
import os

_failed: set[str] = set()
_collect_errors: set[str] = set()


def pytest_runtest_logreport(report):
    if report.failed:
        _failed.add(report.nodeid)


def pytest_collectreport(report):
    if report.failed:
        _collect_errors.add(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    if hasattr(session.config, "workerinput"):  # xdist worker
        return
    with open(os.environ["FAILOUT"], "w") as f:
        json.dump({"failed": sorted(_failed), "collect_errors": sorted(_collect_errors),
                   "exit": int(exitstatus)}, f)
