"""Decode the diagnostic payload from a board's simulated seconds.

    python3 tools/diag_decode.py 263.481 [more runs...] [--base 237.447]

payload ms = round((simulated - base) * 1000); V = payload - 1; fields are the mixed
radix digits of V (see selector/diag.py).
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from selector.diag import (C_STATES, CODES, MAXV, NB, R_STATES, RADICES, T_STATES, WALL_EDGES,  # noqa: E402
                           bucket_range, decode, r_unstate)

U_TEXT = ["0 (all tests trusted: finish_maps retraced everything at build)",
          "1-36 (finish_maps retraced only part of the untrusted tests)",
          "37+ (maps as committed: finish_maps did not retrace, or maps unreadable)"]


def fmt_replica(r):
    code, b = r_unstate(r)
    s = f"code {code} = {CODES[code].split(' (')[0]}"
    if b is not None:
        lo, hi = bucket_range(b)
        s += f", wall in [{lo}, {hi}) s" if hi is not None else f", wall >= {lo} s"
    return s


def one(sim, base):
    payload = round((sim - base) * 1000)
    print(f"simulated {sim:.3f} - base {base:.3f} = payload {payload} ms")
    if payload == 0:
        print("  payload 0: diag code failed before placing markers, or placement impossible")
        return
    if payload < 0 or payload - 1 > MAXV:
        print(f"  out of range (valid 1..{MAXV + 1}): base changed (a real replica behaved differently "
              "from 9019a56?) or decoding scheme mismatch")
        return
    fields, _ = decode(payload - 1)
    print(f"  digits {fields} (radices {RADICES})")
    print(f"  1. synthetic executor replica (o07084co, run_exec, timeout 45 s): {fmt_replica(fields[0])}")
    print(f"  2. synthetic generator_memory replica (o07026co, 4 dialects, timeout 45 s): {fmt_replica(fields[1])}")
    l, d, b = C_STATES[fields[2]]
    cap = lambda x: f"{x}+" if x == 2 and l == 2 else str(x)  # noqa: E731
    print(f"  3. real commits: replica launches {cap(l)}, dropped (PASS) {cap(d)}, "
          f"bad (FAIL_EXC/BADEXIT/TIMEOUT/LAUNCH_ERR) {cap(b)}  [counts saturate at 2; "
          f"launches - dropped - bad = plain FAIL_MISMATCH]")
    u, t = divmod(fields[3], T_STATES)
    print(f"  4a. untrusted tests in the maps the selector used: {U_TEXT[u]}")
    print(f"  4b. commits whose final answer has test_execute_tpcds: {t}{'+' if t == T_STATES - 1 else ''}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("simulated", nargs="+", type=float)
    ap.add_argument("--base", type=float, default=237.447)
    args = ap.parse_args()
    for sim in args.simulated:
        one(sim, args.base)
        print()
    print("Outcome codes:")
    for code, text in CODES.items():
        print(f"  {code}: {text}")
    print(f"Wall buckets (s): edges {WALL_EDGES} ({NB} buckets; codes 0-3 only). "
          f"{R_STATES} states per replica, max payload {MAXV + 1} ms.")


if __name__ == "__main__":
    main()
