#!/usr/bin/env python3
"""Certify which numbered tests may be verified directly by Mode 0 (pinned-commit facts only).

    python3 tools/mode0_eligibility.py --repo SRC --tests tests.json --out mode0.json \
        [--python PY] [-j N] [--engines unittest,pytest] [--seeds 0,1] [--budget SECONDS] \
        [--site DIR ...] [--also-batch] [--max-seconds S] [--sets sets.json]

Every test runs alone in a fresh Mode 0 runner process on the UNPATCHED source (patched = {}),
launched exactly as the selector launches it (selector.mode0_runner.execute):

    run "iso"     PY -I -S -B selector/replica_runner.py spec.json     (random hash seed: -I
                  ignores PYTHONHASHSEED; this is the production launch, m0.ISO_FLAGS)
    run "seedN"   PY -s -P -S -B selector/replica_runner.py spec.json  with every PYTHON* variable
                  removed from the environment and PYTHONHASHSEED=N (-s -P are -I minus -E)

A test is eligible for an engine when every run of that engine reached it and reported
"passed" with no unsafe flag (spawn, meta_path, cwd, read_patched); for unittest, also when
pytest was run and agrees; with --also-batch, also when running all catalog tests of its file
in one process (per engine, catalog order) gives the same outcome as running it alone
(otherwise it is order_sensitive and ineligible for every engine).

--sets FILE ({name: [test ids]}) additionally measures the CPU of running each set in ONE
runner process per engine. Diagnostic only: written to <out>.sets.json, never into the
eligibility file (sets chosen for patches are not pinned-commit facts).

Never reads answers, patches or mutant labels. Exits 0 with whatever was certified within
--budget wall seconds (tests not reached are not eligible); non-zero only on usage errors.
"""

import argparse
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

SCHEMA = "br-mode0-eligibility/1"
STARTUP_CAP = 60.0
# Outcomes the tool records for runs it did not make; never compared between engines/modes.
SYNTHETIC = ("not_reached", "skipped_after_spawn", "skipped_after_failure")


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
        os.makedirs(args.workdir, exist_ok=True)

    def run(self, engine, items, hash_seed=None):
        """items: [(id, seconds)] -> execute() result, or None if the budget is spent."""
        left = self.deadline - time.monotonic()
        if left <= 1.0:
            with self.lock:
                self.jobs_skipped += 1
            return None
        spec = {"kind": "mode0", "src": self.args.repo, "patched": {}, "engine": engine,
                "site": self.args.site, "startup_cap": STARTUP_CAP,
                "tests": [{"id": tid, "cap": cap_for(sec)} for tid, sec in items]}
        timeout = min(left, STARTUP_CAP + sum(cap_for(sec) for _, sec in items) + 10.0)
        res = m0.execute(spec, timeout, python=self.args.python, hash_seed=hash_seed,
                         workdir=self.args.workdir)
        with self.lock:
            self.child_cpu += res["cpu"]
            self.jobs_run += 1
            if res["done"] and res["done"].get("pytest"):
                self.pytest_version = res["done"]["pytest"]
        return res


def one_line(res, tid):
    """(outcome as parse_results reads it, test cpu, process cpu, flags, detail, startup)."""
    if res is None:
        return {"o": "not_reached"}
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
    return out


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
    args = ap.parse_args()

    started_wall, started_cpu = time.monotonic(), time.process_time()
    engines = [e for e in args.engines.split(",") if e]
    if not engines or any(e not in ("unittest", "pytest") for e in engines):
        ap.error("--engines must name unittest and/or pytest")
    try:
        seeds = [int(s) for s in args.seeds.split(",") if s.strip()]
    except ValueError:
        ap.error("--seeds must be integers")
    if args.j < 1 or args.budget <= 0:
        ap.error("-j and --budget must be positive")
    args.repo = os.path.realpath(args.repo)
    args.site = [os.path.realpath(d) for d in args.site]
    args.workdir = os.path.abspath(args.workdir or args.out + ".d")
    if not os.path.isdir(os.path.join(args.repo, "tests")):
        ap.error("--repo has no tests/ directory")
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
    # One job per test: the production run of each engine first, then the seeded runs of each
    # engine that still passes. A run that does not pass (or flags) ends that engine's chain;
    # a spawn flag also skips the other engine (spawned pools are expensive and never eligible).
    jobs = [("iso", t["id"]) for t in attempt]
    results = {}  # (id, engine, run) -> line dict
    batch = {}    # (id, engine) -> line dict

    def do(job):
        kind = job[0]
        if kind == "iso":
            tid = job[1]
            item = [(tid, by_id[tid]["seconds"])]
            alive = {}
            for engine in engines:
                name, seed = runs[0]
                if any("spawn" in results.get((tid, e, name), {}).get("flags", []) for e in engines):
                    results[(tid, engine, name)] = {"o": "skipped_after_spawn"}
                    alive[engine] = False
                    continue
                ln = results[(tid, engine, name)] = one_line(runner.run(engine, item, hash_seed=seed), tid)
                alive[engine] = ln["o"] == "passed"
            for name, seed in runs[1:]:
                for engine in engines:
                    if not alive[engine]:
                        results[(tid, engine, name)] = {"o": "skipped_after_failure"}
                        continue
                    ln = results[(tid, engine, name)] = one_line(runner.run(engine, item, hash_seed=seed), tid)
                    alive[engine] = ln["o"] == "passed"
        else:
            _, file, engine = job
            items = [(t["id"], t["seconds"]) for t in attempt_by_file[file]]
            res = runner.run(engine, items)
            for tid, _ in items:
                batch[(tid, engine)] = one_line(res, tid)

    attempt_by_file = {}
    for t in tests:
        if t["id"] in attempt_ids:
            attempt_by_file.setdefault(t["file"], []).append(t)
    if args.also_batch:
        jobs += [("batch", f, e) for f in sorted(attempt_by_file, key=lambda f: -sum(
            t["seconds"] for t in attempt_by_file[f])) for e in engines]

    with ThreadPoolExecutor(args.j) as pool:
        list(pool.map(do, jobs))

    set_cpu = {}
    for name, ids in sets.items():
        ids = [i for i in ids if i in by_id]
        for engine in engines:
            samples = []
            for _ in range(max(1, args.sets_repeat)):
                res = runner.run(engine, [(i, by_id[i]["seconds"]) for i in ids])
                if res is None:
                    break
                done = res["done"] or {}
                samples.append({"proc": round(res["cpu"], 4), "startup": done.get("startup_cpu"),
                                "wall": round(res["wall"], 4),
                                "passed": sum(1 for o in res["outcomes"].values() if o == "passed"),
                                "outcomes": sorted(set(res["outcomes"].values()))})
            set_cpu.setdefault(name, {"n": len(ids), "recorded": round(sum(by_id[i]["seconds"] for i in ids), 3)})
            set_cpu[name][engine] = {
                "proc_median": pct([s["proc"] for s in samples], 0.5), "samples": samples}

    after = snapshot(args.repo)
    src_writes = sorted(set(after) - set(before)) + sorted(p for p in before if p in after and after[p] != before[p])
    src_removed = sorted(set(before) - set(after))

    # ------------------------------------------------------------------ verdicts
    entries = {}
    eligible = {e: [] for e in engines}
    disagreements, order_sensitive = [], []
    startup = {e: [] for e in engines}
    for t in tests:
        tid = t["id"]
        entry = {"n": t["number"], "s": t["seconds"]}
        if tid not in attempt_ids:
            for engine in engines:
                entry[engine] = {"ok": False, "why": "not attempted (--max-seconds)"}
            entries[tid] = entry
            continue
        iso = {}
        for engine in engines:
            lines = {name: results.get((tid, engine, name), {"o": "not_reached"}) for name, _ in runs}
            outs = {name: ln["o"] for name, ln in lines.items()}
            flags = sorted({f for ln in lines.values() for f in ln.get("flags", [])})
            cpus = [ln["cpu"] for ln in lines.values() if ln.get("cpu") is not None]
            procs = [ln["proc"] for ln in lines.values() if ln.get("proc") is not None]
            for ln in lines.values():
                if ln.get("proc") is not None and ln.get("startup") is not None and ln.get("cpu") is not None:
                    startup[engine].append(ln["proc"] - ln["cpu"])
            info = {"o": outs, "cpu": max(cpus) if cpus else None, "proc": max(procs) if procs else None,
                    "flags": flags}
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
            entry[engine] = info
            iso[engine] = lines["iso"].get("line") or lines["iso"]["o"]
        if len(engines) == 2 and all(iso.get(e) not in (None, *SYNTHETIC) for e in engines) \
                and iso["unittest"] != iso["pytest"]:
            disagreements.append({"id": tid, "unittest": iso["unittest"], "pytest": iso["pytest"]})
            entry["disagree"] = True
        if "pytest" in entry and "unittest" in entry and entry["unittest"]["ok"] and not entry["pytest"]["ok"]:
            entry["unittest"]["ok"] = False
            entry["unittest"]["why"] = "pytest engine does not pass it"
        if args.also_batch:
            b = {}
            for engine in engines:
                ln = batch.get((tid, engine), {"o": "not_reached"})
                b[engine] = ln.get("line") or ln["o"]
                alone = entry[engine]["o"]["iso"]
                if alone in SYNTHETIC:
                    continue
                if b[engine] in ("notrun", "timeout", "missing", "not_reached", "unproven"):
                    if entry[engine]["ok"]:
                        entry[engine]["ok"] = False
                        entry[engine]["why"] = f"batch inconclusive ({b[engine]})"
                elif b[engine] != (results.get((tid, engine, "iso"), {}).get("line") or alone):
                    if tid not in [x["id"] for x in order_sensitive]:
                        order_sensitive.append({"id": tid, "engine": engine, "alone": alone, "batch": b[engine]})
                    entry["order_sensitive"] = True
            entry["batch"] = b
            if entry.get("order_sensitive"):
                for engine in engines:
                    if entry[engine]["ok"]:
                        entry[engine]["ok"] = False
                        entry[engine]["why"] = "order_sensitive"
        for engine in engines:
            if entry[engine]["ok"]:
                eligible[engine].append(t["number"])
        entries[tid] = entry

    ineligible = {}
    for tid, entry in entries.items():
        for engine in engines:
            if not entry[engine]["ok"]:
                key = entry[engine]["why"].split(" (")[0]
                ineligible.setdefault(key, {}).setdefault(engine, 0)
                ineligible[key][engine] += 1

    info = interpreter_info(args.python)
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
        "also_batch": args.also_batch, "max_seconds": args.max_seconds, "budget": args.budget, "j": args.j,
        "counts": {"tests": len(tests), "attempted": len(attempt),
                   "eligible": {e: len(v) for e, v in eligible.items()},
                   "jobs_run": runner.jobs_run, "jobs_skipped": runner.jobs_skipped},
        "cpu": {"children": round(runner.child_cpu, 3), "tool": round(time.process_time() - started_cpu, 3)},
        "wall": round(time.monotonic() - started_wall, 3),
        "startup_cpu": {e: {"median": pct(v, 0.5), "p90": pct(v, 0.9), "n": len(v)} for e, v in startup.items()},
        "src_writes": src_writes[:200], "src_removed": src_removed[:200],
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    summary = {"eligible": {e: sorted(v) for e, v in eligible.items()},
               "ineligible_reasons": ineligible, "disagreements": disagreements,
               "order_sensitive": order_sensitive}
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
    try:
        os.rmdir(args.workdir)
    except OSError:
        pass
    print(f"eligible: {header['counts']['eligible']} of {len(tests)}; disagreements {len(disagreements)}; "
          f"order-sensitive {len(order_sensitive)}; wall {header['wall']}s; child cpu {header['cpu']['children']}s; "
          f"src writes {len(src_writes)}", file=sys.stderr)


if __name__ == "__main__":
    main()
