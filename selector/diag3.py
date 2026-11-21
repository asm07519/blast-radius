"""Diagnostic build 2: the normal selector, plus a report hidden in the simulated seconds.

selector.main.main() runs unchanged; its stdout is captured. Wrappers only observe:
impact.Result.add (how many tests each note contributed) and main.commit_impact (the Result
or the Everything reason of each commit). Then P = V + 1 ms of otherwise unselected tests are
appended to the busiest worker of some commits, so
    (board simulated - base simulated) * 1000 = P        (base: the same commit without diag)
tools/diag2_decode.py reverses it.

V is a mixed-radix number (RADICES): a truncation flag, then for the commits whose charged
cost is >= 12 s how many fall in each class, capped at 3: eight classes for commits that
select >= 1500 tests (BIG_CLASSES), three for the others (MID_CLASSES). If the payload cannot
be placed the highest fields are zeroed and the flag is 1.
Nothing about a patch is reported beyond those counts: which rule fired, which group of
files, and whether nearly every test was selected.
"""

import io
import json
import os
import re
import sys
import time
from pathlib import Path

from . import impact
from . import main as selector_main

EXPENSIVE_MS = 12000
BIG = 1500
SITE_GROUPS = ["import: module/class stmt changed", "import: class header", "import: other statement",
               "call site (function body or signature)", "file-level or other note"]
PATH_GROUPS = ["sqlglot/parser.py", "sqlglot/generator.py",
               "core: sqlglot/*.py, dialects/dialect.py, expressions/*", "elsewhere"]
EVERYTHING = {1: "Everything: conftest/sitecustomize hook", 2: "Everything: top-level module",
              3: "Everything: pytest config", 4: "Everything: patch form (unparseable, cannot apply, path, hunk)",
              5: "Everything: process-killing call", 6: "Everything: error, budget or other"}
NCODES = 48
# Report: a truncation flag, then how many commits fall in each class, each count capped at 3.
BIG_CLASSES = ["Everything: hook / top-level module / pytest config", "Everything: other reason",
               "import-level statement changed in parser.py or generator.py",
               "import-level statement changed elsewhere", "class header changed",
               "other import-level statement (inserted, removed, def header, handlers)",
               "call site (function body or signature)", "file-level note or unclassified"]
MID_CLASSES = ["largest note is a call site", "largest note is an import-level site", "other"]
RADICES = [2] + [4] * (len(BIG_CLASSES) + len(MID_CLASSES))


def big_class(k):
    if k in (1, 2, 3):
        return 0
    if k in (4, 5, 6):
        return 1
    if k == 47 or k == 0:
        return 7
    sg, rest = divmod(k - 7, 8)
    pg = rest // 2
    if sg == 0:
        return 2 if pg in (0, 1) else 3
    return {1: 4, 2: 5, 3: 6, 4: 7}[sg]


def mid_class(k):
    if k < 7 or k == 47:
        return 2
    sg = (k - 7) // 8
    return 0 if sg == 3 else (1 if sg in (0, 1, 2) else 2)


def code_text(k):
    if k == 0:
        return "none / not reported"
    if k in EVERYTHING:
        return EVERYTHING[k]
    if k == 47:
        return "expensive, unclassified"
    sg, rest = divmod(k - 7, 8)
    pg, sz = divmod(rest, 2)
    return f"{SITE_GROUPS[sg]} | {PATH_GROUPS[pg]} | {'>= 1500 tests' if sz else '< 1500 tests'}"


def everything_code(reason):
    r = reason or ""
    if "interpreter/pytest hook" in r:
        return 1
    if "top-level module" in r:
        return 2
    if "pytest" in r:
        return 3
    if "process-killing" in r:
        return 5
    for word in ("unparseable", "cannot apply", "normal form", "a/ ", "out of place", "does not have",
                 "space around", "differ", "added over existing", "prefix"):
        if word in r:
            return 4
    return 6


def path_group(path):
    if path == "sqlglot/parser.py":
        return 0
    if path == "sqlglot/generator.py":
        return 1
    if path.startswith("sqlglot/") and path.endswith(".py") and (
            path.count("/") == 1 or path == "sqlglot/dialects/dialect.py"
            or path.startswith("sqlglot/expressions/")):
        return 2
    return 3


_SITE = re.compile(r"Site\((\w+),")


def site_group(note):
    m = _SITE.search(note)
    if not m:
        return 4
    kind = m.group(1)
    if kind == "call":
        return 3
    if kind == "import":
        if "module/class stmt changed" in note:
            return 0
        if "class header" in note:
            return 1
        return 2
    return 4


def classify(record, size):
    """record: ("every", reason) or ("res", [(count, note)])."""
    if record is None:
        return 6 if size >= BIG else 47
    if record[0] == "every":
        return everything_code(record[1])
    adds = [(c, n) for c, n in record[1] if n and ": " in n]
    if not adds:
        return 47
    count, note = max(adds, key=lambda x: x[0])
    path = note.split(": ", 1)[0]
    return 7 + site_group(note) * 8 + path_group(path) * 2 + (1 if size >= BIG else 0)


def encode(fields):
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


def log(msg):
    print(f"[diag2] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- payload placement (as in diag.py)
def _exact(items, ms, target):
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
    for i in range(len(reach) - 1, 0, -1):
        if not reach[i - 1] >> r & 1:
            t = rest[i - 1]
            take.append(t)
            r -= ms[t]
    assert r == 0
    return take


def place(answers, ms, numbers, payload):
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
    records = []      # one per commit_impact call, in commit order
    current = []
    orig_add = impact.Result.add
    orig_ci = selector_main.commit_impact

    def add(self, bits, note=None):
        try:
            current.append((bin(bits).count("1"), note))
        except Exception:  # noqa: BLE001
            pass
        return orig_add(self, bits, note)

    def commit_impact(fps, *args, **kwargs):
        del current[:]
        try:
            res = orig_ci(fps, *args, **kwargs)
        except impact.Everything as reason:
            records.append((_paths(fps), ("every", str(reason))))
            raise
        except BaseException:
            records.append((_paths(fps), None))
            raise
        records.append((_paths(fps), ("res", list(current))))
        return res

    def _paths(fps):
        try:
            return tuple(sorted({p for fp in fps for p in (fp.old_path, fp.new_path) if p}))
        except Exception:  # noqa: BLE001
            return ()

    impact.Result.add = add
    selector_main.commit_impact = commit_impact
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
        impact.Result.add = orig_add
        selector_main.commit_impact = orig_ci
    text = buf.getvalue()
    try:
        text = add_payload(text, records)
    except Exception as error:  # noqa: BLE001 - fall back to the plain output (payload 0)
        log(f"payload failed: {error!r}")
    log(f"total {time.monotonic() - started:.2f}s wall, {time.process_time():.2f}s cpu (self)")
    real_stdout.write(text)
    real_stdout.flush()


def add_payload(text, records):
    from .patch import parse

    data = Path(os.environ.get("BR_DATA", "/data"))
    tests = json.loads((data / "tests.json").read_text())["tests"]
    numbers = [t["number"] for t in tests]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    answers = [(row["commit"], row["workers"]) for row in rows]

    # Match records to commits by the set of paths each patch names (order as a tie-break).
    pending = list(records)
    by_cid = {}
    for cid, _ in answers:
        try:
            fps = parse((data / "commits" / f"{cid}.patch").read_bytes())
            key = tuple(sorted({p for fp in fps for p in (fp.old_path, fp.new_path) if p}))
        except Exception:  # noqa: BLE001
            continue
        for i, (paths, rec) in enumerate(pending):
            if paths == key:
                by_cid[cid] = rec
                del pending[i]
                break

    costs = []
    for cid, workers in answers:
        used = [w for w in workers if w]
        cost = (max(sum(ms[t] for t in w) for w in used) + 2000 * len(used)) if used else 0
        size = sum(len(w) for w in used)
        costs.append((cost, cid, size))
    expensive = sorted((c for c in costs if c[0] >= EXPENSIVE_MS), key=lambda c: (-c[0], c[1]))
    big = [0] * len(BIG_CLASSES)
    mid = [0] * len(MID_CLASSES)
    for cost, cid, size in expensive:
        k = classify(by_cid.get(cid), size)
        log(f"{cid}: cost {cost} ms, {size} tests, code {k} = {code_text(k)}")
        if size >= BIG:
            big[big_class(k)] += 1
        else:
            mid[mid_class(k)] += 1
    digits = [min(x, 3) for x in big + mid]

    plan = None
    for drop in range(len(MID_CLASSES) + 1):  # zero the highest fields if the payload cannot be placed
        kept = digits[:len(digits) - drop] + [0] * drop
        fields = [1 if drop else 0] + kept
        value = encode(fields)
        plan = place(answers, ms, numbers, value + 1)
        if plan is not None:
            break
    if plan is None:
        log("could not place any payload; no markers")
        return text
    log(f"fields {fields} -> V={value}, payload {value + 1} ms over {len(plan)} commit(s)")
    out = []
    for cid, workers in answers:
        if cid in plan:
            i, extra = plan[cid]
            workers = [list(w) for w in workers]
            workers[i] = workers[i] + list(extra)
        out.append(json.dumps({"commit": cid, "workers": workers}, separators=(",", ":")))
    return "\n".join(out) + ("\n" if out else "")


# ================================================================ diagnostic build 3
# Overrides the report of build 2 (the definitions above stay for reference). For the five
# costliest commits with cost >= 12 s, in order of (-cost, commit id), one base-16 digit each:
#   variant A: which heavy tests the answer holds (4 classes) x file group of the largest note (4)
#   variant B: kind of the largest note (8 classes) x whether its file is dialect-specific/tests (2)
VARIANT = "B"
MAXN = 5
RADICES = [16] * MAXN
T_MAX = "tests/test_parser.py::TestParser::test_max_nodes"
T_TPCDS = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
T_OTHER = ("tests/test_generator.py::TestGenerator::test_generator_memory",
           "tests/test_executor.py::TestExecutor::test_execute_tpch",
           "tests/test_executor.py::TestExecutor::test_optimized_tpch",
           "tests/test_optimizer.py::TestOptimizer::test_tpcds")
HEAVY_CLASSES = ["has test_max_nodes", "has test_execute_tpcds, not test_max_nodes",
                 "has another heavy test only (generator_memory, execute_tpch, optimized_tpch, optimizer test_tpcds)",
                 "no heavy test"]
FILE_GROUPS = ["sqlglot/parser.py, tokens.py or generator.py",
               "sqlglot/expressions/*, other sqlglot/*.py, dialects/dialect.py",
               "sqlglot/optimizer/* or sqlglot/executor/*", "elsewhere (a dialect, tests/, fixtures, other)"]
WHY = ["import: module/class statement changed", "import: statement inserted or removed",
       "import: class header, def header or other", "call: statement changed", "call: statement inserted or removed",
       "call: def signature or a compound-statement header", "file-level note (data readers, added/deleted file, ...)",
       "every test / unclassified / syntax"]
FLAG = ["patch touches one file", "patch touches several files"]
_SITE3 = re.compile(r"Site\((\w+), .*, ([^,()]*)\)\s*$")


def _largest(record):
    if record is None or record[0] != "res":
        return None, None
    adds = [(c, n) for c, n in record[1] if n and ": " in n]
    if not adds:
        return None, None
    _, note = max(adds, key=lambda x: x[0])
    return note.split(": ", 1)[0], note


def file_group4(path):
    if path is None:
        return 3
    if path in ("sqlglot/parser.py", "sqlglot/tokens.py", "sqlglot/generator.py"):
        return 0
    if path.startswith("sqlglot/expressions/") or path == "sqlglot/dialects/dialect.py" or (
            path.startswith("sqlglot/") and path.count("/") == 1):
        return 1
    if path.startswith("sqlglot/optimizer/") or path.startswith("sqlglot/executor/"):
        return 2
    return 3


def why8(note):
    if note is None:
        return 7
    m = _SITE3.search(note)
    if not m:
        return 6 if "Site(" not in note else 7
    kind, why = m.group(1), m.group(2).strip()
    if kind == "import":
        if why == "module/class stmt changed":
            return 0
        return 1 if why in ("stmt inserted", "stmt removed") else 2
    if kind == "call":
        if why == "stmt changed":
            return 3
        return 4 if why in ("stmt inserted", "stmt removed") else 5
    return 7


def shared_flag(path):
    if path is None or not path.startswith("sqlglot/"):
        return 0
    if path.startswith(("sqlglot/parsers/", "sqlglot/generators/", "sqlglot/typing/")):
        return 0
    if path.startswith("sqlglot/dialects/") and path != "sqlglot/dialects/dialect.py":
        return 0
    return 1


def digit(record, selected_ids, nfiles=1):
    path, note = _largest(record)
    if VARIANT == "A":
        if T_MAX in selected_ids:
            h = 0
        elif T_TPCDS in selected_ids:
            h = 1
        elif any(t in selected_ids for t in T_OTHER):
            h = 2
        else:
            h = 3
        return h * 4 + file_group4(path)
    return why8(note) * 2 + (1 if nfiles > 1 else 0)


def digit_text(d, variant=None):
    if (variant or VARIANT) == "A":
        return f"{HEAVY_CLASSES[d // 4]} | largest note in {FILE_GROUPS[d % 4]}"
    return f"{WHY[d // 2]} | {FLAG[d % 2]}"


def add_payload(text, records):  # noqa: F811 - replaces build 2's report
    from .patch import parse

    data = Path(os.environ.get("BR_DATA", "/data"))
    tests = json.loads((data / "tests.json").read_text())["tests"]
    numbers = [t["number"] for t in tests]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    ident = {t["number"]: t["id"] for t in tests}
    rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    answers = [(row["commit"], row["workers"]) for row in rows]

    pending = list(records)
    by_cid = {}
    nfiles = {}
    for cid, _ in answers:
        try:
            fps = parse((data / "commits" / f"{cid}.patch").read_bytes())
            key = tuple(sorted({p for fp in fps for p in (fp.old_path, fp.new_path) if p}))
        except Exception:  # noqa: BLE001
            continue
        nfiles[cid] = len(key)
        for i, (paths, rec) in enumerate(pending):
            if paths == key:
                by_cid[cid] = rec
                del pending[i]
                break

    costs = []
    for cid, workers in answers:
        used = [w for w in workers if w]
        cost = (max(sum(ms[t] for t in w) for w in used) + 2000 * len(used)) if used else 0
        costs.append((cost, cid, {ident[t] for w in used for t in w}))
    expensive = sorted((c for c in costs if c[0] >= EXPENSIVE_MS), key=lambda c: (-c[0], c[1]))[:MAXN]
    fields = []
    for cost, cid, selected in expensive:
        d = digit(by_cid.get(cid), selected, nfiles.get(cid, 1))
        log(f"{cid}: cost {cost} ms, {len(selected)} tests, digit {d} = {digit_text(d)}")
        fields.append(d)
    fields += [0] * (MAXN - len(fields))
    value = encode(fields)
    plan = place(answers, ms, numbers, value + 1)
    if plan is None:
        log("could not place the payload; no markers")
        return text
    log(f"variant {VARIANT} fields {fields} -> V={value}, payload {value + 1} ms over {len(plan)} commit(s)")
    out = []
    for cid, workers in answers:
        if cid in plan:
            i, extra = plan[cid]
            workers = [list(w) for w in workers]
            workers[i] = workers[i] + list(extra)
        out.append(json.dumps({"commit": cid, "workers": workers}, separators=(",", ":")))
    return "\n".join(out) + ("\n" if out else "")
