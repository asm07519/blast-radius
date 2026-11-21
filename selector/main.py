"""Blast radius selector: for each patch, the tests that could fail, split over workers.

Reads /data/tests.json and /data/commits/*.patch, writes one JSON line per commit
to stdout. Logs go to stderr. Any failure on a commit selects every test for it.
"""

import json
import os
import sys
import time
from pathlib import Path

from . import import_probe, mode0, replica, schedule
from .impact import Everything, commit_impact, refine_class_literal
from .maps import Maps
from .patch import parse

APP = Path(__file__).resolve().parent.parent


def log(*parts):
    print("[br]", *parts, file=sys.stderr)


def bits_to_numbers(bits, translate):
    out = []
    i = 0
    while bits:
        low = bits & -bits
        i = low.bit_length() - 1
        num = translate[i] if i < len(translate) else -1
        if num >= 0:
            out.append(num)
        bits ^= low
    return out


def prune_executor_tests(cid, selected, res, maps, by_id, ms, src_root):
    """Drop executor tests whose expensive phase cannot see the change, if the cheap phase
    produces the same fingerprints on the patched code."""
    if res.import_files:
        return selected
    candidates = []
    for test_id, info in maps.exec_phases.items():
        num = by_id.get(test_id)
        if num is None or num not in selected:
            continue
        if replica.exec_eligible(info, res.call_sites):
            candidates.append((num, info))
    if not candidates:
        return selected
    rest = selected - {num for num, _ in candidates}
    saving = schedule.cost(schedule.split(selected, ms), ms) - schedule.cost(schedule.split(rest, ms), ms)
    if saving < 4000:
        return selected
    if replica.run_exec([info for _, info in candidates], res.patched, src_root):
        log(f"{cid}: executor fingerprints unchanged; dropped {len(candidates)} executor test(s)")
        return rest
    log(f"{cid}: executor fingerprints changed; kept")
    return selected


def prune_with_replicas(cid, selected, res, maps, by_id, ms, src_root):
    selected = prune_executor_tests(cid, selected, res, maps, by_id, ms, src_root)
    for test_id in replica.REPLICAS:
        num = by_id.get(test_id)
        info = maps.subtests.get(test_id)
        if num is None or num not in selected or info is None:
            continue
        values = replica.affected_windows(info, res.call_sites, res.import_files)
        if not values or len(values) > 12:
            continue
        rest = selected - {num}
        saving = schedule.cost(schedule.split(selected, ms), ms) - schedule.cost(schedule.split(rest, ms), ms)
        if saving < 1500:
            continue
        if replica.run(test_id, values, res.patched, src_root):
            log(f"{cid}: {test_id.split('::')[-1]} passes on the patched code for {sorted(values)}; dropped")
            selected = rest
        else:
            log(f"{cid}: {test_id.split('::')[-1]} replica did not pass; kept")
    return selected


def main(argv=None):
    started = time.monotonic()
    data = Path(os.environ.get("BR_DATA", "/data"))
    maps_path = Path(os.environ.get("BR_MAPS", APP / "maps" / "maps.bin"))
    src_root = os.environ.get("BR_SRC", str(APP / "src"))
    # Hard budget: past it, remaining commits select every test (the run must finish
    # well inside the scorer's 3-minute limit). Replicas stop earlier, at the soft budget.
    budget = float(os.environ.get("BR_BUDGET", "140"))
    replica_budget = float(os.environ.get("BR_REPLICA_BUDGET", "80"))
    verbose = os.environ.get("BR_VERBOSE", "1") == "1"

    tests = json.loads((data / "tests.json").read_text())["tests"]
    numbers = [t["number"] for t in tests]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    every = list(numbers)
    patches = sorted((data / "commits").glob("*.patch"))

    answers = {}
    try:
        maps = Maps(maps_path)
        by_id = {t["id"]: t["number"] for t in tests}
        translate = [by_id.get(tid, -1) for tid in maps.ids]
        known = {n for n in translate if n >= 0}
        # Tests the maps know nothing about are treated like untrusted ones.
        unmapped = [n for n in numbers if n not in known]
        mapped_numbers = set(bits_to_numbers(maps.mapped, translate))
        unmapped += [n for n in known if n not in mapped_numbers]
        if os.environ.get("BR_ASSUME_TRUSTED") == "1":  # evaluation only: what full maps would give
            maps.untrusted = 0
        untrusted_numbers = set(bits_to_numbers(maps.untrusted, translate))
        log(f"maps: {len(maps.ids)} tests, {len(untrusted_numbers)} untrusted, {len(unmapped)} unmapped")
    except Exception as error:
        log(f"maps unavailable ({error!r}); selecting every test")
        maps = None
    probe_modules = None
    if maps is not None:
        try:
            probe_modules = import_probe.modules_from_maps(maps)
        except Exception as error:
            log(f"import probe off: {error!r}")
    session = None  # Mode 0 (BR_MODE0=1, default off): see selector/mode0.py
    if maps is not None:
        try:
            session = mode0.from_env(tests, src_root, started, replica_budget, budget, log)
        except Exception as error:
            log(f"mode0 off: {error!r}")

    for path in patches:
        cid = path.stem
        if maps is None or time.monotonic() - started > budget:
            answers[cid] = every
            continue
        try:
            raw = path.read_bytes()
            if not raw.strip():
                answers[cid] = []  # an empty patch changes nothing
                log(f"{cid}: empty patch")
                continue
            fps = parse(raw)
            if not fps:
                raise Everything("unparseable patch")
            refiner = None if os.environ.get("BR_NO_REFINE") == "1" else refine_class_literal
            # BR_LITERAL_PROBE=1: class-literal narrowing is used only after the patched code
            # imports cleanly in a child (selector/import_probe.py). No probe means no narrowing.
            probe = None
            if probe_modules is not None and time.monotonic() - started < replica_budget:
                def probe(patched):
                    return import_probe.run(patched, src_root, probe_modules)
            res = commit_impact(fps, src_root, maps, import_refiner=refiner, import_probe=probe)
            selected = set(bits_to_numbers(res.bits, translate))
            if res.nontrivial:
                selected.update(unmapped)
            if res.replica_ok and (res.call_sites or res.import_files) and os.environ.get("BR_NO_REPLICA") != "1" \
                    and time.monotonic() - started < replica_budget:
                selected = prune_with_replicas(cid, selected, res, maps, by_id, ms, src_root)
            selected = replica.mode0_verify(session, cid, selected, res, fps, ms)
            answers[cid] = sorted(selected)
            if verbose:
                log(f"{cid}: {len(selected)} tests; " + " | ".join(res.notes[:6]))
        except Everything as reason:
            answers[cid] = every
            log(f"{cid}: every test ({reason})")
        except Exception as error:  # never let one commit sink the run
            answers[cid] = every
            log(f"{cid}: every test after error {error!r}")

    lines = []
    for path in patches:
        cid = path.stem
        try:
            workers = schedule.split(answers.get(cid, every), ms)
        except Exception as error:
            log(f"{cid}: scheduling failed {error!r}")
            workers = [every]
        lines.append(json.dumps({"commit": cid, "workers": workers}, separators=(",", ":")))
    sys.stdout.write("\n".join(lines) + ("\n" if lines else ""))
    sys.stdout.flush()
    log(f"done: {len(lines)} commits in {time.monotonic() - started:.2f}s wall, "
        f"{time.process_time():.2f}s cpu")


if __name__ == "__main__":
    main()
