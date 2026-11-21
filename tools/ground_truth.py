#!/usr/bin/env python3
"""Label patches the way the organisers do: apply, run the whole suite, list failing tests.

    python3 tools/ground_truth.py --repo /src/sqlglot --tests tests.json \
        --patches dir_with_patches --out answers.json -j 4 [--stub tools/stubs]

A test fails when any of its reports (setup, call, teardown, subtest) failed or
its file could not be collected. The pinned commit is run once as a baseline;
tests already failing there (for example because a stand-in replaced a missing
package) are reported separately as "unknown" instead of failing.
Results are cached per patch in <out>.d/ so runs can be resumed.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))


def groups(tests, n, all_tests):
    """Contiguous chunks in collection order (tests.json order), balanced by recorded time.

    Keeping pytest's own order matters: some test files leave global state behind,
    and the labels were made with the suite in its usual order. Whole files are passed
    when every test of a file runs, else node ids.
    """
    total_per_file = {}
    order = []
    for t in all_tests:
        if t["file"] not in total_per_file:
            order.append(t["file"])
        total_per_file[t["file"]] = total_per_file.get(t["file"], 0) + 1
    by_file = {}
    for t in tests:
        by_file.setdefault(t["file"], [0.0, []])
        by_file[t["file"]][0] += t["seconds"] + 0.02
        by_file[t["file"]][1].append(t["id"])
    files = [f for f in order if f in by_file]
    total = sum(by_file[f][0] for f in files)
    chunks, cur, acc = [], [], 0.0
    for f in files:
        s, ids = by_file[f]
        cur.extend([f] if len(ids) == total_per_file[f] else ids)
        acc += s
        if acc >= total / n and len(chunks) < n - 1:
            chunks.append(cur)
            cur, acc = [], 0.0
    if cur:
        chunks.append(cur)
    return chunks


def run_suite(tree, tests, args, all_tests=None):
    """Run these tests in `tree`; return {test id: 'passed'|'failed'|'skipped'|'missing'}."""
    files = groups(tests, args.j, all_tests or tests)
    tmp = tempfile.mkdtemp(prefix="gt-")
    env = dict(os.environ)
    path = [os.path.join(HERE, "trace"), tree]
    if args.stub:
        path.append(args.stub)
    env.update(PYTHONPATH=os.pathsep.join(path), SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1",
               BR_TEST_TIMEOUT=str(args.test_timeout))
    env.pop("BR_TRACE_DIR", None)

    def one(i, group):
        e = dict(env, BR_RESULT_FILE=os.path.join(tmp, f"{i}.json"))
        cmd = [args.python, "-m", "pytest", "-p", "br_plugin", "-q", "-p", "no:cacheprovider",
               "-o", "addopts=", "--continue-on-collection-errors", *group]
        try:
            subprocess.run(cmd, cwd=tree, env=e, capture_output=True, timeout=args.suite_timeout)
        except subprocess.TimeoutExpired:
            return None
        try:
            return json.load(open(e["BR_RESULT_FILE"]))
        except Exception:
            return None

    with ThreadPoolExecutor(len(files)) as pool:
        results = list(pool.map(lambda x: one(*x), enumerate(files)))
    shutil.rmtree(tmp, ignore_errors=True)

    status = {}
    lost_files = set()
    for group, res in zip(files, results):
        if res is None:
            lost_files.update(g.split("::")[0] for g in group)
            continue
        for nodeid, when, outcome, _ in res["reports"]:
            prev = status.get(nodeid)
            if outcome == "failed":
                status[nodeid] = "failed"
            elif prev is None or (prev == "skipped" and outcome == "passed"):
                status[nodeid] = outcome
        for nodeid in res["collect_errors"]:
            lost_files.add(nodeid.split("::")[0])
    out = {}
    for t in tests:
        if t["file"] in lost_files:
            out[t["id"]] = "failed"
        else:
            out[t["id"]] = status.get(t["id"], "missing")
    return out


def make_tree(repo, patch_file):
    tree = tempfile.mkdtemp(prefix="gt-tree-")
    shutil.rmtree(tree)
    shutil.copytree(repo, tree, ignore=shutil.ignore_patterns(".git", "__pycache__"), symlinks=True)
    if patch_file:
        if shutil.which("git"):
            proc = subprocess.run(["git", "apply", "--unsafe-paths", "-p1", os.path.abspath(patch_file)],
                                  cwd=tree, capture_output=True)
            if proc.returncode != 0:
                raise RuntimeError(f"git apply failed: {proc.stderr.decode()[-300:]}")
        else:
            _apply_with_selector_code(tree, patch_file)
    return tree


def _apply_with_selector_code(tree, patch_file):
    sys.path.insert(0, os.path.dirname(HERE))
    from selector import patch as P

    for fp in P.parse(open(patch_file, "rb").read()):
        old_full = os.path.join(tree, fp.old_path) if fp.old_path else None
        old = open(old_full, "rb").read() if old_full and os.path.exists(old_full) else b""
        if fp.binary:
            raise RuntimeError("binary patch needs git")
        new = P.apply(fp, old) if fp.hunks else old
        if fp.old_path and fp.old_path != fp.new_path and not getattr(fp, "copy", False):
            os.remove(old_full)
        if fp.new_path:
            dest = os.path.join(tree, fp.new_path)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as fh:
                fh.write(new)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--tests", required=True)
    ap.add_argument("--patches", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("-j", type=int, default=os.cpu_count())
    ap.add_argument("--stub", default=None)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--test-timeout", type=int, default=120)
    ap.add_argument("--suite-timeout", type=int, default=1800)
    ap.add_argument("--unknown-from-maps", default=None,
                    help="maps.bin whose untrusted tests cannot be judged here (stand-in packages)")
    ap.add_argument("--complement-of", default=None,
                    help="selector output (jsonl): only run tests it did NOT select; any new failure is a miss")
    args = ap.parse_args()
    if getattr(args, "stub", None):
        args.stub = os.path.abspath(args.stub)

    tests = json.load(open(args.tests))["tests"]
    num = {t["id"]: t["number"] for t in tests}
    cache = args.out + ".d"
    os.makedirs(cache, exist_ok=True)

    base_file = os.path.join(cache, "_baseline.json")
    if os.path.exists(base_file):
        baseline = json.load(open(base_file))
    else:
        t0 = time.time()
        tree = make_tree(args.repo, None)
        baseline = run_suite(tree, tests, args)
        shutil.rmtree(tree, ignore_errors=True)
        json.dump(baseline, open(base_file, "w"))
        print(f"baseline in {time.time() - t0:.0f}s", file=sys.stderr)
    bad_base = {tid for tid, s in baseline.items() if s != "passed"}
    if args.unknown_from_maps:
        import marshal
        import zlib

        md = marshal.loads(zlib.decompress(open(args.unknown_from_maps, "rb").read()))
        bad_base |= {tid for i, tid in enumerate(md["ids"]) if md["untrusted"] >> i & 1}
    print(f"baseline: {len(bad_base)} tests not passing", file=sys.stderr)

    patches = sorted(p for p in os.listdir(args.patches) if p.endswith(".patch"))
    selected = None
    if args.complement_of:
        selected = {}
        for line in open(args.complement_of):
            d = json.loads(line)
            selected[d["commit"]] = {t for w in d["workers"] for t in w}
        cache = cache + ".complement"
        os.makedirs(cache, exist_ok=True)
    commits = {}
    for p in patches:
        cid = p[:-6]
        cf = os.path.join(cache, cid + ".json")
        if os.path.exists(cf):
            status = json.load(open(cf))
        else:
            t0 = time.time()
            try:
                tree = make_tree(args.repo, os.path.join(args.patches, p))
            except RuntimeError as error:
                print(f"{cid}: {error}", file=sys.stderr)
                continue
            run_tests = tests
            if selected is not None:
                chosen = selected.get(cid, set())
                run_tests = [t for t in tests if t["number"] not in chosen and t["id"] not in bad_base]
            status = run_suite(tree, run_tests, args, tests) if run_tests else {}
            shutil.rmtree(tree, ignore_errors=True)
            json.dump(status, open(cf, "w"))
            print(f"{cid}: {time.time() - t0:.0f}s", file=sys.stderr)
        failing = sorted(num[tid] for tid, s in status.items() if s == "failed" and tid not in bad_base)
        unknown = sorted(num[tid] for tid in bad_base)
        commits[cid] = {"failing": failing, "unknown": unknown, "kind": "generated"}
        print(f"{cid}: {len(failing)} failing", file=sys.stderr)
    json.dump({"commits": commits}, open(args.out, "w"), indent=0)


if __name__ == "__main__":
    main()
