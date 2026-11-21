"""Decode the diagnostic-2 payload from a board's simulated seconds.

    python3 tools/diag2_decode.py 263.481 [--base 235.296]
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from selector.diag2 import BIG_CLASSES, MID_CLASSES, RADICES, decode  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("simulated", nargs="+", type=float)
    ap.add_argument("--base", type=float, default=235.296)
    args = ap.parse_args()
    for sim in args.simulated:
        payload = round((sim - args.base) * 1000)
        print(f"simulated {sim:.3f} - base {args.base:.3f} = payload {payload} ms")
        if payload <= 0:
            print("  no payload")
            continue
        fields, rest = decode(payload - 1)
        print(f"  fields {fields} (radices {RADICES}), overflow {rest}")
        print(f"  truncated: {bool(fields[0])}")
        print("  commits selecting >= 1500 tests, by class (3 means 3 or more):")
        for name, count in zip(BIG_CLASSES, fields[1:1 + len(BIG_CLASSES)]):
            print(f"    {count}  {name}")
        print("  other commits with cost >= 12 s, by class:")
        for name, count in zip(MID_CLASSES, fields[1 + len(BIG_CLASSES):]):
            print(f"    {count}  {name}")

if __name__ == "__main__":
    main()
