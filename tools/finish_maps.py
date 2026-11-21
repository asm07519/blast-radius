#!/usr/bin/env python3
"""Image-build step: retrace the tests the shipped maps do not trust, then merge.

Maps built without some test dependency (duckdb, pytz) mark the tests that needed
it as untrusted, and the selector adds those tests to every non-trivial commit.
When the build has the real packages, this retraces just those tests and merges
them in. It stops starting new tests after --budget seconds; whatever is not
retraced stays untrusted, which is safe. Never fails the build.
"""

import argparse
import marshal
import os
import signal
import subprocess
import sys
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))


def _have_deps():
    code = "import duckdb, pytz, pytest, pandas"
    return subprocess.run([sys.executable, "-c", code], capture_output=True).returncode == 0


# Peak resident memory of one traced pytest with duckdb/pandas loaded (~0.8 GB measured
# under emulation; 18 parallel jobs in an 8 GB VM were OOM-killed, rc -9 = untrusted).
JOB_MEMORY = 1 << 30


def default_jobs():
    """CPU count, capped so the traced jobs fit in the available memory."""
    jobs = os.cpu_count() or 1
    try:
        with open("/proc/meminfo") as fh:
            info = dict(line.split(":", 1) for line in fh)
        available = int(info["MemAvailable"].split()[0]) * 1024
        jobs = min(jobs, max(1, available // JOB_MEMORY))
    except (OSError, KeyError, ValueError):
        pass
    return jobs


def run_group(cmd, timeout):
    """Run cmd in its own process group; on timeout kill the whole group (the traced
    pytest children too, so they stop competing for CPU with the steps that follow)."""
    proc = subprocess.Popen(cmd, start_new_session=True)
    try:
        return proc.wait(timeout=max(timeout, 1))
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        raise


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--maps", required=True)
    ap.add_argument("--tests", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--work", default="/tmp/finish")
    ap.add_argument("--budget", type=float, default=240)
    ap.add_argument("-j", type=int, default=default_jobs())
    ap.add_argument("--requirements", default=None, help="pip install this first if packages are missing")
    args = ap.parse_args()

    data = marshal.loads(zlib.decompress(open(args.maps, "rb").read()))
    ids, bad = data["ids"], data["untrusted"]
    todo = [i for i in range(len(ids)) if bad >> i & 1]
    print(f"[finish] {len(todo)} untrusted tests", file=sys.stderr)
    if not todo:
        return
    start = time.time()
    if args.requirements and not _have_deps():
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "--no-cache-dir", "-q", "-r", args.requirements],
                           timeout=args.budget)
        except subprocess.TimeoutExpired:
            pass
    if not _have_deps():
        print("[finish] test dependencies missing; keeping maps as they are", file=sys.stderr)
        return
    args.budget -= time.time() - start
    cmd = [sys.executable, os.path.join(HERE, "build_maps.py"), "--repo", args.repo, "--tests", args.tests,
           "--work", args.work, "-j", str(args.j), "--only", ",".join(map(str, todo))]
    print(f"[finish] retracing with {args.j} jobs, {args.budget:.0f}s left", file=sys.stderr)
    try:
        run_group(cmd, args.budget)
    except subprocess.TimeoutExpired:
        print("[finish] budget reached; merging what finished", file=sys.stderr)
    raw = os.path.join(args.work, "raw")
    done = [f for f in os.listdir(raw) if f.endswith(".json")] if os.path.isdir(raw) else []
    print(f"[finish] retraced {len(done)} tests in {time.time() - start:.0f}s", file=sys.stderr)
    if not done:
        return
    out = args.maps + ".new"
    static = os.path.join(args.work, "static.json")
    classes = os.path.join(args.work, "classes.json")
    # Each step below is bounded; on any failure or timeout the shipped maps stay as they are.
    # (Natively each takes a few seconds; emulated on a busy machine static_index took > 30 s.)
    steps = [
        ([sys.executable, os.path.join(HERE, "static_index.py"), "--repo", args.repo, "--out", static], 90),
        ([sys.executable, os.path.join(HERE, "class_info.py"), "--repo", args.repo, "--out", classes], 90),
        ([sys.executable, os.path.join(HERE, "compact_maps.py"), "--tests", args.tests, "--base", args.maps,
          "--raw", raw, "--out", out, "--static", static, "--classes", classes], 90),
    ]
    for cmd, limit in steps:
        try:
            if run_group(cmd, limit) != 0:
                print(f"[finish] {os.path.basename(cmd[1])} failed; keeping maps", file=sys.stderr)
                return
        except subprocess.TimeoutExpired:
            print(f"[finish] {os.path.basename(cmd[1])} timed out; keeping maps", file=sys.stderr)
            return
    if os.path.exists(out):
        os.replace(out, args.maps)
        print(f"[finish] merged in {time.time() - start:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # the build must not fail because of this optional step
        print(f"[finish] skipped after error: {error!r}", file=sys.stderr)
