"""Decode the diagnostic-3 payload from a board's simulated seconds.

    python3 tools/diag3_decode.py A 1234.567 [--base 235.296]
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from selector.diag3 import decode, digit_text  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("variant", choices=["A", "B"])
    ap.add_argument("simulated", type=float)
    ap.add_argument("--base", type=float, default=235.296)
    args = ap.parse_args()
    payload = round((args.simulated - args.base) * 1000)
    print(f"simulated {args.simulated:.3f} - base {args.base:.3f} = payload {payload} ms")
    fields, rest = decode(payload - 1)
    print(f"  digits {fields}, overflow {rest}")
    for i, d in enumerate(fields, 1):
        print(f"  commit #{i} by cost: {digit_text(d, args.variant)}")


if __name__ == "__main__":
    main()
