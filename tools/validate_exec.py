#!/usr/bin/env python3
"""Check executor-test drops: run the whole TPC pipeline under a patch, compare with the original.

    python3 tools/validate_exec.py --repo /src/sqlglot --patches dir --commits id1,id2 [--stub tools/stubs]

For every executed query: parse, pickle round trip, sqlglot.executor.execute over the
real data (exactly what the test's workers do), and transpile(sql, write="duckdb").
If rows, columns and duckdb SQL are identical to the pinned code's, the test's outcome
cannot change. Prints SAME or DIFFERENT per commit and suite.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

WORKER = r'''
import sys, json, pickle, hashlib
sys.path.insert(0, sys.argv[1])
import os
os.chdir(sys.argv[1])
os.environ["SKIP_INTEGRATION"] = "0"
from sqlglot import find_tables, parse_one, transpile
from sqlglot.executor import execute
import tests.test_executor as te
from tests import helpers
te.TestExecutor.setUpClass()
out = {}
for suite, fixture, schema_name, tables_attr in [("tpcds", "optimizer/tpc-ds/tpc-ds.sql", "TPCDS_SCHEMA", "tpcds_tables"),
                                                 ("tpch", "optimizer/tpc-h/tpc-h.sql", "TPCH_SCHEMA", "tpch_tables")]:
    data = getattr(te.TestExecutor, tables_attr)
    schema = getattr(helpers, schema_name)
    res = []
    for meta, sql, _ in helpers.load_sql_fixture_pairs(fixture):
        try:
            e = pickle.loads(pickle.dumps(parse_one(sql)))
        except Exception as error:
            res.append("parse " + type(error).__name__); continue
        if not meta.get("execute"):
            res.append("skip"); continue
        try:
            tables = {t.name: data[t.name] for t in find_tables(e)}
            table = execute(e, schema=schema, tables=tables)
            body = repr((table.columns, [tuple(r) for r in table.rows]))
        except Exception as error:
            body = "raise " + type(error).__name__
        try:
            duck = transpile(sql, write="duckdb")[0]
        except Exception as error:
            duck = "raise " + type(error).__name__
        res.append(hashlib.sha256((body + "|" + duck).encode()).hexdigest()[:16])
    out[suite] = res
print(json.dumps(out))
'''


def run_tree(tree, stub):
    env = dict(os.environ, PYTHONHASHSEED="0", SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1")
    path = [tree] + ([stub] if stub else [])
    env["PYTHONPATH"] = os.pathsep.join(path)
    proc = subprocess.run([sys.executable, "-c", WORKER, tree], capture_output=True, env=env, timeout=900)
    lines = proc.stdout.decode().strip().splitlines()
    if not lines:
        return {"error": proc.stderr.decode()[-500:]}
    return json.loads(lines[-1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--patches", required=True)
    ap.add_argument("--commits", required=True)
    ap.add_argument("--stub", default=None)
    args = ap.parse_args()
    stub = os.path.abspath(args.stub) if args.stub else None
    sys.path.insert(0, HERE)
    from ground_truth import make_tree

    base = run_tree(os.path.abspath(args.repo), stub)
    for cid in args.commits.split(","):
        tree = make_tree(os.path.abspath(args.repo), os.path.join(args.patches, cid + ".patch"))
        try:
            got = run_tree(tree, stub)
        finally:
            shutil.rmtree(tree, ignore_errors=True)
        for suite in ("tpcds", "tpch"):
            if "error" in got:
                print(f"{cid} {suite}: ERROR {got['error'][-200:]}")
                continue
            diff = [i for i, (a, b) in enumerate(zip(base[suite], got[suite])) if a != b]
            print(f"{cid} {suite}: {'SAME' if not diff else 'DIFFERENT queries ' + str(diff[:6])}", flush=True)


if __name__ == "__main__":
    main()
