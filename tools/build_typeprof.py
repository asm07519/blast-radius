#!/usr/bin/env python3
"""Run the whole suite once with the comparison type profiler and fold the results.

    python3 tools/build_typeprof.py --repo /src/sqlglot --tests maps/tests.json --out work/typeprof.json -j 2

Output: {"sites": {"file:line:col": [[left_cat, right_cat, same_type], ...]},
         "incomplete": [test ids whose run stopped early (a failure outside a subtest)]}
A site's profile only describes tests that ran to the end, so the selector ignores a
profile whenever a test listed in "incomplete" runs that site.
"""

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ground_truth import groups  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--tests", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("-j", type=int, default=os.cpu_count())
    ap.add_argument("--stub", default=None)
    ap.add_argument("--timeout", type=int, default=3600)
    args = ap.parse_args()
    repo = os.path.abspath(args.repo)
    tests = json.load(open(args.tests))["tests"]
    work = tempfile.mkdtemp(prefix="typeprof-")
    out_dir = os.path.join(work, "out")
    os.makedirs(out_dir)
    path = [os.path.join(HERE, "typeprof"), os.path.join(HERE, "trace"), repo]
    if args.stub:
        path.append(os.path.abspath(args.stub))
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(path), BR_TYPEPROF_DIR=out_dir, BR_REPO=repo,
               SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1", PYTHONHASHSEED="0")
    env.pop("BR_TRACE_DIR", None)
    procs = []
    for i, group in enumerate(groups(tests, args.j, tests)):
        e = dict(env, BR_RESULT_FILE=os.path.join(work, f"r{i}.json"))
        cmd = [sys.executable, "-m", "pytest", "-p", "br_plugin", "-q", "-p", "no:cacheprovider", "-o",
               "addopts=", "--continue-on-collection-errors", *group]
        procs.append(subprocess.Popen(cmd, cwd=repo, env=e, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    for p in procs:
        p.wait(timeout=args.timeout)

    sites = {}
    for fn in glob.glob(os.path.join(out_dir, "*.json")):
        for site, rows in json.load(open(fn)).items():
            s = sites.setdefault(site, set())
            for lk, lc, rk, rc in rows:
                s.add((lc, rc, lk == rk))
    incomplete = set()
    seen = set()
    for fn in glob.glob(os.path.join(work, "r*.json")):
        res = json.load(open(fn))
        for nodeid, when, outcome, kind in res["reports"]:
            seen.add(nodeid)
            if outcome == "failed" and not (when == "call" and kind == "SubtestReport"):
                incomplete.add(nodeid)
        for nodeid in res["collect_errors"]:
            incomplete.update(t["id"] for t in tests if t["file"] == nodeid.split("::")[0])
    incomplete.update(t["id"] for t in tests if t["id"] not in seen)
    json.dump({"sites": {k: sorted(v) for k, v in sites.items()}, "incomplete": sorted(incomplete)},
              open(args.out, "w"))
    shutil.rmtree(work, ignore_errors=True)
    print(f"{len(sites)} comparison sites profiled, {len(incomplete)} tests incomplete", file=sys.stderr)


if __name__ == "__main__":
    main()
