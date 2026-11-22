#!/usr/bin/env python3
"""Build the import-state baseline that selector/state_probe.run compares patched code with.

    python -I -S tools/build_state.py --repo /app/src --out /app/maps/state.bin [--maps /app/maps/maps.bin]

Runs the state probe's child on the unpatched tree `--runs` times (default 5; every child has
its own random hash seed), records which cells are not reproducible across seeds, and writes
zlib-compressed marshal (hashes, per-key value hashes, no values). Stdlib only, so it runs in
the entry image at build time. The module list is the probe's (import_probe.modules_from_maps
of the shipped maps), so the baseline matches what selector/main.py will pass at run time.
The baseline is tied to the Python that built it (state_probe.run refuses another one).
"""

import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from selector import import_probe, state_probe  # noqa: E402
from selector.maps import Maps  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="pinned sqlglot tree (absolute)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--maps", default=os.path.join(ROOT, "maps", "maps.bin"))
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--timeout", type=float, default=120.0)
    args = ap.parse_args()
    started = time.time()
    cpu = time.process_time()
    modules = import_probe.modules_from_maps(Maps(args.maps))
    base = state_probe.build_baseline(os.path.abspath(args.repo), modules, runs=args.runs, timeout=args.timeout)
    size = state_probe.write_baseline(base, args.out)
    stats = base.get("stats", {})
    print(f"state baseline: {len(base['cells'])} cells, {len(base['unstable'])} unstable, "
          f"{len(base['unstable_order'])} order-unstable, {len(base['opaque'])} opaque, "
          f"{len(base['warm'])} warm caches, {size} bytes, child cpu {stats.get('total_cpu', 0):.2f}s, "
          f"wall {time.time() - started:.1f}s for {args.runs} runs", file=sys.stderr)
    del cpu


if __name__ == "__main__":
    main()
