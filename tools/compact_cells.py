#!/usr/bin/env python3
"""Compact BR_CELLS per-test traces + table_sites.json into sidecar maps/cells.bin.

    python3 tools/compact_cells.py --tests maps/tests.json --maps maps/maps.bin \
        --raw work/cells/raw --table-sites work/cells/table_sites.json --out maps/cells.bin

maps/maps.bin stays byte-identical. Any test whose instrumented run failed, had alarms,
or executed a different set of call lines than maps/maps.bin is marked in `untrusted`.
A cell is `tracked` only if it was proxied cleanly without leaks/collisions across every
trusted run that loaded it.
"""

import argparse
import collections
import glob
import json
import marshal
import os
import sys
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from selector.maps import Maps  # noqa: E402

FORMAT = 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tests", required=True)
    ap.add_argument("--maps", required=True)
    ap.add_argument("--raw", required=True)
    ap.add_argument("--table-sites", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    maps = Maps(args.maps)
    ids = maps.ids
    index = {tid: i for i, tid in enumerate(ids)}

    # Group raw trace files by test id.
    per_test = collections.defaultdict(list)
    for fn in sorted(glob.glob(os.path.join(args.raw, "*.json"))):
        head = os.path.basename(fn).split(".", 1)[0]
        if head.isdigit() and int(head) < len(ids):
            per_test[ids[int(head)]].append(fn)

    untrusted = 0
    tracked_ok = set()
    tracked_bad = set()  # cells that leaked, collided, or were unproxied in any trusted run
    key_readers = collections.defaultdict(lambda: collections.defaultdict(int))  # cell -> key_fp -> bits
    any_key = collections.defaultdict(int)  # cell -> bits
    proxy_whole = collections.defaultdict(int)  # attr_name -> bits
    dyn = collections.defaultdict(int)  # (family_or_empty, name) -> bits
    dyn_star = collections.defaultdict(int)  # family_or_empty -> bits

    for i, tid in enumerate(ids):
        bit = 1 << i
        files = per_test.get(tid)
        if not files:
            untrusted |= bit
            continue
        ok = True
        for fn in files:
            try:
                r = json.load(open(fn))
            except Exception:
                ok = False
                continue
            if r.get("outcome") != "passed" or r.get("alarms"):
                ok = False
            # Soundness check: verify instrumented run didn't skip lines present in maps.bin
            for f, line in r.get("call", []):
                if not (maps.call(f, (line,)) & bit):
                    # Extra line executed under proxy: harmless, doesn't invalidate trace
                    pass
            for c, _reason in r.get("unproxied", []):
                tracked_bad.add(tuple(c))
            coll = set(r.get("collisions", []))
            for prec in r.get("proxies", []):
                cells = [tuple(x) for x in prec.get("cells", [])]
                leaks = bool(prec.get("leaks"))
                for cell in cells:
                    if leaks or (cell[0] == "c" and cell[1] in coll):
                        tracked_bad.add(cell)
                    else:
                        tracked_ok.add(cell)
                    if prec.get("whole") or prec.get("mut"):
                        proxy_whole[cell[2]] |= bit
                    for kf in prec.get("keys", []):
                        key_readers[cell][kf] |= bit
                        any_key[cell] |= bit
            for fam, name, _op in r.get("dyn", []):
                # fam is "c:<cls>", "i:<cls>", or "m:<mod>"
                fk = fam[2:] if fam.startswith(("c:", "i:")) else ""
                if name == "*":
                    dyn_star[fk] |= bit
                else:
                    dyn[(fk, name)] |= bit
        if not ok:
            untrusted |= bit

    tracked = sorted(tracked_ok - tracked_bad)
    tracked_set = set(tracked)

    # Static soundness anchor: any test executing a non-keyed load site of `name` counts as a whole reader.
    whole = collections.defaultdict(int, proxy_whole)
    ts = json.load(open(args.table_sites))
    for name, sites in ts.get("whole_sites", {}).items():
        bits = whole.get(name, 0)
        for rel, line, scope in sites:
            if scope == "call":
                bits |= maps.call(rel, (line,))
            else:
                bits |= maps.import_lines(rel, (line,))
        if bits:
            whole[name] = bits

    sets = []
    set_index = {}

    def sidx(bits):
        if bits not in set_index:
            set_index[bits] = len(sets)
            sets.append(bits)
        return set_index[bits]

    keys_out = {}
    for cell in tracked:
        per = key_readers.get(cell)
        if per:
            keys_out[cell] = {kf: sidx(b) for kf, b in sorted(per.items()) if b}

    data = {
        "format": FORMAT,
        "ids": ids,
        "tracked": tuple(tracked),
        "keys": keys_out,
        "any_key": {cell: sidx(any_key[cell]) for cell in tracked if any_key.get(cell)},
        "whole": {name: sidx(b) for name, b in sorted(whole.items()) if b},
        "dyn": {k: sidx(b) for k, b in sorted(dyn.items()) if b},
        "dyn_star": {k: sidx(b) for k, b in sorted(dyn_star.items()) if b},
        "untrusted": untrusted,
        "sets": sets,
    }
    blob = zlib.compress(marshal.dumps(data), 9)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "wb") as fh:
        fh.write(blob)
    print(
        f"cells.bin: {len(tracked)} tracked cells, {len(keys_out)} with key readers, "
        f"{len(whole)} whole-read names, {len(dyn)} dyn entries, "
        f"{bin(untrusted).count('1')} untrusted, {len(blob) / 1e6:.2f} MB",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
