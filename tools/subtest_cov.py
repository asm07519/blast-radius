#!/usr/bin/env python3
"""Per-subtest coverage of one unittest test method (map building only).

    python3 tools/subtest_cov.py --repo /src/sqlglot \
        --test tests/test_generator.py::TestGenerator::test_generator_memory --out work/subtests.json

Runs the test in this process with a line tracer. Each `self.subTest(...)` opens a
window; line events are re-armed at every window boundary so each window records
every line it runs. Lines run outside any window (imports, set-up, the loop itself)
are recorded separately. Output:
  {"test": id, "outside": {file: [lines]}, "windows": [[params, {file: [lines]}], ...], "passed": bool}
"""

import argparse
import contextlib
import json
import os
import sys
import unittest

TOOL = sys.monitoring.PROFILER_ID


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--test", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    repo = os.path.abspath(args.repo)
    prefix = repo + "/"
    sys.path.insert(0, repo)
    os.chdir(repo)
    os.environ.setdefault("SKIP_INTEGRATION", "0")

    outside = set()
    windows = []
    state = {"cur": outside}
    disable = sys.monitoring.DISABLE

    def on_line(code, line):
        fn = code.co_filename
        if fn.startswith(prefix):
            state["cur"].add((fn[len(prefix):], line))
        return disable

    sys.monitoring.use_tool_id(TOOL, "br-subtests")
    sys.monitoring.register_callback(TOOL, sys.monitoring.events.LINE, on_line)
    sys.monitoring.set_events(TOOL, sys.monitoring.events.LINE)

    original = unittest.TestCase.subTest

    @contextlib.contextmanager
    def sub_test(self, msg=None, **params):
        prev = state["cur"]
        win = set()
        shown = {k: str(getattr(v, "value", v)) for k, v in params.items()}
        if msg is not None:
            shown["_msg"] = str(msg)
        windows.append((shown, win))
        state["cur"] = win
        sys.monitoring.restart_events()
        try:
            with original(self, msg, **params):
                yield
        finally:
            state["cur"] = prev
            sys.monitoring.restart_events()

    unittest.TestCase.subTest = sub_test

    path, cls_name, method = args.test.split("::")
    module = path[:-3].replace("/", ".")
    import importlib

    mod = importlib.import_module(module)
    case = getattr(mod, cls_name)(method)
    result = unittest.TestResult()
    case.run(result)
    sys.monitoring.set_events(TOOL, 0)

    def pack(lines):
        out = {}
        for f, line in lines:
            out.setdefault(f, []).append(line)
        return {f: sorted(v) for f, v in out.items()}

    data = {
        "test": args.test,
        "outside": pack(outside),
        "windows": [[params, pack(lines)] for params, lines in windows],
        "passed": result.wasSuccessful(),
    }
    with open(args.out, "w") as fh:
        json.dump(data, fh)
    print(f"{args.test}: {len(windows)} subtests, passed={data['passed']}", file=sys.stderr)


if __name__ == "__main__":
    main()
