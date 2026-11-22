#!/usr/bin/env python3
"""Run every test alone with BR_CELLS=1 (logging proxies + dynamic name wrappers).

    python3 tools/build_cells.py --repo /src/sqlglot --tests maps/tests.json --work /work/cells -j 6 --seed 0 --seed 1

Each test runs in a fresh interpreter with BR_CELLS=1 on top of br_tracer.
Results go to <work>/raw/<number>.s<seed>.json (resumable).
tools/compact_cells.py turns them + table_sites.json into the sidecar maps/cells.bin.
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
    tmp = tempfile.mkdtemp(prefix="br-cells-", dir=args.tmp)
    env = dict(os.environ)
    path = [os.path.join(HERE, "trace"), args.repo]
    env.update(
        PYTHONPATH=os.pathsep.join(path),
        BR_TRACE_DIR=tmp,
        BR_REPO=args.repo,
        BR_RESULT_FILE=os.path.join(tmp, "result.json"),
        BR_CELLS="1",
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

    call, imp = set(), set()
    reports, collect_errors, nproc = [], [], 0
    alarms, unproxied, collisions = [], [], set()
    proxies_merged = {}  # tuple(cells) -> {"kind": str, "leaks": set, "keys": set, "whole": bool, "mut": bool}
    dyn_test = set()

    for fn in glob.glob(os.path.join(tmp, "[0-9]*.json")):
        try:
            d = json.load(open(fn))
        except Exception:
            continue
        nproc += 1
        call.update(map(tuple, d.get("call", [])))
        imp.update(map(tuple, d.get("import", [])))
        reports += d.get("extra", {}).get("report", [])
        collect_errors += d.get("extra", {}).get("collect_error", [])
        c = d.get("cells")
        if not c:
            alarms.append("missing cells dump")
            continue
        alarms.extend(c.get("alarms", []))
        unproxied.extend(c.get("unproxied", []))
        collisions.update(c.get("collisions", []))
        for fam, name, op in c.get("dyn", {}).get("test", []):
            dyn_test.add((fam, name, op))
        for prec in c.get("proxies", []):
            ckey = tuple(tuple(x) for x in prec.get("cells", []))
            if not ckey:
                continue
            dst = proxies_merged.get(ckey)
            if dst is None:
                dst = proxies_merged[ckey] = {
                    "kind": prec.get("kind", ""),
                    "leaks": set(),
                    "keys": set(),
                    "whole": False,
                    "mut": False,
                }
            dst["leaks"].update(prec.get("leaks", []))
            t = prec.get("test", {})
            dst["keys"].update(t.get("keys", []))
            if t.get("whole"):
                dst["whole"] = True
            if t.get("mut"):
                dst["mut"] = True
    shutil.rmtree(tmp, ignore_errors=True)

    mine = [r for r in reports if r[0] == test["id"]]
    if any(r[2] == "failed" for r in mine) or collect_errors or rc == "timeout":
        outcome = "failed"
    elif mine and all(r[2] == "skipped" for r in mine):
        outcome = "skipped"
    elif any(r[2] == "passed" for r in mine) and rc == 0:
        outcome = "passed"
    elif any(r[2] == "passed" for r in mine):
        outcome = "unclean-exit"
    else:
        outcome = "missing"

    proxies_out = []
    for ckey, dst in sorted(proxies_merged.items()):
        proxies_out.append({
            "cells": [list(x) for x in ckey],
            "kind": dst["kind"],
            "leaks": sorted(dst["leaks"]),
            "keys": sorted(dst["keys"]),
            "whole": dst["whole"],
            "mut": dst["mut"],
        })

    return {
        "number": test["number"], "id": test["id"], "seed": seed, "outcome": outcome,
        "rc": rc, "elapsed": round(elapsed, 3), "nproc": nproc,
        "call": sorted(call), "import": sorted(imp),
        "alarms": alarms[:50], "unproxied": unproxied, "collisions": sorted(collisions),
        "proxies": proxies_out, "dyn": sorted(list(x) for x in dyn_test),
        "tail": tail if outcome != "passed" else "",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--tests", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("-j", type=int, default=os.cpu_count())
    ap.add_argument("--seed", type=int, action="append")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--timeout", type=float, default=900)
    ap.add_argument("--tmp", default=None)
    ap.add_argument("--only", default=None)
    args = ap.parse_args()
    args.repo = os.path.abspath(args.repo)
    seeds = args.seed or [0]

    tests = json.load(open(args.tests))["tests"]
    if args.only:
        keep = {int(x) for x in args.only.split(",")}
        tests = [t for t in tests if t["number"] in keep]
    raw = os.path.join(args.work, "raw")
    os.makedirs(raw, exist_ok=True)
    tests.sort(key=lambda t: -t["seconds"])

    jobs = []
    for seed in seeds:
        for t in tests:
            out = os.path.join(raw, f"{t['number']}.s{seed}.json")
            if not os.path.exists(out):
                jobs.append((t, seed, out))
    print(f"{len(jobs)} cell runs to do with {args.j} workers", file=sys.stderr)
    done = 0
    start = time.time()

    def job(t, seed, out):
        rec = run_one(args, t, seed)
        with open(out + ".tmp", "w") as fh:
            json.dump(rec, fh)
        os.replace(out + ".tmp", out)
        return rec["outcome"], rec["elapsed"], rec["rc"], len(rec["alarms"])

    killed = []
    with ThreadPoolExecutor(args.j) as pool:
        futures = {pool.submit(job, t, seed, out): (t, seed, out) for t, seed, out in jobs}
        for fut in as_completed(futures):
            t, seed, out = futures[fut]
            try:
                outcome, elapsed, rc, nalarms = fut.result()
            except Exception as error:
                print(f"error on {t['id']}: {error!r}", file=sys.stderr)
                continue
            done += 1
            if isinstance(rc, int) and rc < 0:
                killed.append((t, seed, out))
            if done % 50 == 0 or outcome != "passed" or nalarms:
                rate = (time.time() - start) / done
                print(f"[{done}/{len(jobs)} {rate:.2f}s/run] {outcome:7s} alarms={nalarms} {t['id']} {elapsed}s",
                      file=sys.stderr)
    for t, seed, out in killed:
        outcome, elapsed, rc, nalarms = job(t, seed, out)
        print(f"[retry alone] {outcome:7s} alarms={nalarms} {t['id']} seed {seed} rc {rc} {elapsed}s", file=sys.stderr)


if __name__ == "__main__":
    main()
