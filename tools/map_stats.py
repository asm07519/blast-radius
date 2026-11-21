#!/usr/bin/env python3
"""Print the trust counts of a maps.bin (read-only, stdlib only).

    python3 tools/map_stats.py [maps/maps.bin] [--check]

Shows file size, sha256, number of test ids, how many are mapped and how many are
untrusted (with their ids). With --check, exits 1 unless every id is mapped and
none is untrusted, so it can gate a commit of freshly built maps.

Inside a built submission image:

    docker run --rm --platform linux/amd64 IMAGE python /app/tools/map_stats.py /app/maps/maps.bin
"""

import argparse
import collections
import hashlib
import marshal
import os
import sys
import zlib


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("maps", nargs="?", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                            "..", "maps", "maps.bin"))
    ap.add_argument("--check", action="store_true", help="exit 1 unless all ids are mapped and trusted")
    args = ap.parse_args()

    with open(args.maps, "rb") as fh:
        blob = fh.read()
    data = marshal.loads(zlib.decompress(blob))
    ids, mapped, bad = data["ids"], data["mapped"], data["untrusted"]
    n = len(ids)
    untrusted = [ids[i] for i in range(n) if bad >> i & 1]
    unmapped = [ids[i] for i in range(n) if not mapped >> i & 1]

    print(f"file      {os.path.abspath(args.maps)}")
    print(f"size      {len(blob)} bytes")
    print(f"sha256    {hashlib.sha256(blob).hexdigest()}")
    print(f"format    {data.get('format')}  commit {data.get('commit')}")
    print(f"ids       {n}")
    print(f"mapped    {n - len(unmapped)}")
    print(f"untrusted {len(untrusted)}")
    tp = data.get("typeprof_incomplete")
    print(f"typeprof  {'absent' if tp is None else str(bin(tp).count('1')) + ' incomplete'}")
    print(f"subtests  {sorted(data.get('subtests', {}))}")
    print(f"exec      {sorted(data.get('exec_phases', {}))}")
    if untrusted:
        per_file = collections.Counter(t.split("::")[0] for t in untrusted)
        print("untrusted by file: " + ", ".join(f"{f} {c}" for f, c in per_file.most_common()))
        for t in untrusted:
            print(f"  untrusted {t}")
    for t in unmapped:
        print(f"  unmapped  {t}")
    if args.check and (untrusted or unmapped):
        print("CHECK FAILED: maps are not fully traced", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
