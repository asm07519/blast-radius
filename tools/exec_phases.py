#!/usr/bin/env python3
"""Split sqlglot's executor tests into a cheap and an expensive phase (map building only).

    python3 tools/exec_phases.py --repo /src/sqlglot --out work/exec_phases.json [--stub tools/stubs]

test_execute_tpch / test_execute_tpcds do, per query: parse (parent), pickle to a
worker, find_tables, ensure_schema, optimize, render it once for a debug log, build
a Plan, run the Python executor on the data, and compare the rows with duckdb's
answer for transpile(sql, write="duckdb").

  cheap    parse, pickle round trip, find_tables, ensure_schema, optimize, render,
           transpile: re-run at selection time on the patched code (~3 s for TPC-DS)
           and fingerprinted.
  exposed  everything else: importing the test module and loading the data
           (set-up), building the plans (planner), executing them and reading the
           result rows (executor). Each is traced in a fresh process that starts from
           pickled inputs, so a cache filled in an earlier phase cannot hide code.

A change whose lines never run in the exposed phases, and that leaves every cheap
fingerprint unchanged, cannot change the test's outcome.
"""

import argparse
import json
import os
import pickle
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SUITES = {
    "tpcds": {"test": "tests/test_executor.py::TestExecutor::test_execute_tpcds",
              "fixture": "optimizer/tpc-ds/tpc-ds.sql", "schema": "TPCDS_SCHEMA", "tables": "tpcds_tables"},
    "tpch": {"test": "tests/test_executor.py::TestExecutor::test_execute_tpch",
             "fixture": "optimizer/tpc-h/tpc-h.sql", "schema": "TPCH_SCHEMA", "tables": "tpch_tables"},
}


def start_trace(repo):
    lines = set()
    prefix = repo + "/"
    disable = sys.monitoring.DISABLE

    def on_line(code, line):
        fn = code.co_filename
        if fn.startswith(prefix):
            lines.add((fn[len(prefix):], line))
        return disable

    tool = sys.monitoring.PROFILER_ID
    sys.monitoring.use_tool_id(tool, "br-phases")
    sys.monitoring.register_callback(tool, sys.monitoring.events.LINE, on_line)
    sys.monitoring.set_events(tool, sys.monitoring.events.LINE)
    return lines


def dump_lines(lines, out):
    packed = {}
    for f, line in lines:
        packed.setdefault(f, []).append(line)
    with open(out, "w") as fh:
        json.dump({f: sorted(v) for f, v in packed.items()}, fh)


def phase(args):
    repo = os.path.abspath(args.repo)
    lines = start_trace(repo)
    sys.path.insert(0, repo)
    os.chdir(repo)
    os.environ.setdefault("SKIP_INTEGRATION", "0")
    suite = SUITES[args.suite]
    work = args.work
    if args.phase == "setup":
        import tests.test_executor as te

        te.TestExecutor.setUpClass()
        tables = getattr(te.TestExecutor, suite["tables"])
        with open(os.path.join(work, "tables.pkl"), "wb") as fh:
            pickle.dump(tables, fh)
    elif args.phase == "planner":
        from sqlglot.planner import Plan

        optimized = pickle.load(open(os.path.join(work, "optimized.pkl"), "rb"))
        plans = [Plan(o) for o in optimized]
        with open(os.path.join(work, "plans.pkl"), "wb") as fh:
            pickle.dump(plans, fh)
    elif args.phase == "executor":
        from sqlglot import find_tables
        from sqlglot.executor.python import PythonExecutor
        from sqlglot.executor.table import ensure_tables

        data = pickle.load(open(os.path.join(work, "tables.pkl"), "rb"))
        plans = pickle.load(open(os.path.join(work, "plans.pkl"), "rb"))
        parsed = pickle.load(open(os.path.join(work, "parsed.pkl"), "rb"))
        for expression, plan in zip(parsed, plans):
            tables = {t.name: data[t.name] for t in find_tables(expression)}
            tables_ = ensure_tables(tables, dialect=None)
            _ = tables_.supported_table_args
            table = PythonExecutor(tables=tables_).execute(plan)
            rows = [tuple(c for c in r) for r in table.rows]
            _ = (table.columns, rows)
    dump_lines(lines, args.out)


def cheap(repo, suite_name):
    """Run the cheap phase on the pinned code: fingerprints plus pickles for the next phases."""
    sys.path.insert(0, ROOT)
    from selector.exec_fingerprint import fingerprint_suite

    return fingerprint_suite(repo, SUITES[suite_name])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--stub", default=None)
    ap.add_argument("--phase", default=None)
    ap.add_argument("--suite", default=None)
    ap.add_argument("--work", default=None)
    args = ap.parse_args()
    if args.phase:
        return phase(args)

    repo = os.path.abspath(args.repo)
    env = dict(os.environ, PYTHONHASHSEED="0", SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1")
    path = [repo] + ([os.path.abspath(args.stub)] if args.stub else [])
    env["PYTHONPATH"] = os.pathsep.join(path)
    result = {}
    for name, suite in SUITES.items():
        work = tempfile.mkdtemp(prefix=f"phases-{name}-")
        # cheap phase on the pinned code, in a fresh process (also writes parsed/optimized pickles)
        proc = subprocess.run([sys.executable, "-c", (
            "import sys, json, pickle; sys.path.insert(0, %r); sys.path.insert(0, %r);"
            "from selector.exec_fingerprint import fingerprint_suite;"
            "fp, parsed, optimized = fingerprint_suite(%r, %r, keep=True);"
            "pickle.dump(parsed, open(%r, 'wb')); pickle.dump(optimized, open(%r, 'wb'));"
            "json.dump(fp, open(%r, 'w'))") % (ROOT, repo, repo, suite, os.path.join(work, "parsed.pkl"),
                                                 os.path.join(work, "optimized.pkl"), os.path.join(work, "fp.json"))],
            env=env, cwd=repo)
        if proc.returncode != 0:
            sys.exit(f"cheap phase failed for {name}")
        cov = {}
        for ph in ("setup", "planner", "executor"):
            out = os.path.join(work, f"{ph}.json")
            proc = subprocess.run([sys.executable, os.path.abspath(__file__), "--repo", repo, "--out", out,
                                   "--phase", ph, "--suite", name, "--work", work], env=env, cwd=repo)
            if proc.returncode != 0:
                sys.exit(f"phase {ph} failed for {name}")
            cov[ph] = json.load(open(out))
        exposed = {}
        for ph in cov.values():
            for f, lines in ph.items():
                exposed.setdefault(f, set()).update(lines)
        fp = json.load(open(os.path.join(work, "fp.json")))
        result[name] = {"test": suite["test"], "suite": suite, "fingerprint": fp,
                        "exposed": {f: sorted(v) for f, v in exposed.items()}}
        print(f"{name}: {len(fp['queries'])} queries fingerprinted, "
              f"{sum(map(len, exposed.values()))} exposed lines", file=sys.stderr)
    with open(args.out, "w") as fh:
        json.dump(result, fh)


if __name__ == "__main__":
    main()
