#!/usr/bin/env python3
"""Run every test alone, in a fresh traced process, and keep what it depended on.

    python3 tools/build_maps.py --repo /src/sqlglot --tests tests.json --work work -j 4

Each test gets its own interpreter so that caches warmed by one test can never
hide a dependency of another. Results go to <work>/raw/<number>.json and the
run can be resumed. tools/compact_maps.py turns them into the shipped maps.
Several --seed values can be given; their traces are unioned.
"""

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))


def run_one(args, test, seed):
    tmp = tempfile.mkdtemp(prefix="br-", dir=args.tmp)
    env = dict(os.environ)
    path = [os.path.join(HERE, "trace"), args.repo]
    if args.stub:
        path.append(args.stub)
    env.update(
        PYTHONPATH=os.pathsep.join(path),
        BR_TRACE_DIR=tmp,
        BR_REPO=args.repo,
        BR_RESULT_FILE=os.path.join(tmp, "result.json"),
        SKIP_INTEGRATION="0",
        PYTHONHASHSEED=str(seed),
        PYTHONDONTWRITEBYTECODE="1",
    )
    cmd = [
        args.python, "-m", "pytest", "-p", "br_plugin", "-q", "-p", "no:cacheprovider",
        "-o", "addopts=", "--no-header", test["id"],
    ]
    start = time.time()
    try:
        proc = subprocess.run(cmd, cwd=args.repo, env=env, capture_output=True, timeout=args.timeout)
        rc = proc.returncode
        tail = proc.stdout.decode(errors="replace")[-600:]
    except subprocess.TimeoutExpired:
        rc, tail = "timeout", ""
    elapsed = time.time() - start

    call, imp, listed = set(), set(), set()
    call_at_import, classes, classes_at_import = set(), set(), set()
    reads, duck, reports, collect_errors, nproc = {}, set(), [], [], 0
    for fn in glob.glob(os.path.join(tmp, "[0-9]*.json")):
        try:
            d = json.load(open(fn))
        except Exception:
            continue
        nproc += 1
        call.update(map(tuple, d["call"]))
        imp.update(map(tuple, d["import"]))
        listed.update(d["listed"])
        call_at_import.update(map(tuple, d.get("call_at_import", [])))
        classes.update(d.get("classes", []))
        classes_at_import.update(d.get("classes_at_import", []))
        for k, v in d["reads"].items():
            if reads.get(k) != "data":
                reads[k] = v
        duck.update(d["extra"].get("duckdb", []))
        reports += d["extra"].get("report", [])
        collect_errors += d["extra"].get("collect_error", [])
    shutil.rmtree(tmp, ignore_errors=True)

    mine = [r for r in reports if r[0] == test["id"]]
    if any(r[2] == "failed" for r in mine) or collect_errors or rc == "timeout":
        outcome = "failed"
    elif mine and all(r[2] == "skipped" for r in mine):
        outcome = "skipped"
    elif any(r[2] == "passed" for r in mine) and rc == 0:
        outcome = "passed"
    elif any(r[2] == "passed" for r in mine):
        # pytest did not exit cleanly (e.g. killed, rc -9, after reporting a pass): the trace
        # may have lost shards, so the test is not trusted.
        outcome = "unclean-exit"
    else:
        outcome = "missing"
    return {
        "number": test["number"], "id": test["id"], "seed": seed, "outcome": outcome,
        "rc": rc, "elapsed": round(elapsed, 3), "nproc": nproc, "duckdb": sorted(duck),
        "call": sorted(call), "import": sorted(imp), "reads": reads, "listed": sorted(listed),
        "call_at_import": sorted(call_at_import), "classes": sorted(classes),
        "classes_at_import": sorted(classes_at_import),
        "tail": tail if outcome != "passed" else "",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--tests", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("-j", type=int, default=os.cpu_count())
    ap.add_argument("--seed", type=int, action="append")
    ap.add_argument("--stub", default=None, help="extra PYTHONPATH entry with stand-in modules")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--timeout", type=float, default=900)
    ap.add_argument("--tmp", default=None)
    ap.add_argument("--only", default=None, help="comma separated test numbers")
    args = ap.parse_args()
    if getattr(args, "stub", None):
        args.stub = os.path.abspath(args.stub)
    args.repo = os.path.abspath(args.repo)
    seeds = args.seed or [0]

    tests = json.load(open(args.tests))["tests"]
    if args.only:
        keep = {int(x) for x in args.only.split(",")}
        tests = [t for t in tests if t["number"] in keep]
    raw = os.path.join(args.work, "raw")
    os.makedirs(raw, exist_ok=True)
    # Slow tests first so they do not end up alone at the tail of the run.
    tests.sort(key=lambda t: -t["seconds"])

    jobs = []
    for seed in seeds:
        for t in tests:
            out = os.path.join(raw, f"{t['number']}.s{seed}.json")
            if not os.path.exists(out):
                jobs.append((t, seed, out))
    print(f"{len(jobs)} runs to do with {args.j} workers", file=sys.stderr)
    done = 0
    start = time.time()

    def job(t, seed, out):
        # Write the record here and return only a summary: completed futures keep their
        # results, and 3240 full records held to the end ran the parent out of memory.
        rec = run_one(args, t, seed)
        with open(out + ".tmp", "w") as fh:
            json.dump(rec, fh)
        os.replace(out + ".tmp", out)
        return rec["outcome"], rec["elapsed"], rec["rc"]

    killed = []
    with ThreadPoolExecutor(args.j) as pool:
        futures = {pool.submit(job, t, seed, out): (t, seed, out) for t, seed, out in jobs}
        for fut in as_completed(futures):
            t, seed, out = futures[fut]
            try:
                outcome, elapsed, rc = fut.result()
            except Exception as error:
                print(f"error on {t['id']}: {error!r}", file=sys.stderr)
                continue
            done += 1
            if isinstance(rc, int) and rc < 0:
                killed.append((t, seed, out))
            if done % 50 == 0 or outcome != "passed":
                rate = (time.time() - start) / done
                print(f"[{done}/{len(jobs)} {rate:.2f}s/run] {outcome:7s} {t['id']} {elapsed}s", file=sys.stderr)
    # A run killed by a signal (rc -9: the kernel's OOM killer when parallel jobs exceed the
    # memory, e.g. test_max_nodes) says nothing about the test: rerun those once, alone.
    for t, seed, out in killed:
        outcome, elapsed, rc = job(t, seed, out)
        print(f"[retry alone] {outcome:7s} {t['id']} seed {seed} rc {rc} {elapsed}s", file=sys.stderr)


if __name__ == "__main__":
    main()
