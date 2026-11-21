#!/usr/bin/env python3
"""Benchmark the selector on simulated cost + CPU of every descendant, per config.

    python3 tools/benchmark.py --patches sample/commits --tests sample/tests.json \\
        --answers sample/answers.json --src /path/to/sqlglot \\
        [--maps maps/maps.bin] [--python PY] [--runs 3] \\
        [--config base:BR_ASSUME_TRUSTED=1 --config mode0:BR_ASSUME_TRUSTED=1,BR_MODE0=1] \\
        [--out result.json] [--keep-output DIR] [--timeout 600]

Each ``--config NAME:K=V,K=V`` is a named set of extra environment variables for the
selector (values cannot contain commas; ``NAME:`` means no extra variables). Without
``--config`` one config named ``default`` with no extra variables runs. Every config
runs ``--runs`` times on the same workload, interleaved (run 1 of every config, then
run 2, ...), so drift on the machine affects all configs alike.

Each run is executed like the submission image: ``PY -I -S run.py`` from the repo root
with BR_DATA (a fresh data dir: tests.json + commits/*.patch), BR_MAPS and BR_SRC set,
plus the config's variables. ``-I`` makes Python ignore PYTHON* variables, as in
production; PYTHONDONTWRITEBYTECODE=1 is passed only because the image sets it (it has
no effect under ``-I``).

The score of a run is ``simulated + CPU``. Simulated cost is computed here in integer
milliseconds per SCORING.md and cross-checked against ``score.py`` (the authority) on
every output: any rejection or disagreement aborts with exit status 2. The gate metric
is the excess over the zero-miss floor per commit: (median total - floor) / commits.

CPU measurement (see measure_process): user+sys of the selector and every descendant it
reaped, from the rusage ``os.wait4`` returns. Limitations, also written to the JSON:
  * A descendant that is never waited for by its parent (an orphaned grandchild, or a
    child still running when its parent exits) is not counted. The selector runs in its
    own session, and after it exits any surviving member of its process group is
    reported (``leftover_processes``) and killed; a descendant that called setsid()
    escapes even that check.
  * On Linux with cgroup v2, ``usage_usec`` in the cgroup's ``cpu.stat`` counts every
    process in the cgroup, reaped or not. Run the harness inside an isolated cgroup (for
    example ``docker run`` or ``systemd-run --scope``) and the recorded
    ``cgroup_usage_usec_delta`` is a complete figure, but it then also includes this
    harness's own (small) CPU between the two reads. It is recorded when readable and is
    never required.
  * ru_maxrss is the peak RSS of the largest single process among the selector and its
    reaped descendants, not a sum. Its unit is bytes on macOS and KiB on Linux; both the
    raw value and a KiB conversion are reported.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import resource
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

WORKER_MS = 2_000
MISS_MS = 300_000
MAX_WORKERS = 8

CPU_NOTES = [
    "CPU = user+sys of the selector process and every descendant it waited for "
    "(rusage returned by os.wait4, which includes waited-for descendants recursively).",
    "Descendants that are never reaped by their parent (orphaned grandchildren) are not "
    "counted; surviving members of the selector's process group are reported as "
    "leftover_processes and killed, a descendant that called setsid() escapes that check.",
    "On Linux with cgroup v2, cpu.stat usage_usec of an isolated cgroup (docker run, "
    "systemd-run --scope) counts every process including unreaped ones; when readable, "
    "its delta around the run is recorded as cgroup_usage_usec_delta (it then includes "
    "this harness's own CPU in that window). It is informational only.",
    "max_rss: ru_maxrss of the largest single process, bytes on macOS, KiB on Linux; "
    "max_rss_kib is normalised.",
]


# ---------------------------------------------------------------- scoring arithmetic


def duration_ms(seconds: float) -> int:
    """A test's duration in ms, rounding halves up exactly like score.py."""
    return math.floor(seconds * 1000 + 0.5)


def cost_ms(workers, ms) -> int:
    """Busiest worker + 2000 ms per nonempty worker (0 with no nonempty worker)."""
    occupied = [w for w in workers if w]
    if not occupied:
        return 0
    return max(sum(ms[t] for t in w) for w in occupied) + WORKER_MS * len(occupied)


def floor_ms(failing, ms) -> int:
    """Zero-miss lower bound of a commit whose failing tests are ``failing``:
    min over k = 1..min(8, |F|) of max(longest, ceil(total / k)) + 2000 k."""
    failing = set(failing)
    if not failing:
        return 0
    durations = [ms[t] for t in failing]
    total, longest = sum(durations), max(durations)
    return min(
        max(longest, -(-total // k)) + WORKER_MS * k
        for k in range(1, min(MAX_WORKERS, len(failing)) + 1)
    )


def split_cost_ms(failing, ms) -> int:
    """What the selector's own scheduler achieves on exactly the failing tests."""
    if not failing:
        return 0
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from selector import schedule

    return cost_ms(schedule.split(sorted(failing), ms), ms)


def score_commit(workers, failing, ms) -> dict:
    selected = {t for w in workers for t in w}
    missed = sorted(set(failing) - selected)
    cost = cost_ms(workers, ms)
    return {
        "selected": selected,
        "workers": sum(1 for w in workers if w),
        "cost_ms": cost,
        "missed": missed,
        "charged_ms": cost + (MISS_MS if missed else 0),
    }


# ---------------------------------------------------------------- inputs


def parse_config(spec: str) -> tuple[str, dict]:
    name, sep, rest = spec.partition(":")
    if not name or not sep and "=" in name:
        raise argparse.ArgumentTypeError(f"--config must be NAME:K=V,K=V, got {spec!r}")
    env = {}
    for item in filter(None, rest.split(",")):
        key, eq, value = item.partition("=")
        if not eq or not key:
            raise argparse.ArgumentTypeError(f"bad K=V {item!r} in --config {spec!r}")
        env[key] = value
    return name, env


def load_answers(path) -> dict:
    """cid -> {failing (set, without unknown), unknown (set), kind}.

    Tests listed as "unknown" could not be judged (tools/ground_truth.py). Like
    tools/evaluate.py they are excluded from the failing set: neither scored as missed
    when left out nor counted in the floor."""
    raw = json.loads(Path(path).read_text("utf-8"))["commits"]
    out = {}
    for cid, a in raw.items():
        unknown = set(a.get("unknown", []))
        out[cid] = {
            "failing": set(a["failing"]) - unknown,
            "unknown": unknown,
            "kind": a.get("kind", "?"),
        }
    return out


def answers_for_score_py(answers: dict) -> dict:
    """score.py has no notion of "unknown": it would count a left-out unknown test as a
    miss. It is therefore given a filtered answers file whose failing lists already
    exclude unknown tests, so its verdict uses exactly the failing sets used here."""
    return {
        "commits": {
            cid: {"failing": sorted(a["failing"]), "kind": a["kind"]}
            for cid, a in sorted(answers.items())
        }
    }


# ---------------------------------------------------------------- measuring a process


def _maxrss_kib(raw: int) -> int:
    return raw // 1024 if sys.platform == "darwin" else raw


def cgroup_usage_usec() -> int | None:
    """usage_usec of this process's cgroup v2 (Linux only), or None if unreadable."""
    try:
        line = next(
            l for l in Path("/proc/self/cgroup").read_text().splitlines() if l.startswith("0::")
        )
        stat = Path("/sys/fs/cgroup") / line[3:].lstrip("/") / "cpu.stat"
        for row in stat.read_text().splitlines():
            key, _, value = row.partition(" ")
            if key == "usage_usec":
                return int(value)
    except (OSError, StopIteration, ValueError):
        return None
    return None


def measure_process(argv, env, cwd, stdout_path, stderr_path, timeout=None) -> dict:
    """Run argv to completion and measure it with all of its reaped descendants.

    The child gets its own session, so its process group can be checked for (and
    cleaned of) survivors afterwards. os.wait4 returns the child's rusage, which the
    kernel builds as the child's own usage plus that of every descendant waited for
    below it (each exiting process folds its reaped children into its own totals).
    RUSAGE_CHILDREN of this process is sampled around the run as a second figure; the
    harness has no other child in that window, so the two must agree."""
    before_children = resource.getrusage(resource.RUSAGE_CHILDREN)
    before_cgroup = cgroup_usage_usec()
    timed_out = threading.Event()
    with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
        t0 = time.monotonic()
        proc = subprocess.Popen(
            argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
            start_new_session=True,
        )

        def _kill():
            timed_out.set()
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass

        timer = threading.Timer(timeout, _kill) if timeout else None
        if timer:
            timer.start()
        try:
            _, status, usage = os.wait4(proc.pid, 0)
        finally:
            if timer:
                timer.cancel()
        wall = time.monotonic() - t0
    proc.returncode = os.waitstatus_to_exitcode(status)  # reaped here, not by Popen
    after_children = resource.getrusage(resource.RUSAGE_CHILDREN)
    after_cgroup = cgroup_usage_usec()

    leftover = False
    try:
        os.killpg(proc.pid, 0)
        leftover = True
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass

    user_us = round(usage.ru_utime * 1_000_000)
    sys_us = round(usage.ru_stime * 1_000_000)
    children_us = round(
        (after_children.ru_utime - before_children.ru_utime
         + after_children.ru_stime - before_children.ru_stime) * 1_000_000
    )
    return {
        "exit_code": proc.returncode,
        "timed_out": timed_out.is_set(),
        "wall_s": wall,
        "cpu_user_us": user_us,
        "cpu_sys_us": sys_us,
        "cpu_us": user_us + sys_us,
        "rusage_children_delta_us": children_us,
        "max_rss_raw": usage.ru_maxrss,
        "max_rss_unit": "bytes" if sys.platform == "darwin" else "KiB",
        "max_rss_kib": _maxrss_kib(usage.ru_maxrss),
        "leftover_processes": leftover,
        "cgroup_usage_usec_delta": (
            after_cgroup - before_cgroup
            if before_cgroup is not None and after_cgroup is not None else None
        ),
    }


# ---------------------------------------------------------------- score.py cross-check


class CrossCheckError(RuntimeError):
    pass


_SCORE_RE = {
    "simulated": re.compile(r"^Simulated: (\d+)\.(\d{3}) s$"),
    "misses": re.compile(r"^Misses:\s+(\d+) "),
    "missed": re.compile(r"^  missed (.+)$"),
    "cpu": re.compile(r"^CPU:\s+(\d+)\.(\d{6}) s$"),
    "total": re.compile(r"^Total:\s+(\d+)\.(\d{6}) s$"),
}


def parse_score_py(text: str) -> dict:
    """Parse score.py's report (exit status 0). Missing fields stay None."""
    got = {"simulated_ms": None, "misses": None, "missed_commits": [], "cpu_us": None, "total_us": None}
    for line in text.splitlines():
        if m := _SCORE_RE["simulated"].match(line):
            got["simulated_ms"] = int(m[1]) * 1000 + int(m[2])
        elif m := _SCORE_RE["misses"].match(line):
            got["misses"] = int(m[1])
        elif m := _SCORE_RE["missed"].match(line):
            got["missed_commits"].append(m[1])
        elif m := _SCORE_RE["cpu"].match(line):
            got["cpu_us"] = int(m[1]) * 1_000_000 + int(m[2])
        elif m := _SCORE_RE["total"].match(line):
            got["total_us"] = int(m[1]) * 1_000_000 + int(m[2])
    return got


def run_score_py(data_dir, answers_path, output_path, cpu_us, python=None) -> dict:
    """Run score.py on one saved output. Exit 0: report; 1: output rejected;
    2: answers and patches cover different commits. Anything but 0 is an error."""
    argv = [python or sys.executable, str(ROOT / "score.py"), str(data_dir), str(answers_path),
            str(output_path), "--cpu-seconds", f"{cpu_us // 1_000_000}.{cpu_us % 1_000_000:06d}"]
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        raise CrossCheckError(
            f"score.py rejected {output_path} (exit {proc.returncode}): "
            f"{(proc.stdout + proc.stderr).strip()}"
        )
    got = parse_score_py(proc.stdout)
    if None in (got["simulated_ms"], got["misses"], got["cpu_us"], got["total_us"]):
        raise CrossCheckError(f"could not parse score.py output for {output_path}:\n{proc.stdout}")
    got["raw"] = proc.stdout
    return got


def cross_check(mine: dict, theirs: dict, where: str) -> None:
    problems = []
    for key in ("simulated_ms", "misses", "cpu_us", "total_us"):
        if mine[key] != theirs[key]:
            problems.append(f"{key}: benchmark {mine[key]} vs score.py {theirs[key]}")
    if sorted(mine["missed_commits"]) != sorted(theirs["missed_commits"]):
        problems.append(f"missed commits: benchmark {sorted(mine['missed_commits'])} "
                        f"vs score.py {sorted(theirs['missed_commits'])}")
    if problems:
        raise CrossCheckError(f"score.py disagrees on {where}:\n  " + "\n  ".join(problems))


# ---------------------------------------------------------------- one run


# Replica / fingerprint / Mode 0 decisions ("... passes on the patched code ...; dropped").
_INTERESTING = re.compile(r"replica|fingerprint|mode ?0|patched code", re.IGNORECASE)


def parse_logs(stderr: str, cids) -> dict:
    """Per-commit ``[br] <cid>: ...`` lines and counts of interesting events."""
    cids = set(cids)
    per_commit: dict[str, list[str]] = {}
    other = []
    for line in stderr.splitlines():
        if not line.startswith("[br] "):
            if line.strip():
                other.append(line)
            continue
        body = line[5:]
        cid, sep, _ = body.partition(":")
        if sep and cid in cids:
            per_commit.setdefault(cid, []).append(body)
        else:
            other.append(line)
    every = sorted(c for c, ls in per_commit.items() if any(": every test" in l for l in ls))
    flagged = {c: [l for l in ls if _INTERESTING.search(l)] for c, ls in per_commit.items()}
    flagged = {c: ls for c, ls in flagged.items() if ls}
    return {
        "per_commit": per_commit,
        "other": other,
        "every_test_commits": every,
        "replica_lines": flagged,
        "replica_line_count": sum(len(ls) for ls in flagged.values()),
    }


def build_data_dir(base: Path, tests_path: Path, patches: dict) -> Path:
    data = base / "data"
    (data / "commits").mkdir(parents=True)
    shutil.copy(tests_path, data / "tests.json")
    for cid, path in patches.items():
        shutil.copy(path, data / "commits" / f"{cid}.patch")
    return data


def selector_env(data, maps, src, extra) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PYTHON", "BR_")) and k not in ("VIRTUAL_ENV", "__PYVENV_LAUNCHER__")}
    env.update(PYTHONDONTWRITEBYTECODE="1", BR_DATA=str(data), BR_MAPS=str(maps), BR_SRC=str(src))
    env.update(extra)
    return env


def run_once(ctx, name, extra, run_index, run_dir: Path) -> dict:
    run_dir.mkdir(parents=True)
    data = build_data_dir(run_dir, ctx["tests_path"], ctx["patches"])
    out_path, err_path = run_dir / "output.jsonl", run_dir / "stderr.log"
    m = measure_process(
        [ctx["python"], "-I", "-S", str(ROOT / "run.py")],
        selector_env(data, ctx["maps"], ctx["src"], extra), str(ROOT), out_path, err_path,
        ctx["timeout"],
    )
    where = f"config {name!r} run {run_index + 1} ({run_dir})"
    if m["exit_code"] != 0 or m["timed_out"]:
        tail = err_path.read_text("utf-8", "replace")[-3000:]
        raise CrossCheckError(f"selector failed on {where}: exit {m['exit_code']}, "
                              f"timed out {m['timed_out']}\n{tail}")

    stdout = out_path.read_bytes()
    theirs = run_score_py(data, ctx["score_answers"], out_path, m["cpu_us"])

    # score.py accepted the output, so it is valid: parse it plainly.
    outputs = {}
    for line in stdout.decode("utf-8").splitlines():
        d = json.loads(line)
        outputs[d["commit"]] = d["workers"]
    ms, answers = ctx["ms"], ctx["answers"]
    commits = {}
    for cid in sorted(answers):
        a = answers[cid]
        s = score_commit(outputs[cid], a["failing"], ms)
        commits[cid] = {
            "kind": a["kind"],
            "selected_count": len(s["selected"]),
            "selected": sorted(s["selected"]),
            "workers": s["workers"],
            "failing_count": len(a["failing"]),
            "unknown_count": len(a["unknown"]),
            "cost_ms": s["cost_ms"],
            "charged_ms": s["charged_ms"],
            "floor_ms": ctx["floors"][cid],
            "excess_ms": s["charged_ms"] - ctx["floors"][cid],
            "missed": [ctx["names"][t] for t in s["missed"]],
        }
    simulated = sum(c["charged_ms"] for c in commits.values())
    mine = {
        "simulated_ms": simulated,
        "misses": sum(1 for c in commits.values() if c["missed"]),
        "missed_commits": [cid for cid, c in commits.items() if c["missed"]],
        "cpu_us": m["cpu_us"],
        "total_us": simulated * 1000 + m["cpu_us"],
    }
    cross_check(mine, theirs, where)

    logs = parse_logs(err_path.read_text("utf-8", "replace"), answers)
    for cid, c in commits.items():
        c["log"] = logs["per_commit"].get(cid, [])
    return {
        "run": run_index + 1,
        **mine,
        "score_py_agrees": True,
        **m,
        "output_bytes": len(stdout),
        "every_test_commits": logs["every_test_commits"],
        "selected_all_commits": [cid for cid, c in commits.items()
                                 if c["selected_count"] == len(ms)],
        "replica_line_count": logs["replica_line_count"],
        "replica_lines": logs["replica_lines"],
        "other_log_lines": logs["other"],
        "commits": commits,
    }


# ---------------------------------------------------------------- summaries


def summarize(runs, floor_total, n_commits) -> dict:
    cpus = [r["cpu_us"] for r in runs]
    totals = [r["total_us"] for r in runs]
    sims = [r["simulated_ms"] for r in runs]
    median_total = statistics.median(totals)
    return {
        "runs": len(runs),
        "simulated_ms": sims,
        "simulated_stable": len(set(sims)) == 1,
        "selection_stable": all(
            r["commits"][c]["selected"] == runs[0]["commits"][c]["selected"]
            for r in runs for c in r["commits"]
        ),
        "misses": [r["misses"] for r in runs],
        "cpu_us": cpus,
        "cpu_median_us": statistics.median(cpus),
        "cpu_spread_us": max(cpus) - min(cpus),
        "total_us": totals,
        "total_median_us": median_total,
        "floor_ms": floor_total,
        "excess_total_s": (median_total - floor_total * 1000) / 1e6,
        "excess_per_commit_s": (median_total - floor_total * 1000) / 1e6 / n_commits,
        "wall_s": [r["wall_s"] for r in runs],
        "wall_median_s": statistics.median(r["wall_s"] for r in runs),
        "max_rss_kib": max(r["max_rss_kib"] for r in runs),
        "output_bytes": [r["output_bytes"] for r in runs],
        "every_test_commits": runs[0]["every_test_commits"],
        "replica_line_count": [r["replica_line_count"] for r in runs],
        "leftover_processes": any(r["leftover_processes"] for r in runs),
    }


def gate(excess_per_commit):
    if excess_per_commit <= 0.2:
        return "release"
    if excess_per_commit <= 0.6:
        return "interim"
    return "none"


def print_report(result):
    configs = result["configs"]
    first = next(iter(configs.values()))
    base_commits = first["runs"][0]["commits"]
    names = list(configs)
    if len(configs) == 1:
        print(f"{'commit':14s} {'kind':12s} {'sel':>5s} {'fail':>4s} {'cost':>8s} {'floor':>8s} "
              f"{'excess':>8s}  missed")
        for cid, c in base_commits.items():
            print(f"{cid:14s} {c['kind']:12s} {c['selected_count']:5d} {c['failing_count']:4d} "
                  f"{c['cost_ms'] / 1000:8.3f} {c['floor_ms'] / 1000:8.3f} "
                  f"{c['excess_ms'] / 1000:8.3f}  {', '.join(c['missed'][:3])}")
    else:
        head = f"{'commit':14s} {'kind':12s} {'floor':>8s} {names[0][:10]:>10s}"
        for n in names[1:]:
            head += f" {('d ' + n)[:12]:>12s}"
        print(head)
        for cid, c in base_commits.items():
            row = f"{cid:14s} {c['kind']:12s} {c['floor_ms'] / 1000:8.3f} {c['charged_ms'] / 1000:10.3f}"
            for n in names[1:]:
                o = configs[n]["runs"][0]["commits"][cid]
                mark = "*" if o["selected"] != c["selected"] else " "
                miss = "M" if o["missed"] else " "
                row += f" {(o['charged_ms'] - c['charged_ms']) / 1000:+10.3f}{mark}{miss}"
            print(row)
        print("(d = charged cost delta vs first config in run 1; * selected set changed; M miss)")
    print()
    print(f"{'config':12s} {'simulated':>10s} {'cpu med':>8s} {'spread':>7s} {'total med':>10s} "
          f"{'excess/c':>9s} {'gate':>8s} {'miss':>4s} {'wall':>6s} {'rss MiB':>7s} {'every':>5s} "
          f"{'repl':>4s}")
    for n, cfg in configs.items():
        s = cfg["summary"]
        print(f"{n[:12]:12s} {s['simulated_ms'][0] / 1000:10.3f} {s['cpu_median_us'] / 1e6:8.3f} "
              f"{s['cpu_spread_us'] / 1e6:7.3f} {s['total_median_us'] / 1e6:10.3f} "
              f"{s['excess_per_commit_s']:9.3f} {gate(s['excess_per_commit_s']):>8s} "
              f"{max(s['misses']):4d} {s['wall_median_s']:6.2f} {s['max_rss_kib'] / 1024:7.1f} "
              f"{len(s['every_test_commits']):5d} {s['replica_line_count'][0]:4d}")
        if not s["simulated_stable"] or not s["selection_stable"]:
            print(f"  WARNING {n}: selections differ between runs: simulated {s['simulated_ms']}")
        if s["leftover_processes"]:
            print(f"  WARNING {n}: processes outlived the selector; their CPU is not counted")
        print(f"  {n}: cpu per run " + ", ".join(f"{c / 1e6:.3f}" for c in s["cpu_us"])
              + " s; total per run " + ", ".join(f"{t / 1e6:.3f}" for t in s["total_us"]) + " s")
    if len(configs) > 1:
        base = first["summary"]
        for n in names[1:]:
            s = configs[n]["summary"]
            print(f"  {n} vs {names[0]}: simulated "
                  f"{(s['simulated_ms'][0] - base['simulated_ms'][0]) / 1000:+.3f} s, cpu median "
                  f"{(s['cpu_median_us'] - base['cpu_median_us']) / 1e6:+.3f} s, total median "
                  f"{(s['total_median_us'] - base['total_median_us']) / 1e6:+.3f} s")
    f = result["floor"]
    print(f"\nfloor {f['total_ms'] / 1000:.3f} s over {f['commits']} commits; "
          f"selector.schedule.split on the failing sets: {f['split_total_ms'] / 1000:.3f} s"
          + ("" if not f["split_differs"] else f"  WARNING differs on {f['split_differs']}"))
    print("score.py agreed on every output (simulated, misses, missed commits, CPU, total).")


# ---------------------------------------------------------------- main


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--patches", required=True)
    ap.add_argument("--tests", required=True)
    ap.add_argument("--answers", required=True)
    ap.add_argument("--src", required=True, help="pinned sqlglot checkout (BR_SRC)")
    ap.add_argument("--maps", default=str(ROOT / "maps" / "maps.bin"))
    ap.add_argument("--python", default=sys.executable, help="interpreter that runs the selector")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--config", action="append", type=parse_config, default=None)
    ap.add_argument("--out", default=None, help="full JSON result")
    ap.add_argument("--keep-output", default=None, help="keep every run's data, stdout, stderr here")
    ap.add_argument("--timeout", type=float, default=600.0, help="seconds per selector run")
    ap.add_argument("--no-precompile", action="store_true",
                    help="skip compiling selector/ before measuring (the image ships bytecode)")
    args = ap.parse_args(argv)

    # Absolute paths everywhere: the selector's replica subprocesses resolve BR_SRC
    # relative to their own working directory, so a relative path silently breaks them.
    patches_dir = Path(args.patches).resolve()
    tests_path = Path(args.tests).resolve()
    answers_path = Path(args.answers).resolve()
    src = Path(args.src).resolve()
    maps = Path(args.maps).resolve()
    python = shutil.which(args.python) if os.sep not in args.python else args.python
    python = str(Path(python).absolute()) if python else args.python
    for label, p in (("--patches", patches_dir), ("--tests", tests_path),
                     ("--answers", answers_path), ("--src", src), ("--maps", maps)):
        if not p.exists():
            ap.error(f"{label} {p} does not exist")
    configs = args.config or [("default", {})]
    if len({n for n, _ in configs}) != len(configs):
        ap.error("config names must be unique")

    tests = json.loads(tests_path.read_text("utf-8"))["tests"]
    ms = {t["number"]: duration_ms(t["seconds"]) for t in tests}
    names = {t["number"]: t["id"] for t in tests}
    answers = load_answers(answers_path)
    patch_files = {p.stem: p for p in sorted(patches_dir.glob("*.patch"))}
    no_answer = sorted(set(patch_files) - set(answers))
    no_patch = sorted(set(answers) - set(patch_files))
    if no_answer:
        print(f"note: {len(no_answer)} patches have no answer and are skipped", file=sys.stderr)
    if no_patch:
        print(f"note: {len(no_patch)} answers have no patch and are skipped", file=sys.stderr)
    answers = {c: a for c, a in answers.items() if c in patch_files}
    if not answers:
        ap.error("no commit has both a patch and an answer")
    patches = {c: patch_files[c] for c in answers}

    floors = {c: floor_ms(a["failing"], ms) for c, a in answers.items()}
    split_costs = {c: split_cost_ms(a["failing"], ms) for c, a in answers.items()}
    split_differs = sorted(c for c in answers if split_costs[c] != floors[c])

    keep = Path(args.keep_output).resolve() if args.keep_output else None
    if keep:
        keep.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="bench-", dir=keep))
    else:
        (ROOT / "work").mkdir(exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="bench-", dir=ROOT / "work"))
    score_answers = work / "answers-for-score.json"
    score_answers.write_text(json.dumps(answers_for_score_py(answers)))

    if not args.no_precompile:
        # The image runs `compileall` on selector/ at build time; do the same so the
        # first measured run does not pay for compiling the selector.
        subprocess.run([python, "-I", "-S", "-m", "compileall", "-q", str(ROOT / "selector"),
                        str(ROOT / "run.py")], check=False, capture_output=True)

    ctx = {"tests_path": tests_path, "patches": patches, "maps": maps, "src": src,
           "python": python, "timeout": args.timeout, "score_answers": score_answers,
           "ms": ms, "answers": answers, "names": names, "floors": floors}
    runs = {n: [] for n, _ in configs}
    try:
        for i in range(args.runs):
            for n, extra in configs:
                safe = re.sub(r"[^A-Za-z0-9_.-]", "_", n)
                r = run_once(ctx, n, extra, i, work / f"{safe}-run{i + 1}")
                runs[n].append(r)
                print(f"[bench] {n} run {i + 1}: simulated {r['simulated_ms'] / 1000:.3f} s, "
                      f"cpu {r['cpu_us'] / 1e6:.3f} s, misses {r['misses']}, "
                      f"wall {r['wall_s']:.2f} s", file=sys.stderr)
    except CrossCheckError as error:
        print(f"\nBENCHMARK FAILED: {error}", file=sys.stderr)
        print(f"(run files kept in {work})", file=sys.stderr)
        return 2

    floor_total = sum(floors.values())
    result = {
        "inputs": {"patches": str(patches_dir), "tests": str(tests_path),
                   "answers": str(answers_path), "src": str(src), "maps": str(maps),
                   "python": python, "runs": args.runs, "platform": sys.platform,
                   "skipped_patches_without_answer": no_answer,
                   "skipped_answers_without_patch": no_patch},
        "measurement_notes": CPU_NOTES,
        "floor": {"commits": len(answers), "total_ms": floor_total, "per_commit_ms": floors,
                  "split_total_ms": sum(split_costs.values()), "split_per_commit_ms": split_costs,
                  "split_differs": split_differs},
        "configs": {
            n: {"env": extra, "summary": summarize(runs[n], floor_total, len(answers)),
                "runs": runs[n]}
            for n, extra in configs
        },
    }
    if len(configs) > 1:
        base_name = configs[0][0]
        base = result["configs"][base_name]
        for n, _ in configs[1:]:
            cfg = result["configs"][n]
            b0, o0 = base["runs"][0]["commits"], cfg["runs"][0]["commits"]
            cfg["vs_first"] = {
                "first": base_name,
                "total_median_delta_us": cfg["summary"]["total_median_us"] - base["summary"]["total_median_us"],
                "simulated_delta_ms": cfg["summary"]["simulated_ms"][0] - base["summary"]["simulated_ms"][0],
                "cpu_median_delta_us": cfg["summary"]["cpu_median_us"] - base["summary"]["cpu_median_us"],
                "per_commit": {
                    c: {"charged_delta_ms": o0[c]["charged_ms"] - b0[c]["charged_ms"],
                        "selection_changed": o0[c]["selected"] != b0[c]["selected"]}
                    for c in b0
                },
            }
    if args.out:
        Path(args.out).resolve().write_text(json.dumps(result, indent=1, sort_keys=False) + "\n")
    print_report(result)
    if not keep:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
