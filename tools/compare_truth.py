#!/usr/bin/env python3
"""Compare two label files (e.g. sample/answers.json vs our ground_truth.py output).

    python3 tools/compare_truth.py sample/answers.json work/truth-sample.json

Tests our run could not judge (listed under "unknown") are ignored.
"""

import json
import sys


def main():
    ref = json.load(open(sys.argv[1]))["commits"]
    ours = json.load(open(sys.argv[2]))["commits"]
    same = diff = 0
    for cid in sorted(ref):
        if cid not in ours:
            print(f"{cid}: not labelled")
            continue
        unknown = set(ours[cid].get("unknown", []))
        a = set(ref[cid]["failing"]) - unknown
        b = set(ours[cid]["failing"]) - unknown
        if a == b:
            same += 1
        else:
            diff += 1
            print(f"{cid}: reference only {sorted(a - b)[:10]}  ours only {sorted(b - a)[:10]}")
    print(f"{same} commits agree, {diff} differ")


if __name__ == "__main__":
    main()
