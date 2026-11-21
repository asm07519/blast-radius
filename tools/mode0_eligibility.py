#!/usr/bin/env python3
"""Certify which numbered tests may be verified directly by Mode 0 (pinned-commit facts only).

    python3 tools/mode0_eligibility.py --repo SRC --tests tests.json --out mode0.json \
        [--python PY] [-j N] [--engines unittest,pytest] [--seeds 0,1] [--budget SECONDS] \
        [--site DIR ...] [--also-batch] [--max-seconds S] [--sets sets.json] \
        [--overlay memory,fs] [--cpu-count N] [--overlay-dir DIR] [--trust-unittest-without-pytest]

Every test runs alone in a fresh Mode 0 runner process on the UNPATCHED source (patched = {}),
launched exactly as the selector launches it (selector.mode0_runner.execute):

    run "iso"     PY -I -S -B selector/replica_runner.py spec.json     (random hash seed: -I
                  ignores PYTHONHASHSEED; this is the production launch, m0.ISO_FLAGS)
    run "seedN"   PY -s -P -S -B selector/replica_runner.py spec.json  with every PYTHON* variable
                  removed from the environment and PYTHONHASHSEED=N (-s -P are -I minus -E)

Overlay modes (--overlay, default "memory"):
    memory  the in-memory overlay (spec without "overlay"): src = --repo.
    fs      a fresh filesystem overlay of --repo with NO changes per run (selector/overlay.py;
            spec "overlay": "fs", "shadowed": [], "pinned": --repo), launched with
            `-X cpu_count=N` (--cpu-count, default 2; 0 = no cap). Multiprocessing spawn/fork
            children started in the overlay are allowed there (flag mp_spawn / mp_fork).

A test is eligible for an engine in a mode when every run of that engine in that mode reached
it and reported "passed" with no unsafe flag (m0.UNSAFE_FLAGS); for unittest, ONLY when the
pytest engine ran in the same mode in this certification and passed it too (unless
--trust-unittest-without-pytest, a debugging switch recorded in the header); with
--also-batch, also when running all catalog tests of its file in one process (per engine and
mode, catalog order) gives the same outcome as running it alone (otherwise it is
order_sensitive and ineligible for every engine and mode).

Output (schema br-mode0-eligibility/1; fields only ever added):
    header      ... as before, plus "overlays": [modes run], "fs": {"cpu_count", "overlay_digest"
                (sha256 of selector/overlay.py), "build_cpu"/"remove_cpu" {median, p90, n} (tool
                CPU per overlay), "runs"} when fs ran; "counts"."eligible_fs" {engine: n};
                "complete" (false when the budget cut the run short, a job was skipped or an
                overlay could not be built: tests not reached are then simply not eligible);
                "engines_compared" (both engines ran in every certified mode);
                "trust_unittest_without_pytest"; "source_digest" = m0.source_digest(repo), the
                CONTENT of the whole pinned tree ("source_digest_scope": "tree"); "env_digest" =
                m0.env_digest(site dirs) of the certified interpreter (version, implementation,
                machine and every *.dist-info/*.egg-info name in the site dirs; "site" lists
                them in order); "pad" [frame depth, C levels] and "assert_watch" as reported by
                the runner; "batch_modes".
    summary     "eligible" {engine: [numbers]} = MEMORY mode, unchanged in meaning (empty when
                memory was not run); "eligible_fs" {engine: [numbers]}; "ineligible_reasons_fs";
                "mode_differences" [{id, engine, memory, fs}] where the iso outcomes differ.
    tests[id]   "n", "s", "<engine>": memory-mode verdict as before ({"ok", "why", "o" per run,
                "cpu" test CPU of the runner process, "proc" whole runner CPU INCLUDING reaped
                children, "flags", "detail"?}); "fs": {"<engine>": the same verdict for fs mode,
                plus "children" = max mp_children reported}.
                With --also-batch: "batch" {engine: outcome in the per-file batch run (memory)}
                and "bcpu" {engine: the test's own WARM CPU seconds in that batch run}; for fs
                mode the same two under "fs".

--sets FILE ({name: [test ids]}) additionally measures the CPU of running each set in ONE
runner process per engine (and per mode). Diagnostic only: written to <out>.sets.json, never
into the eligibility file (sets chosen for patches are not pinned-commit facts).

Never reads answers, patches or mutant labels. Exits 0 with whatever was certified within
--budget wall seconds (tests not reached are not eligible); non-zero only on usage errors.
Prints a build-time recommendation (CPU of an iso-only fs certification of the tests a Mode 0
set can contain) at the end.
"""

import argparse
import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from selector import mode0_runner as m0  # noqa: E402
from selector import overlay as ovl  # noqa: E402

SCHEMA = "br-mode0-eligibility/1"
STARTUP_CAP = 60.0
MODES = ("memory", "fs")
# Outcomes the tool records for runs it did not make; never compared between engines/modes.
SYNTHETIC = ("not_reached", "skipped_after_spawn", "skipped_after_failure", "overlay_error")
# Recorded seconds above which a test cannot be part of a Mode 0 set (PRD: <=1.5 s per set).
MODE0_SET_SECONDS = 1.5


def cap_for(seconds):
    """Per-test wall cap for certification runs: generous, scaled from recorded time."""
    return round(10.0 + 4.0 * seconds, 3)


def snapshot(src):
    out = {}
    for root, dirs, names in os.walk(src):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in names:
            path = os.path.join(root, name)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            out[os.path.relpath(path, src)] = (st.st_size, st.st_mtime_ns)
    return out


def interpreter_info(python):
    code = ("import json, platform, sys; print(json.dumps({'python': sys.version.split()[0], "
            "'implementation': sys.implementation.name, 'platform': platform.platform(), "
            "'machine': platform.machine(), 'executable': sys.executable}))")
    try:
        out = subprocess.run([python, "-I", "-S", "-c", code], capture_output=True, text=True, timeout=30)
        return json.loads(out.stdout)
    except Exception as error:
        return {"error": repr(error)}


def pct(values, q):
    if not values:
        return None
    values = sorted(values)
    k = min(len(values) - 1, max(0, int(round(q * (len(values) - 1)))))
    return round(values[k], 4)


def _one(values):
    """The single value every run reported (JSON-encoded set), else None."""
    if values and len(values) == 1:
        return json.loads(next(iter(values)))
    return None


def file_digest(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class Runner:
    def __init__(self, args, tests):
        self.args = args
        self.tests = tests
        self.deadline = time.monotonic() + args.budget
        self.lock = threading.Lock()
        self.child_cpu = 0.0
        self.jobs_run = 0
        self.jobs_skipped = 0
        self.pytest_version = None
        self.build_cpu, self.remove_cpu, self.overlay_errors = [], [], []
        self.runner_info = {}
        os.makedirs(args.workdir, exist_ok=True)
        os.makedirs(args.overlay_dir, exist_ok=True)

    def run(self, engine, items, hash_seed=None, mode="memory"):
        """items: [(id, seconds)] -> execute() result, None if the budget is spent, or
        {"overlay_error": text} if the fs overlay could not be built."""
        left = self.deadline - time.monotonic()
        if left <= 1.0:
            with self.lock:
                self.jobs_skipped += 1
            return None
        spec = {"kind": "mode0", "src": self.args.repo, "patched": {}, "engine": engine,
                "site": self.args.site, "startup_cap": STARTUP_CAP,
                "tests": [{"id": tid, "cap": cap_for(sec)} for tid, sec in items]}
        timeout = min(left, STARTUP_CAP + sum(cap_for(sec) for _, sec in items) + 10.0)
        root, cpu_count = None, None
        if mode == "fs":
            cpu_count = self.args.cpu_count or None
            t0 = time.thread_time()
            try:
                root = ovl.build(self.args.repo, {}, self.args.overlay_dir)
            except ovl.OverlayError as error:
                with self.lock:
                    self.overlay_errors.append(str(error))
                return {"overlay_error": str(error)}
            build = time.thread_time() - t0
            spec.update(ovl.spec_fields(root))
        try:
            res = m0.execute(spec, timeout, python=self.args.python, hash_seed=hash_seed,
                             workdir=self.args.workdir, cpu_count=cpu_count)
        finally:
            if root is not None:
                t0 = time.thread_time()
                ovl.remove(root)
                with self.lock:
                    self.build_cpu.append(build)
                    self.remove_cpu.append(time.thread_time() - t0)
        with self.lock:
            self.child_cpu += res["cpu"]
            self.jobs_run += 1
            if res["done"] and res["done"].get("pytest"):
                self.pytest_version = res["done"]["pytest"]
            for key in ("pad", "assert_watch"):
                if res["done"] and res["done"].get(key) is not None:
                    self.runner_info.setdefault(key, set()).add(json.dumps(res["done"][key]))
        return res


def one_line(res, tid):
    """(outcome as parse_results reads it, test cpu, process cpu, flags, detail, startup)."""
    if res is None:
        return {"o": "not_reached"}
    if "overlay_error" in res:
        return {"o": "overlay_error", "detail": res["overlay_error"][:200]}
    line = res["lines"].get(tid, {})
    done = res["done"] or {}
    out = {"o": res["outcomes"].get(tid, "missing"), "cpu": line.get("cpu"), "proc": round(res["cpu"], 4),
           "flags": line.get("flags", []), "rc": res["returncode"]}
    if line.get("outcome") and line["outcome"] != out["o"]:
        out["line"] = line["outcome"]
    if line.get("detail"):
        out["detail"] = line["detail"]
    if done.get("startup_cpu") is not None:
        out["startup"] = done["startup_cpu"]
    if done.get("aborted"):
        out["aborted"] = done["aborted"]
    if done.get("mp_children") is not None:
        out["children"] = done["mp_children"]
    return out


def verdict(lines_by_run, engine):
    """Per-engine verdict dict from {run name: line dict} (one mode)."""
    lines = lines_by_run
    outs = {name: ln["o"] for name, ln in lines.items()}
    flags = sorted({f for ln in lines.values() for f in ln.get("flags", [])})
    cpus = [ln["cpu"] for ln in lines.values() if ln.get("cpu") is not None]
    procs = [ln["proc"] for ln in lines.values() if ln.get("proc") is not None]
    info = {"o": outs, "cpu": max(cpus) if cpus else None, "proc": max(procs) if procs else None,
            "flags": flags}
    kids = [ln["children"] for ln in lines.values() if ln.get("children") is not None]
    if kids:
        info["children"] = max(kids)
    details = sorted({ln["detail"] for ln in lines.values() if ln.get("detail")})
    if details:
        info["detail"] = details[0]
    why = None
    first_bad = next(((name, o) for name, o in outs.items() if o != "passed"), None)
    if first_bad and first_bad[1] == "not_reached":
        why = "not reached (budget)"
    elif first_bad:
        o = first_bad[1]
        if o == "flagged":
            o += " " + ",".join(sorted(m0.UNSAFE_FLAGS.intersection(flags)))
        why = f"{o} ({first_bad[0]})"
    elif m0.UNSAFE_FLAGS.intersection(flags):
        why = "unsafe flags: " + ",".join(sorted(m0.UNSAFE_FLAGS.intersection(flags)))
    info["ok"] = why is None
    info["why"] = why
    return info


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--tests", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("-j", type=int, default=os.cpu_count() or 1)
    ap.add_argument("--engines", default="unittest,pytest")
    ap.add_argument("--seeds", default="0,1", help="comma-separated PYTHONHASHSEED values ('' = none)")
    ap.add_argument("--budget", type=float, default=180.0, help="wall seconds for the whole run")
    ap.add_argument("--site", action="append", default=[], help="site-packages dir (repeatable)")
    ap.add_argument("--also-batch", action="store_true", help="also run each file's tests in one process")
    ap.add_argument("--max-seconds", type=float, default=None,
                    help="do not attempt tests whose recorded seconds exceed this")
    ap.add_argument("--sets", default=None, help="JSON {name: [ids]}: CPU of each set in one process")
    ap.add_argument("--sets-repeat", type=int, default=3)
    ap.add_argument("--workdir", default=None, help="scratch dir (default <out>.d)")
    ap.add_argument("--overlay", default="memory", help="comma-separated modes: memory, fs")
    ap.add_argument("--cpu-count", type=int, default=2,
                    help="fs mode: launch with -X cpu_count=N (0 = no cap)")
    ap.add_argument("--overlay-dir", default=None, help="fs mode: where overlays are built (default workdir)")
    ap.add_argument("--trust-unittest-without-pytest", action="store_true",
                    help="debugging only: certify unittest even when the pytest engine did not run")
    args = ap.parse_args()

    started_wall, started_cpu = time.monotonic(), time.process_time()
    engines = [e for e in args.engines.split(",") if e]
    if not engines or any(e not in ("unittest", "pytest") for e in engines):
        ap.error("--engines must name unittest and/or pytest")
    modes = [m for m in args.overlay.split(",") if m]
    if not modes or any(m not in MODES for m in modes) or len(set(modes)) != len(modes):
        ap.error("--overlay must name memory and/or fs")
    try:
        seeds = [int(s) for s in args.seeds.split(",") if s.strip()]
    except ValueError:
        ap.error("--seeds must be integers")
    if args.j < 1 or args.budget <= 0 or args.cpu_count < 0:
        ap.error("-j, --budget and --cpu-count must be positive")
    args.repo = os.path.realpath(args.repo)
    args.site = [os.path.realpath(d) for d in args.site]
    args.workdir = os.path.abspath(args.workdir or args.out + ".d")
    args.overlay_dir = os.path.abspath(args.overlay_dir or args.workdir)
    if not os.path.isdir(os.path.join(args.repo, "tests")):
        ap.error("--repo has no tests/ directory")
    for d in (args.workdir, args.overlay_dir):
        if os.path.realpath(d) == args.repo or os.path.realpath(d).startswith(args.repo + os.sep):
            ap.error(f"{d} is inside --repo")
    try:
        tests = json.load(open(args.tests))["tests"]
        sets = json.load(open(args.sets)) if args.sets else {}
    except Exception as error:
        ap.error(f"cannot read input: {error!r}")

    runs = [("iso", None)] + [(f"seed{s}", s) for s in seeds]
    by_id = {t["id"]: t for t in tests}
    before = snapshot(args.repo)
    runner = Runner(args, tests)

    attempt = [t for t in tests if args.max_seconds is None or t["seconds"] <= args.max_seconds]
    attempt.sort(key=lambda t: (t["seconds"], t["number"]))
    attempt_ids = {t["id"] for t in attempt}
    # One job per test and mode: the production run of each engine first, then the seeded runs
    # of each engine that still passes. A run that does not pass (or flags) ends that engine's
    # chain; an unsafe "spawn" flag also skips the other engine (never eligible in that mode).
    jobs = [("iso", t["id"], mode) for mode in modes for t in attempt]
    results = {}  # (id, engine, run, mode) -> line dict
    batch = {}    # (id, engine, mode) -> line dict

    def do(job):
        kind = job[0]
        if kind == "iso":
            _, tid, mode = job
            item = [(tid, by_id[tid]["seconds"])]
            alive = {}
            for engine in engines:
                name, seed = runs[0]
                if any("spawn" in results.get((tid, e, name, mode), {}).get("flags", []) for e in engines):
                    results[(tid, engine, name, mode)] = {"o": "skipped_after_spawn"}
                    alive[engine] = False
                    continue
                ln = results[(tid, engine, name, mode)] = one_line(
                    runner.run(engine, item, hash_seed=seed, mode=mode), tid)
                alive[engine] = ln["o"] == "passed"
            for name, seed in runs[1:]:
                for engine in engines:
                    if not alive[engine]:
                        results[(tid, engine, name, mode)] = {"o": "skipped_after_failure"}
                        continue
                    ln = results[(tid, engine, name, mode)] = one_line(
                        runner.run(engine, item, hash_seed=seed, mode=mode), tid)
                    alive[engine] = ln["o"] == "passed"
        else:
            _, file, engine, mode = job
            items = [(t["id"], t["seconds"]) for t in attempt_by_file[file]]
            res = runner.run(engine, items, mode=mode)
            for tid, _ in items:
                batch[(tid, engine, mode)] = one_line(res, tid)

    attempt_by_file = {}
    for t in tests:
        if t["id"] in attempt_ids:
            attempt_by_file.setdefault(t["file"], []).append(t)
    also_batch = args.also_batch and "memory" in modes
    batch_modes = modes if args.also_batch else []
    by_cost = sorted(attempt_by_file, key=lambda f: -sum(t["seconds"] for t in attempt_by_file[f]))
    jobs += [("batch", f, e, m) for m in batch_modes for f in by_cost for e in engines]

    with ThreadPoolExecutor(args.j) as pool:
        list(pool.map(do, jobs))

    set_cpu = {}
    for name, ids in sets.items():
        ids = [i for i in ids if i in by_id]
        for mode in modes:
            for engine in engines:
                samples = []
                for _ in range(max(1, args.sets_repeat)):
                    res = runner.run(engine, [(i, by_id[i]["seconds"]) for i in ids], mode=mode)
                    if res is None or "overlay_error" in res:
                        break
                    done = res["done"] or {}
                    samples.append({"proc": round(res["cpu"], 4), "startup": done.get("startup_cpu"),
                                    "wall": round(res["wall"], 4), "children": done.get("mp_children"),
                                    "passed": sum(1 for o in res["outcomes"].values() if o == "passed"),
                                    "outcomes": sorted(set(res["outcomes"].values()))})
                set_cpu.setdefault(name, {"n": len(ids), "recorded": round(sum(by_id[i]["seconds"] for i in ids), 3)})
                key = engine if mode == "memory" else f"{engine}/fs"
                set_cpu[name][key] = {"proc_median": pct([s["proc"] for s in samples], 0.5), "samples": samples}

    after = snapshot(args.repo)
    src_writes = sorted(set(after) - set(before)) + sorted(p for p in before if p in after and after[p] != before[p])
    src_removed = sorted(set(before) - set(after))

    # ------------------------------------------------------------------ verdicts
    entries = {}
    eligible = {mode: {e: [] for e in engines} for mode in MODES}
    disagreements, order_sensitive, mode_differences = [], [], []
    startup = {e: [] for e in engines}
    for t in tests:
        tid = t["id"]
        entry = {"n": t["number"], "s": t["seconds"]}
        if tid not in attempt_ids:
            for engine in engines:
                entry[engine] = {"ok": False, "why": "not attempted (--max-seconds)"}
            if "fs" in modes:
                entry["fs"] = {e: {"ok": False, "why": "not attempted (--max-seconds)"} for e in engines}
            entries[tid] = entry
            continue
        iso = {}
        for mode in MODES:
            if mode not in modes:
                if mode == "memory":
                    for engine in engines:
                        entry[engine] = {"ok": False, "why": "not attempted (--overlay)"}
                continue
            target = entry if mode == "memory" else entry.setdefault("fs", {})
            for engine in engines:
                lines = {name: results.get((tid, engine, name, mode), {"o": "not_reached"}) for name, _ in runs}
                if mode == "memory":
                    for ln in lines.values():
                        if ln.get("proc") is not None and ln.get("startup") is not None and ln.get("cpu") is not None:
                            startup[engine].append(ln["proc"] - ln["cpu"])
                target[engine] = verdict(lines, engine)
                iso[(mode, engine)] = lines["iso"].get("line") or lines["iso"]["o"]
            if len(engines) == 2 and all(iso.get((mode, e)) not in (None, *SYNTHETIC) for e in engines) \
                    and iso[(mode, "unittest")] != iso[(mode, "pytest")]:
                disagreements.append({"id": tid, "mode": mode, "unittest": iso[(mode, "unittest")],
                                      "pytest": iso[(mode, "pytest")]})
                if mode == "memory":
                    entry["disagree"] = True
                else:
                    target["disagree"] = True
            if "unittest" in target and target["unittest"]["ok"]:
                if "pytest" not in target:
                    if not args.trust_unittest_without_pytest:
                        target["unittest"]["ok"] = False
                        target["unittest"]["why"] = "pytest engine not run (no agreement)"
                elif not target["pytest"]["ok"]:
                    target["unittest"]["ok"] = False
                    target["unittest"]["why"] = "pytest engine does not pass it"
        if len(modes) == 2:
            for engine in engines:
                a, b = iso.get(("memory", engine)), iso.get(("fs", engine))
                if a not in (None, *SYNTHETIC) and b not in (None, *SYNTHETIC) and a != b:
                    mode_differences.append({"id": tid, "engine": engine, "memory": a, "fs": b})
                ok_m, ok_f = entry[engine]["ok"], entry["fs"][engine]["ok"]
                if ok_m != ok_f:
                    mode_differences.append({"id": tid, "engine": engine, "memory_ok": ok_m, "fs_ok": ok_f,
                                             "memory_why": entry[engine]["why"], "fs_why": entry["fs"][engine]["why"]})
        for mode in batch_modes:
            target = entry if mode == "memory" else entry["fs"]
            b, bcpu = {}, {}
            for engine in engines:
                ln = batch.get((tid, engine, mode), {"o": "not_reached"})
                b[engine] = ln.get("line") or ln["o"]
                if ln.get("cpu") is not None:
                    bcpu[engine] = ln["cpu"]
                alone = target[engine]["o"]["iso"]
                if alone in SYNTHETIC:
                    continue
                if b[engine] in ("notrun", "timeout", "missing", "not_reached", "unproven"):
                    if target[engine]["ok"]:
                        target[engine]["ok"] = False
                        target[engine]["why"] = f"batch inconclusive ({b[engine]})"
                elif b[engine] != (results.get((tid, engine, "iso", mode), {}).get("line") or alone):
                    if tid not in [x["id"] for x in order_sensitive]:
                        order_sensitive.append({"id": tid, "engine": engine, "mode": mode,
                                                "alone": alone, "batch": b[engine]})
                    entry["order_sensitive"] = True
            target["batch"] = b
            target["bcpu"] = bcpu
            if entry.get("order_sensitive"):
                for target in [entry] + ([entry["fs"]] if "fs" in entry else []):
                    for engine in engines:
                        if target[engine]["ok"]:
                            target[engine]["ok"] = False
                            target[engine]["why"] = "order_sensitive"
        for engine in engines:
            if entry[engine]["ok"]:
                eligible["memory"][engine].append(t["number"])
            if "fs" in entry and entry["fs"][engine]["ok"]:
                eligible["fs"][engine].append(t["number"])
        entries[tid] = entry

    def reasons(get):
        out = {}
        for tid, entry in entries.items():
            for engine in engines:
                verdict_ = get(entry)
                if verdict_ is not None and not verdict_[engine]["ok"]:
                    key = verdict_[engine]["why"].split(" (")[0]
                    out.setdefault(key, {}).setdefault(engine, 0)
                    out[key][engine] += 1
        return out

    ineligible = reasons(lambda e: e if "memory" in modes else None)
    ineligible_fs = reasons(lambda e: e.get("fs"))

    info = interpreter_info(args.python)
    not_reached = sum(1 for ln in list(results.values()) + list(batch.values()) if ln.get("o") == "not_reached")
    header = {
        "python": info.get("python"), "implementation": info.get("implementation"),
        "platform": info.get("platform"), "machine": info.get("machine"),
        "engines": {e: {"python": info.get("python"), **({"pytest": runner.pytest_version} if e == "pytest" else {})}
                    for e in engines},
        "source_digest": m0.source_digest(args.repo),
        "tests_identity": m0.tests_identity(tests),
        "runner_digest": m0.runner_digest(),
        "runs": [{"name": name, "argv": list(m0.ISO_FLAGS if seed is None else m0.SEEDED_FLAGS),
                  "hash_seed": "random" if seed is None else seed,
                  "env": "inherited (PYTHON* ignored by -I)" if seed is None
                  else f"inherited minus PYTHON*, PYTHONHASHSEED={seed}"} for name, seed in runs],
        "seeds": seeds, "site": args.site, "caps": "10 + 4 * recorded seconds", "startup_cap": STARTUP_CAP,
        "also_batch": also_batch, "max_seconds": args.max_seconds, "budget": args.budget, "j": args.j,
        "overlays": modes,
        "counts": {"tests": len(tests), "attempted": len(attempt),
                   "eligible": {e: len(v) for e, v in eligible["memory"].items()},
                   "jobs_run": runner.jobs_run, "jobs_skipped": runner.jobs_skipped},
        "cpu": {"children": round(runner.child_cpu, 3), "tool": round(time.process_time() - started_cpu, 3)},
        "wall": round(time.monotonic() - started_wall, 3),
        "startup_cpu": {e: {"median": pct(v, 0.5), "p90": pct(v, 0.9), "n": len(v)} for e, v in startup.items()},
        "src_writes": src_writes[:200], "src_removed": src_removed[:200],
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "complete": runner.jobs_skipped == 0 and not_reached == 0 and not runner.overlay_errors,
        "engines_compared": set(engines) == {"unittest", "pytest"},
        "trust_unittest_without_pytest": bool(args.trust_unittest_without_pytest),
        "source_digest_scope": "tree",
        "env_digest": m0.env_digest(args.site, version=info.get("python"),
                                    implementation=info.get("implementation"),
                                    machine=info.get("machine")),
        "pad": _one(runner.runner_info.get("pad")),
        "assert_watch": _one(runner.runner_info.get("assert_watch")),
        "batch_modes": batch_modes,
    }
    if "fs" in modes:
        header["counts"]["eligible_fs"] = {e: len(v) for e, v in eligible["fs"].items()}
        header["fs"] = {
            "cpu_count": args.cpu_count or None,
            "argv_extra": ["-X", f"cpu_count={args.cpu_count}"] if args.cpu_count else [],
            "overlay_digest": file_digest(os.path.join(os.path.dirname(HERE), "selector", "overlay.py")),
            "mirror": list(ovl.MIRROR_DIRS),
            "build_cpu": {"median": pct(runner.build_cpu, 0.5), "p90": pct(runner.build_cpu, 0.9),
                          "n": len(runner.build_cpu)},
            "remove_cpu": {"median": pct(runner.remove_cpu, 0.5), "p90": pct(runner.remove_cpu, 0.9),
                           "n": len(runner.remove_cpu)},
            "overlay_errors": sorted(set(runner.overlay_errors))[:20],
        }
    summary = {"eligible": {e: sorted(v) for e, v in eligible["memory"].items()},
               "ineligible_reasons": ineligible, "disagreements": disagreements,
               "order_sensitive": order_sensitive}
    if "fs" in modes:
        summary["eligible_fs"] = {e: sorted(v) for e, v in eligible["fs"].items()}
        summary["ineligible_reasons_fs"] = ineligible_fs
        summary["mode_differences"] = mode_differences
    if args.sets:
        with open(args.out + ".sets.json", "w") as fh:
            json.dump(set_cpu, fh, indent=1)

    tmp = args.out + ".tmp"
    with open(tmp, "w") as fh:
        fh.write('{"schema": %s,\n"header": %s,\n"summary": %s,\n"tests": {\n' % (
            json.dumps(SCHEMA), json.dumps(header, indent=1), json.dumps(summary, separators=(",", ":"))))
        items = list(entries.items())
        for k, (tid, entry) in enumerate(items):
            fh.write(json.dumps(tid) + ": " + json.dumps(entry, separators=(",", ":"))
                     + ("," if k + 1 < len(items) else "") + "\n")
        fh.write("}}\n")
    os.replace(tmp, args.out)
    for d in (args.overlay_dir, args.workdir):
        try:
            os.rmdir(d)
        except OSError:
            pass
    print(f"complete {header['complete']}, engines compared {header['engines_compared']}; "
          f"eligible: memory {header['counts']['eligible'] if 'memory' in modes else '-'}, "
          f"fs {header['counts'].get('eligible_fs', '-')} of {len(tests)}; disagreements {len(disagreements)}; "
          f"mode differences {len(mode_differences)}; order-sensitive {len(order_sensitive)}; "
          f"wall {header['wall']}s; child cpu {header['cpu']['children']}s; src writes {len(src_writes)}",
          file=sys.stderr)
    recommend(entries, engines, modes, args, file=sys.stderr)


def recommend(entries, engines, modes, args, file):
    """Build-time recommendation: what an iso-only certification of the tests that can ever be
    in a Mode 0 set (recorded seconds <= MODE0_SET_SECONDS) costs, per mode, from this run."""
    for mode in modes:
        engine = "unittest" if "unittest" in engines else engines[0]
        small, large = [], []
        for entry in entries.values():
            verdict_ = entry.get(engine) if mode == "memory" else entry.get("fs", {}).get(engine)
            if not verdict_ or verdict_.get("proc") is None:
                continue
            iso_proc = verdict_["proc"]
            (small if entry["s"] <= MODE0_SET_SECONDS else large).append(iso_proc)
        if not small and not large:
            continue
        print(f"build-time recommendation ({mode}, {engine}): tests with recorded seconds <= "
              f"{MODE0_SET_SECONDS} ({len(small)}) cost ~{sum(small):.0f} CPU-s per run here (max over runs); "
              f"the {len(large)} longer ones ~{sum(large):.0f} CPU-s and can never be in a Mode 0 set. "
              f"Suggest: --overlay {mode} --engines {engine} --seeds '' --max-seconds {MODE0_SET_SECONDS}"
              + (f" --cpu-count {args.cpu_count}" if mode == "fs" else "")
              + f" -j <cpus> (~{sum(small) / 2:.0f} s wall on 2 CPUs).", file=file)


if __name__ == "__main__":
    main()
