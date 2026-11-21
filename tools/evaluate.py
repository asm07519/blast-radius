#!/usr/bin/env python3
"""Run the selector on a directory of patches and score it against known failing tests.

    python3 tools/evaluate.py --patches sample/commits --tests sample/tests.json \
        --answers sample/answers.json [--maps maps/maps.bin] [--src /src/sqlglot]

Prints per-commit cost, misses (with the missed tests) and totals, using the
scoring rules: busiest worker + 2 s per worker, 300 s per miss.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def cost_ms(workers, ms):
    workers = [w for w in workers if w]
    if not workers:
        return 0
    return max(sum(ms[t] for t in w) for w in workers) + 2000 * len(workers)


def oracle_ms(failing, ms):
    sys.path.insert(0, ROOT)
    from selector import schedule

    return cost_ms(schedule.split(failing, ms), ms) if failing else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--patches", required=True)
    ap.add_argument("--tests", required=True)
    ap.add_argument("--answers", required=True)
    ap.add_argument("--maps", default=os.path.join(ROOT, "maps", "maps.bin"))
    ap.add_argument("--src", default="/home/claude/sqlglot")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--only-kind", default=None)
    args = ap.parse_args()

    tests = json.load(open(args.tests))["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    name = {t["number"]: t["id"] for t in tests}
    answers = json.load(open(args.answers))["commits"]

    data = tempfile.mkdtemp(prefix="eval-")
    os.makedirs(os.path.join(data, "commits"))
    shutil.copy(args.tests, os.path.join(data, "tests.json"))
    for p in os.listdir(args.patches):
        if p.endswith(".patch") and p[:-6] in answers:
            shutil.copy(os.path.join(args.patches, p), os.path.join(data, "commits", p))

    env = dict(os.environ, BR_DATA=data, BR_MAPS=args.maps, BR_SRC=args.src, PYTHONPATH=ROOT)
    t0 = time.time()
    proc = subprocess.run([sys.executable, "-m", "selector.main"], env=env, capture_output=True, cwd=ROOT)
    wall = time.time() - t0
    shutil.rmtree(data, ignore_errors=True)
    if proc.returncode != 0:
        print(proc.stderr.decode()[-3000:])
        sys.exit(1)
    logs = {}
    for line in proc.stderr.decode().splitlines():
        if line.startswith("[br] ") and ":" in line:
            cid = line[5:].split(":", 1)[0]
            logs[cid] = line[5:]
    out = {}
    for line in proc.stdout.decode().splitlines():
        d = json.loads(line)
        out[d["commit"]] = d["workers"]

    total = oracle_total = misses = 0
    by_kind = {}
    rows = []
    for cid in sorted(answers):
        a = answers[cid]
        kind = a.get("kind", "?")
        if args.only_kind and kind != args.only_kind:
            continue
        workers = out.get(cid)
        if workers is None:
            print(f"{cid}: MISSING OUTPUT")
            continue
        sel = {t for w in workers for t in w}
        unknown = set(a.get("unknown", []))
        failing = set(a["failing"]) - unknown
        missed = failing - sel
        c = cost_ms(workers, ms)
        o = oracle_ms(sorted(failing), ms)
        total += c + (300000 if missed else 0)
        oracle_total += o
        misses += bool(missed)
        k = by_kind.setdefault(kind, [0, 0, 0, 0])
        k[0] += 1
        k[1] += c + (300000 if missed else 0)
        k[2] += o
        k[3] += bool(missed)
        rows.append((cid, kind, c, o, len(sel), len(failing), missed))
    if not args.quiet:
        for cid, kind, c, o, nsel, nfail, missed in rows:
            flag = "  MISS " + ", ".join(name[t] for t in sorted(missed)[:4]) if missed else ""
            print(f"{cid:28s} {kind:16s} cost {c / 1000:7.2f}  oracle {o / 1000:6.2f}  sel {nsel:4d} fail {nfail:3d}{flag}")
            if missed and cid in logs:
                print("      " + logs[cid][:400])
    print("\nby kind:")
    for kind, (n, c, o, m) in sorted(by_kind.items()):
        print(f"  {kind:16s} n={n:3d} total {c / 1000:8.1f}  oracle {o / 1000:7.1f}  misses {m}")
    print(f"\nTOTAL {total / 1000:.1f} s (oracle {oracle_total / 1000:.1f}), misses {misses}, "
          f"selector wall {wall:.2f}s")


if __name__ == "__main__":
    main()
