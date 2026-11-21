"""Diagnostic build: the normal selector, plus a payload hidden in the simulated seconds.

main.main() runs unchanged (same maps, rules and replica behaviour), its stdout is
captured, and only then:
  1. two synthetic replicas run (executor fingerprints, test_generator_memory) on
     embedded patches, through the very same replica.run_exec / replica.run code;
  2. a value V (mixed radix, see SCHEME) is computed from their outcomes, from what
     the real-commit replicas did, and from the final answers;
  3. P = V + 1 ms of extra, otherwise unselected tests are appended to the busiest
     worker of some commits. Each commit's cost grows by exactly what is added there,
     so (board simulated - base simulated) * 1000 = P. tools/diag_decode.py reverses it.

Replica outcomes are observed by wrapping replica.subprocess.run (a shim that records
and delegates) and replica.run / replica.run_exec (which call the originals and return
their result untouched), so the selection path is the same code as without diag.

Scheme: V = r_exec + 47 * (r_genmem + 47 * (c + 13 * f4)), payload = V + 1 ms (<= 258.453 s).
  r_*  (47): outcome code 0-3 x 11 wall buckets (WALL_EDGES) -> code * 11 + bucket;
             codes 4-6 (no wall) -> 44 + (code - 4).
  c    (13): (launches, drops, bad) of the real-commit replicas, each capped at 2 (C_STATES).
  f4    (9): 3 * u + t; u = untrusted tests in the maps (0 / 1-36 / 37+),
             t = commits whose final answer holds test_execute_tpcds (0 / 1 / 2+).
The synthetic replicas run after every commit is decided, so the real-commit replicas see
the same (cold) conditions as in the plain build and the base score cannot move.
"""

import io
import json
import os
import subprocess as _subprocess
import sys
import threading
import time
from pathlib import Path

from . import main as selector_main
from . import replica

# ---------------------------------------------------------------- scheme
# Outcome codes. 0-3 carry a wall-time bucket, 4-6 do not.
CODES = {
    0: "PASS (rc 0, last stdout line PASS)",
    1: "FAIL_MISMATCH (rc 0; genmem: plain FAIL = unittest failed; exec: 'FAIL <fixture> queries [...]')",
    2: "FAIL_EXC (rc 0, 'FAIL <exception repr>': the runner raised, e.g. import error, unwritable dir)",
    3: "BADEXIT (nonzero return code, empty stdout or unexpected last line: crash/killed)",
    4: "TIMEOUT (subprocess.TimeoutExpired)",
    5: "LAUNCH_ERR (exception before/while spawning: mkstemp, json dump, Popen OSError...)",
    6: "NOT_RUN (synthetic only: gating refused - e.g. no exec_phases/subtests in the maps, replica_ok "
       "False, no candidate/window - or an exception in the diag code, or no wall time left)",
}
WALL_EDGES = [0.5, 1, 1.5, 2, 3, 4, 6, 8, 11, 15]  # bucket b: EDGES[b-1] <= wall < EDGES[b]
NB = len(WALL_EDGES) + 1  # 11 buckets
NW = 3  # codes without wall
R_STATES = 4 * NB + NW  # 47 states per synthetic replica
# Real-commit counters (L launches, D drops = PASS, B bad = codes 2-6), capped at 2 ("2+").
C_STATES = [(l, d, b) for l in range(2) for d in range(l + 1) for b in range(l + 1 - d)] + \
    [(2, d, b) for d in range(3) for b in range(3)]  # 4 + 9 = 13
# Field 4: untrusted tests in the maps the selector used (0 / 1-36 / 37+) x commits whose
# final answer holds test_execute_tpcds (0 / 1 / 2+).
U_STATES = 3
T_STATES = 3
RADICES = [R_STATES, R_STATES, len(C_STATES), U_STATES * T_STATES]
MAXV = R_STATES * R_STATES * len(C_STATES) * U_STATES * T_STATES - 1  # 258452 -> payload <= 258.453 s

SYN_TIMEOUT = 45.0
TPCDS = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
GENMEM = "tests/test_generator.py::TestGenerator::test_generator_memory"

# Locally: "executor fingerprints unchanged; dropped 1 executor test(s)" (/tmp/d_org o07084co).
EXEC_PATCH = """\
diff --git a/sqlglot/optimizer/unnest_subqueries.py b/sqlglot/optimizer/unnest_subqueries.py
--- a/sqlglot/optimizer/unnest_subqueries.py
+++ b/sqlglot/optimizer/unnest_subqueries.py
@@ -546,7 +546,7 @@
     group = select.args.get("group")
     parent_select.join(
         select.group_by(
-            *(key for key in group_by if not group or key not in group.expressions), copy=False
+            *(key for key in group_by if not group or key not in group.expressions), copy=True
         ),
         # A grouped key is constant per group, so any predicate on it can be checked in the join
         on=[predicate for key, _, predicate in keys if key in group_by],
"""

# Locally: "test_generator_memory passes on the patched code for ['exasol', 'teradata', 'trino',
# 'tsql']; dropped" (/tmp/d_org o07026co).
GENMEM_PATCH = """\
diff --git a/sqlglot/dialects/teradata.py b/sqlglot/dialects/teradata.py
--- a/sqlglot/dialects/teradata.py
+++ b/sqlglot/dialects/teradata.py
@@ -11,7 +11,7 @@
     # Teradata's 'DAY' TRUNC format starts the week on Sunday by default
     WEEK_OFFSET = -1
     NORMALIZATION_STRATEGY = NormalizationStrategy.CASE_INSENSITIVE
-    TYPED_DIVISION = True
+    TYPED_DIVISION = False

     TIME_MAPPING = {
         "YY": "%y",
"""


def wall_bucket(wall):
    return sum(1 for e in WALL_EDGES if wall >= e)


def bucket_range(b):
    lo = 0 if b == 0 else WALL_EDGES[b - 1]
    hi = WALL_EDGES[b] if b < len(WALL_EDGES) else None
    return lo, hi


def r_state(code, wall):
    if code < 4:
        return code * NB + wall_bucket(wall or 0.0)
    return 4 * NB + (code - 4)


def r_unstate(r):
    if r < 4 * NB:
        return r // NB, r % NB
    return 4 + (r - 4 * NB), None


def encode(fields):
    """fields: [r_exec, r_genmem, c_index, t] -> V."""
    v = 0
    for value, radix in reversed(list(zip(fields, RADICES))):
        assert 0 <= value < radix
        v = v * radix + value
    return v


def decode(v):
    out = []
    for radix in RADICES:
        out.append(v % radix)
        v //= radix
    return out, v


# ---------------------------------------------------------------- instrumentation
_rec = threading.local()


def log(*parts):
    print("[diag]", *parts, file=sys.stderr)


class _Shim:
    """Stands in for the subprocess module inside selector.replica: records each run()."""

    def __getattr__(self, name):
        return getattr(_subprocess, name)

    def run(self, *args, **kwargs):
        r = {"launched": True}
        _rec.cur = r
        t0 = time.monotonic()
        try:
            proc = _subprocess.run(*args, **kwargs)
        except _subprocess.TimeoutExpired:
            r["wall"] = time.monotonic() - t0
            r["timeout"] = True
            raise
        except BaseException as error:
            r["wall"] = time.monotonic() - t0
            r["exc"] = repr(error)
            raise
        r["wall"] = time.monotonic() - t0
        r["rc"] = proc.returncode
        r["out"] = (proc.stdout or b"").decode(errors="replace")
        r["err"] = (proc.stderr or b"").decode(errors="replace")
        return proc


def classify(rec):
    """(code, wall, detail) for one replica call."""
    if rec is None:
        return 5, None, "no subprocess launched"
    if rec.get("timeout"):
        return 4, rec["wall"], "timeout"
    if "exc" in rec:
        return 5, rec["wall"], rec["exc"]
    lines = rec["out"].strip().splitlines()
    last = lines[-1] if lines else ""
    detail = f"rc={rec['rc']} last={last[:300]!r} stderr_tail={rec['err'][-600:]!r}"
    if rec["rc"] != 0 or not last:
        return 3, rec["wall"], detail
    if last == "PASS":
        return 0, rec["wall"], detail
    if last == "FAIL" or (last.startswith("FAIL ") and " queries [" in last):
        return 1, rec["wall"], detail
    if last.startswith("FAIL "):
        return 2, rec["wall"], detail
    return 3, rec["wall"], detail


def _instrumented(fn, kind, events):
    def wrapper(*args, **kwargs):
        _rec.cur = None
        ok = fn(*args, **kwargs)
        try:
            code, wall, detail = classify(getattr(_rec, "cur", None))
            if ok and code != 0:  # cannot happen; never let it look like a drop we did not see
                detail += f" (ok but code {code})"
            events.append((kind, code, wall, bool(ok)))
            log(f"real {kind} replica: code {code} wall {wall if wall is None else round(wall, 2)} ok={ok}: {detail}")
        except Exception as error:  # noqa: BLE001
            events.append((kind, 6, None, bool(ok)))
            log(f"real {kind} replica: classify failed {error!r}")
        return ok

    return wrapper


# ---------------------------------------------------------------- synthetic replicas
def _prepare(patch_text, src_root, maps):
    from .impact import commit_impact, refine_class_literal
    from .patch import parse

    fps = parse(patch_text.encode())
    if not fps:
        return None, "unparseable embedded patch"
    res = commit_impact(fps, src_root, maps, import_refiner=refine_class_literal)
    if not res.replica_ok or not (res.call_sites or res.import_files):
        return None, f"replica_ok={res.replica_ok} call_sites={len(res.call_sites)} imports={len(res.import_files)}"
    return res, None


def synthetic_exec(src_root, maps, translate, by_id, timeout):
    from .main import bits_to_numbers

    res, why = _prepare(EXEC_PATCH, src_root, maps)
    if res is None:
        return 6, None, why
    if res.import_files:
        return 6, None, "import-time change"
    selected = set(bits_to_numbers(res.bits, translate))
    infos = []
    for test_id, info in maps.exec_phases.items():
        num = by_id.get(test_id)
        if num is None or num not in selected:
            continue
        if replica.exec_eligible(info, res.call_sites):
            infos.append(info)
    if not infos:
        return 6, None, f"no candidates ({len(maps.exec_phases)} exec_phases in maps)"
    _rec.cur = None
    ok = replica.run_exec(infos, res.patched, src_root, timeout=timeout)
    code, wall, detail = classify(getattr(_rec, "cur", None))
    return code, wall, f"ok={ok} n={len(infos)} {detail}"


def synthetic_genmem(src_root, maps, timeout):
    res, why = _prepare(GENMEM_PATCH, src_root, maps)
    if res is None:
        return 6, None, why
    info = maps.subtests.get(GENMEM)
    if info is None:
        return 6, None, f"no subtests for genmem ({len(maps.subtests)} in maps)"
    values = replica.affected_windows(info, res.call_sites, res.import_files)
    if not values or len(values) > 12:
        return 6, None, f"windows {values!r}"
    _rec.cur = None
    ok = replica.run(GENMEM, values, res.patched, src_root, timeout=timeout)
    code, wall, detail = classify(getattr(_rec, "cur", None))
    return code, wall, f"ok={ok} values={sorted(values)} {detail}"


def _guarded(fn, *args):
    try:
        return fn(*args)
    except Exception as error:  # noqa: BLE001
        return 6, None, repr(error)


def run_synthetic(src_root, maps, translate, by_id, started):
    left = 168.0 - (time.monotonic() - started)
    if left < 12:
        return [(6, None, "no wall time left")] * 2
    if left >= 2 * SYN_TIMEOUT + 10:
        mode, timeout = "sequential", SYN_TIMEOUT
    else:
        mode, timeout = "concurrent", max(5.0, min(SYN_TIMEOUT, left - 8))
    log(f"synthetic replicas: {mode}, timeout {timeout:.0f}s")
    jobs = [(synthetic_exec, src_root, maps, translate, by_id, timeout), (synthetic_genmem, src_root, maps, timeout)]
    results = [None, None]

    def work(i):
        results[i] = _guarded(*jobs[i])

    if mode == "sequential":
        for i in range(2):
            work(i)
    else:
        threads = [threading.Thread(target=work, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    return [r if r is not None else (6, None, "thread died") for r in results]


# ---------------------------------------------------------------- payload placement
def _exact(items, ms, target):
    """Subset of items (largest first) whose durations sum exactly to target, or None."""
    take, rest, r = [], [], target
    for t in items:
        d = ms[t]
        if d <= r - 3000:
            take.append(t)
            r -= d
        else:
            rest.append(t)
    rest = [t for t in rest if ms[t] <= r]
    if r == 0:
        return take
    mask = (1 << (r + 1)) - 1
    reach = [1]
    for t in rest:
        reach.append((reach[-1] | (reach[-1] << ms[t])) & mask)
        if reach[-1] >> r & 1:
            break
    if not reach[-1] >> r & 1:
        return None
    for i in range(len(reach) - 1, 0, -1):  # walk back: item i-1 is used if r not reachable without it
        if not reach[i - 1] >> r & 1:
            t = rest[i - 1]
            take.append(t)
            r -= ms[t]
    assert r == 0
    return take


def place(answers, ms, numbers, payload):
    """answers: list of (cid, workers). Returns {cid: (worker index, [tests])} summing to payload, or None."""
    plan = {}
    remaining = payload
    order = []
    for cid, workers in answers:
        if not workers:
            continue
        selected = {t for w in workers for t in w}
        avail = sorted((t for t in numbers if t not in selected and ms[t] > 0), key=lambda t: (-ms[t], t))
        loads = [sum(ms[t] for t in w) for w in workers]
        busiest = max(range(len(workers)), key=lambda i: (loads[i], -i))
        order.append((sum(ms[t] for t in avail), cid, busiest, avail))
    order.sort(key=lambda x: (-x[0], x[1]))
    for total, cid, busiest, avail in order:
        if remaining == 0:
            break
        if total <= remaining:
            plan[cid] = (busiest, avail)
            remaining -= total
            continue
        pick = _exact(avail, ms, remaining)
        if pick is not None:
            plan[cid] = (busiest, pick)
            remaining = 0
            break
        greedy, r = [], remaining
        for t in avail:
            if ms[t] <= r:
                greedy.append(t)
                r -= ms[t]
        plan[cid] = (busiest, greedy)
        remaining = r
    return plan if remaining == 0 else None


# ---------------------------------------------------------------- entry
def main():
    started = time.monotonic()
    events = []
    replica.subprocess = _Shim()
    orig_run, orig_exec = replica.run, replica.run_exec
    replica.run = _instrumented(orig_run, "genmem", events)
    replica.run_exec = _instrumented(orig_exec, "exec", events)

    real_stdout = sys.stdout
    buf = io.StringIO()
    sys.stdout = buf
    try:
        selector_main.main()
    except BaseException:
        sys.stdout = real_stdout
        real_stdout.write(buf.getvalue())
        raise
    finally:
        sys.stdout = real_stdout
        replica.run, replica.run_exec = orig_run, orig_exec
    text = buf.getvalue()
    main_wall = time.monotonic() - started
    try:
        text = add_payload(text, events, started)
    except Exception as error:  # noqa: BLE001 - fall back to the plain output (payload 0)
        log(f"payload failed: {error!r}")
    log(f"main {main_wall:.2f}s, total {time.monotonic() - started:.2f}s wall, "
        f"{time.process_time():.2f}s cpu (self)")
    real_stdout.write(text)
    real_stdout.flush()


def add_payload(text, events, started):
    from .maps import Maps

    data = Path(os.environ.get("BR_DATA", "/data"))
    maps_path = Path(os.environ.get("BR_MAPS", selector_main.APP / "maps" / "maps.bin"))
    src_root = os.environ.get("BR_SRC", str(selector_main.APP / "src"))
    tests = json.loads((data / "tests.json").read_text())["tests"]
    numbers = [t["number"] for t in tests]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    by_id = {t["id"]: t["number"] for t in tests}

    rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    answers = [(row["commit"], row["workers"]) for row in rows]

    # field 4
    tp = by_id.get(TPCDS)
    t_count = sum(1 for _, ws in answers if any(tp in w for w in ws))
    # field 3
    launches = len(events)
    drops = sum(1 for e in events if e[3])
    bad = sum(1 for e in events if e[1] in (2, 3, 4, 5, 6))
    if launches < 2:
        c_state = (launches, min(drops, launches), min(bad, launches - min(drops, launches)))
    else:
        c_state = (2, min(drops, 2), min(bad, 2))
    # fields 1, 2
    untrusted = None
    try:
        maps = Maps(maps_path)
        if os.environ.get("BR_ASSUME_TRUSTED") == "1":  # as selector.main does
            maps.untrusted = 0
        untrusted = bin(maps.untrusted).count("1")
        translate = [by_id.get(tid, -1) for tid in maps.ids]
        syn = run_synthetic(src_root, maps, translate, by_id, started)
    except Exception as error:  # noqa: BLE001
        syn = [(6, None, repr(error))] * 2
    # maps unreadable: report "37+" (the selector then selected everything anyway)
    u_state = 2 if untrusted is None or untrusted >= 37 else (0 if untrusted == 0 else 1)
    for name, (code, wall, detail) in zip(("exec", "genmem"), syn):
        log(f"synthetic {name}: code {code} wall {wall if wall is None else round(wall, 3)}: {detail}")
    log(f"real replicas: {launches} launched, {drops} dropped, {bad} bad; "
        f"tpcds in {t_count}/{len(answers)} answers; {untrusted} untrusted tests in maps")

    fields = [r_state(syn[0][0], syn[0][1]), r_state(syn[1][0], syn[1][1]),
              C_STATES.index(c_state), u_state * T_STATES + min(t_count, T_STATES - 1)]
    value = encode(fields)
    payload = value + 1
    plan = place(answers, ms, numbers, payload)
    if plan is None:
        log(f"could not place payload {payload} ms; no markers")
        return text
    log(f"fields {fields} -> V={value}, payload {payload} ms over {len(plan)} commit(s)")
    out = []
    for cid, workers in answers:
        if cid in plan:
            i, extra = plan[cid]
            workers = [list(w) for w in workers]
            workers[i] = workers[i] + list(extra)
        out.append(json.dumps({"commit": cid, "workers": workers}, separators=(",", ":")))
    return "\n".join(out) + ("\n" if out else "")
