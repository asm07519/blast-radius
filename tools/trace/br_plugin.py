"""pytest plugin for map building and ground truth runs.

* Exposes the current phase (collect/setup/call/teardown) in BR_PHASE so the
  duckdb stand-in can tell when a test body depends on it.
* Records every report. A test fails if any of its reports (setup, call,
  teardown or a subtest) failed, or if its file could not be collected.
"""

import json
import os

import pytest

_reports = []
_collect_errors = []


def _note(key, value):
    try:
        import br_tracer

        br_tracer.note(key, value)
    except Exception:
        pass


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    os.environ["BR_PHASE"] = "setup"
    yield


class _TestTimeout(Exception):
    pass


def _alarm(signum, frame):
    raise _TestTimeout("test timed out (BR_TEST_TIMEOUT)")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    os.environ["BR_PHASE"] = "call"
    limit = int(os.environ.get("BR_TEST_TIMEOUT", "0") or 0)
    if limit:
        import signal

        old = signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(limit)
        try:
            yield
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)
    else:
        yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    os.environ["BR_PHASE"] = "teardown"
    yield


def pytest_runtest_logreport(report):
    entry = [report.nodeid, report.when, report.outcome, type(report).__name__]
    _reports.append(entry)
    _note("report", entry)


def pytest_collectreport(report):
    if report.failed:
        _collect_errors.append(report.nodeid)
        _note("collect_error", report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    out = os.environ.get("BR_RESULT_FILE")
    if out:
        with open(out, "w") as fh:
            json.dump({"reports": _reports, "collect_errors": _collect_errors}, fh)
