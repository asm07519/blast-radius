#!/usr/bin/env python3
"""Label patches the way the organisers do: apply, run the whole suite, list failing tests.

    python3 tools/ground_truth.py --repo /src/sqlglot --tests tests.json \
        --patches dir_with_patches --out answers.json -j 4 [--patch-jobs 3] [--stub tools/stubs]

A test fails when any of its reports (setup, call, teardown, subtest) failed or
its file could not be collected. The pinned commit is run once as a baseline;
tests already failing there (for example because a stand-in replaced a missing
package) are reported separately as "unknown" instead of failing.

Reports that are missing or incomplete never count as passing:

* a test with no report, or whose report set is incomplete (setup passed but no
  call/teardown report), in a pytest run that finished normally is "missing" or
  "incomplete" and listed under the commit's "unknown";
* a pytest process that did not finish normally (killed by --suite-timeout,
  crashed, os._exit, exit code other than 0/1/5, no or unreadable result file) is a
  lost chunk: every test of its files counts as failing, as before, and the ones
  that only fail because of that are also listed under "lost".

-j N splits the suite into N contiguous chunks in catalog order, each in its own
pytest process (fast, but state does not carry across chunks). --serial-order runs
the whole suite in one process in catalog order (the strict suite-order check).
--patch-jobs M labels M patches at once (total processes = M x chunks); a single
patch is labelled exactly the same way either way.

Results are cached per patch in <out>.d/ (atomic writes, keyed by the patch's sha256
and the run settings) so runs can be resumed; a truncated or stale cache entry is
recomputed, never read as a result.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_VERSION = 2
NORMAL_EXIT = (0, 1, 5)  # all passed, some failed, nothing collected
_print_lock = threading.Lock()

# Passive, ground-truth-only plugin: appends every report as it happens, so a pytest
# process that dies still leaves a record of what it finished (diagnostics only).
STREAM_PLUGIN = '''
import json
import os

_out = None


def _write(entry):
    global _out
    if _out is None:
        _out = open(os.environ["GT_STREAM_FILE"], "a", buffering=1)
    _out.write(json.dumps(entry) + "\\n")
    _out.flush()


def pytest_runtest_logstart(nodeid, location):
    _write(["start", nodeid, "", ""])


def pytest_runtest_logreport(report):
    _write(["report", report.nodeid, report.when, report.outcome])


def pytest_collectreport(report):
    if report.failed:
        _write(["collect_error", report.nodeid, "", ""])
'''


def log(msg):
    with _print_lock:
        print(msg, file=sys.stderr, flush=True)


def write_json_atomic(path, obj, **kw):
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, **kw)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


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


def _read_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return None


def _read_stream(path):
    out = []
    try:
        with open(path) as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    break  # a line cut by a kill
    except OSError:
        pass
    return out


def _judge(reports):
    """Outcome of one test from its [when, outcome] reports: passed/failed/skipped/incomplete."""
    if any(o == "failed" for _, o in reports):
        return "failed"
    whens = {w for w, _ in reports}
    setup = [o for w, o in reports if w == "setup"]
    if not setup:
        return "incomplete"
    if setup[0] == "skipped":
        return "skipped"
    if "call" not in whens or "teardown" not in whens:
        return "incomplete"
    calls = [o for w, o in reports if w == "call"]
    return "skipped" if calls and all(o == "skipped" for o in calls) else "passed"


def _collect_error_hits(nodeid, files):
    """Test files a collection error covers: the file itself, or everything under a directory."""
    path = nodeid.split("::")[0].rstrip("/")
    if path in ("", "."):
        return set(files)
    return {f for f in files if f == path or f.startswith(path + "/")}


def run_suite(tree, tests, args, all_tests=None):
    """Run these tests in `tree`; return ({test id: status}, info).

    status is 'passed' | 'failed' | 'skipped' | 'missing' | 'incomplete'; info lists the
    lost chunks and the tests that only fail because their chunk was lost.
    """
    files = groups(tests, 1 if args.serial_order else args.j, all_tests or tests)
    tmp = tempfile.mkdtemp(prefix="gt-", dir=args.tmp_dir)
    with open(os.path.join(tmp, "_gt_stream.py"), "w") as fh:
        fh.write(STREAM_PLUGIN)
    env = dict(os.environ)
    path = [os.path.join(HERE, "trace"), tmp, tree]
    if args.stub:
        path.append(args.stub)
    env.update(PYTHONPATH=os.pathsep.join(path), SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1",
               BR_TEST_TIMEOUT=str(args.test_timeout))
    for var in ("BR_TRACE_DIR", "PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        env.pop(var, None)
    # Tests that start `python` themselves (test_lazy_load) get the labelling interpreter.
    env["PATH"] = os.path.dirname(os.path.abspath(args.python)) + os.pathsep + env.get("PATH", "")
    if args.hash_seed is not None:
        env["PYTHONHASHSEED"] = str(args.hash_seed)

    def one(i, group):
        # A test file the patch deleted or renamed away is not passed to pytest (it would
        # stop the whole process with a usage error); its tests come back "missing".
        group = [g for g in group if os.path.exists(os.path.join(tree, g.split("::")[0]))]
        result = os.path.join(tmp, f"{i}.json")
        stream = os.path.join(tmp, f"{i}.stream")
        e = dict(env, BR_RESULT_FILE=result, GT_STREAM_FILE=stream)
        if not group:
            return {"reports": [], "collect_errors": []}, None, []
        cmd = [args.python, "-m", "pytest", "-p", "br_plugin", "-p", "_gt_stream", "-q", "-p", "no:cacheprovider",
               "-o", "addopts=", "--continue-on-collection-errors", *group]
        why = None
        try:
            proc = subprocess.run(cmd, cwd=tree, env=e, capture_output=True, timeout=args.suite_timeout)
            if proc.returncode not in NORMAL_EXIT:
                why = f"pytest exit code {proc.returncode}: " + proc.stdout.decode(errors="replace")[-300:].strip()
        except subprocess.TimeoutExpired:
            why = f"suite timeout {args.suite_timeout}s"
        res = _read_json(result)
        if why is None and not (isinstance(res, dict) and isinstance(res.get("reports"), list)
                                and isinstance(res.get("collect_errors"), list)):
            why = "no readable result file"
        return (None if why else res), why, _read_stream(stream)

    with ThreadPoolExecutor(max(1, len(files))) as pool:
        results = list(pool.map(lambda x: one(*x), enumerate(files)))
    shutil.rmtree(tmp, ignore_errors=True)

    test_files = sorted({t["file"] for t in tests})
    reports = {}
    failed_files = set()
    lost_files = set()
    lost_chunks = []
    streamed_fail = set()
    for i, (group, (res, why, stream)) in enumerate(zip(files, results)):
        if res is None:
            chunk_files = {g.split("::")[0] for g in group}
            lost_files.update(chunk_files)
            running = [n for kind, n, _, _ in stream if kind == "start"]
            lost_chunks.append({"chunk": i, "reason": why, "files": len(chunk_files),
                                "last_started": running[-1] if running else None})
            streamed_fail.update(n for kind, n, _, o in stream if kind == "report" and o == "failed")
            for kind, n, _, _ in stream:
                if kind == "collect_error":
                    failed_files.update(_collect_error_hits(n, test_files))
            continue
        for nodeid, when, outcome, _ in res["reports"]:
            reports.setdefault(nodeid, []).append((when, outcome))
        for nodeid in res["collect_errors"]:
            failed_files.update(_collect_error_hits(nodeid, test_files))
    out = {}
    lost = []
    for t in tests:
        tid = t["id"]
        if t["file"] in lost_files:
            out[tid] = "failed"
            if tid not in streamed_fail and t["file"] not in failed_files:
                lost.append(tid)
        elif t["file"] in failed_files:
            out[tid] = "failed"
        elif tid in reports:
            out[tid] = _judge(reports[tid])
        else:
            out[tid] = "missing"
    return out, {"lost_chunks": lost_chunks, "lost": lost}


def make_tree(repo, patch_file, tmp_dir=None):
    tree = tempfile.mkdtemp(prefix="gt-tree-", dir=tmp_dir)
    shutil.rmtree(tree)
    shutil.copytree(repo, tree, ignore=shutil.ignore_patterns(".git", "__pycache__"), symlinks=True)
    if patch_file:
        if shutil.which("git"):
            # Outside any repository: inside one, git apply silently skips paths outside the
            # current directory and exits 0, which would label the unpatched code.
            env = dict(os.environ, GIT_CEILING_DIRECTORIES=os.path.dirname(tree))
            for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
                env.pop(var, None)
            proc = subprocess.run(["git", "apply", "--unsafe-paths", "-p1", os.path.abspath(patch_file)],
                                  cwd=tree, capture_output=True, env=env)
            if proc.returncode != 0:
                shutil.rmtree(tree, ignore_errors=True)
                raise RuntimeError(f"git apply failed: {proc.stderr.decode(errors='replace')[-300:]}")
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


def run_settings(args, complement):
    """What a cached result depends on besides the patch (a mismatch recomputes it)."""
    return {"version": CACHE_VERSION, "serial_order": bool(args.serial_order), "hash_seed": args.hash_seed,
            "test_timeout": args.test_timeout, "complement": complement, "python": args.python,
            "stub": bool(args.stub), "python_on_path": True}


def load_cached(path, settings, patch_sha, test_ids, accept_legacy):
    """A cached status dict, or None when absent, truncated, stale or incomplete."""
    data = _read_json(path)
    if data is None:
        return None
    if isinstance(data, dict) and data.get("cache_version") == CACHE_VERSION:
        if data.get("settings") != settings or data.get("patch_sha256") != patch_sha:
            return None
        status = data.get("status")
        info = data.get("info", {})
    elif accept_legacy and isinstance(data, dict) and all(isinstance(v, str) for v in data.values()):
        status, info = data, {"legacy_cache": True}
    else:
        return None
    if not isinstance(status, dict) or not all(tid in status for tid in test_ids):
        return None
    return status, info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--tests", required=True)
    ap.add_argument("--patches", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("-j", type=int, default=os.cpu_count(), help="contiguous pytest chunks per patch")
    ap.add_argument("--serial-order", action="store_true",
                    help="whole suite in one pytest process in catalog order (strict suite-order check)")
    ap.add_argument("--patch-jobs", type=int, default=1, help="patches labelled concurrently")
    ap.add_argument("--hash-seed", default=None, help="PYTHONHASHSEED for the test processes (default: inherited)")
    ap.add_argument("--tmp-dir", default=None, help="where patched trees and reports go (default: system temp)")
    ap.add_argument("--only", default=None, help="comma separated patch ids to label (others are skipped)")
    ap.add_argument("--accept-legacy-cache", action="store_true",
                    help="reuse cache files written before cache version 2 (no settings/patch check)")
    ap.add_argument("--stub", default=None)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--test-timeout", type=int, default=120)
    ap.add_argument("--suite-timeout", type=int, default=1800)
    ap.add_argument("--unknown-from-maps", default=None,
                    help="maps.bin whose untrusted tests cannot be judged here (stand-in packages)")
    ap.add_argument("--complement-of", default=None,
                    help="selector output (jsonl): only run tests it did NOT select; any new failure is a miss")
    args = ap.parse_args()
    if args.stub:
        args.stub = os.path.abspath(args.stub)
    if args.tmp_dir:
        args.tmp_dir = os.path.abspath(args.tmp_dir)
        os.makedirs(args.tmp_dir, exist_ok=True)

    tests = json.load(open(args.tests))["tests"]
    num = {t["id"]: t["number"] for t in tests}
    all_ids = [t["id"] for t in tests]
    cache = args.out + ".d"
    os.makedirs(cache, exist_ok=True)

    base_settings = run_settings(args, False)
    base_file = os.path.join(cache, "_baseline.json")
    cached = load_cached(base_file, base_settings, None, all_ids, args.accept_legacy_cache)
    if cached:
        baseline, base_info = cached
    else:
        t0 = time.time()
        tree = make_tree(args.repo, None, args.tmp_dir)
        baseline, base_info = run_suite(tree, tests, args)
        shutil.rmtree(tree, ignore_errors=True)
        write_json_atomic(base_file, {"cache_version": CACHE_VERSION, "settings": base_settings,
                                      "patch_sha256": None, "status": baseline, "info": base_info})
        log(f"baseline in {time.time() - t0:.0f}s")
    if base_info.get("lost_chunks"):
        log(f"WARNING: baseline lost chunks: {base_info['lost_chunks']}")
    # Skipped in the baseline stays judgeable (it can start failing), as before.
    bad_base = {tid for tid, s in baseline.items() if s not in ("passed", "skipped")}
    if args.unknown_from_maps:
        import marshal
        import zlib

        md = marshal.loads(zlib.decompress(open(args.unknown_from_maps, "rb").read()))
        bad_base |= {tid for i, tid in enumerate(md["ids"]) if md["untrusted"] >> i & 1}
    counts = {}
    for tid in bad_base:
        counts[baseline.get(tid, "untrusted")] = counts.get(baseline.get(tid, "untrusted"), 0) + 1
    log(f"baseline: {len(bad_base)} tests not passing {counts}")

    patches = sorted(p for p in os.listdir(args.patches) if p.endswith(".patch"))
    if args.only:
        keep = set(args.only.split(","))
        patches = [p for p in patches if p[:-6] in keep]
    selected = None
    complement = False
    if args.complement_of:
        complement = True
        selected = {}
        for line in open(args.complement_of):
            d = json.loads(line)
            selected[d["commit"]] = {t for w in d["workers"] for t in w}
        cache = cache + ".complement"
        os.makedirs(cache, exist_ok=True)
    settings = run_settings(args, complement)

    def label(p):
        cid = p[:-6]
        pf = os.path.join(args.patches, p)
        with open(pf, "rb") as fh:
            patch_sha = hashlib.sha256(fh.read()).hexdigest()
        run_tests = tests
        if selected is not None:
            chosen = selected.get(cid, set())
            run_tests = [t for t in tests if t["number"] not in chosen and t["id"] not in bad_base]
        cf = os.path.join(cache, cid + ".json")
        cached = load_cached(cf, settings, patch_sha, [t["id"] for t in run_tests], args.accept_legacy_cache)
        if cached:
            return cid, cached[0], cached[1], None
        t0 = time.time()
        try:
            tree = make_tree(args.repo, pf, args.tmp_dir)
        except RuntimeError as error:
            log(f"{cid}: {error}")
            return cid, None, None, str(error)
        try:
            status, info = run_suite(tree, run_tests, args, tests) if run_tests else ({}, {})
        finally:
            shutil.rmtree(tree, ignore_errors=True)
        write_json_atomic(cf, {"cache_version": CACHE_VERSION, "settings": settings, "patch_sha256": patch_sha,
                               "status": status, "info": info})
        log(f"{cid}: {time.time() - t0:.0f}s")
        return cid, status, info, None

    commits = {}
    unapplied = {}
    with ThreadPoolExecutor(max(1, args.patch_jobs)) as pool:
        for cid, status, info, error in pool.map(label, patches):
            if status is None:
                unapplied[cid] = error
                continue
            failing = sorted(num[tid] for tid, s in status.items() if s == "failed" and tid not in bad_base)
            unjudged = sorted(num[tid] for tid, s in status.items()
                              if s in ("missing", "incomplete") and tid not in bad_base)
            unknown = sorted(set(num[tid] for tid in bad_base) | set(unjudged))
            entry = {"failing": failing, "unknown": unknown, "kind": "generated"}
            lost = sorted(num[tid] for tid in info.get("lost", []) if tid not in bad_base)
            if lost:
                entry["lost"] = lost
                entry["lost_chunks"] = info.get("lost_chunks", [])
            if unjudged:
                entry["unjudged"] = unjudged
            commits[cid] = entry
            note = f" ({len(lost)} only from lost chunks)" if lost else ""
            note += f", {len(unjudged)} without a complete report (unknown)" if unjudged else ""
            log(f"{cid}: {len(failing)} failing{note}")
    out = {"commits": commits}
    if unapplied:
        out["unapplied"] = unapplied
    write_json_atomic(args.out, out, indent=0)


if __name__ == "__main__":
    main()
