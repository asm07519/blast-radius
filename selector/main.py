"""Blast radius selector: for each patch, the tests that could fail, split over workers.

Reads /data/tests.json and /data/commits/*.patch, writes one JSON line per commit
to stdout. Logs go to stderr. Any failure on a commit selects every test for it.
"""

import json
import os
import sys
import time
from pathlib import Path

from . import schedule
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


def main(argv=None):
    started = time.monotonic()
    data = Path(os.environ.get("BR_DATA", "/data"))
    maps_path = Path(os.environ.get("BR_MAPS", APP / "maps" / "maps.bin"))
    src_root = os.environ.get("BR_SRC", str(APP / "src"))
    budget = float(os.environ.get("BR_BUDGET", "100"))
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
            res = commit_impact(fps, src_root, maps, import_refiner=refiner)
            selected = set(bits_to_numbers(res.bits, translate))
            if res.nontrivial:
                selected.update(unmapped)
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
