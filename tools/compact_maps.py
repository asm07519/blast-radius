#!/usr/bin/env python3
"""Fold per-test traces (tools/build_maps.py output) into the shipped map file.

    python3 tools/compact_maps.py --tests tests.json --raw work/raw [--raw more/raw] --out maps/maps.bin

Test sets are Python ints used as bitsets (bit i = map test index i), stored
once each and referenced by index. The file is zlib-compressed marshal data.
Later --raw directories override earlier ones for the same test, so traces
rebuilt at image build time replace provisional ones.
"""

import argparse
import collections
import glob
import json
import marshal
import os
import sys
import zlib

FORMAT = 3


def plan_records(raw_dirs, ids):
    """Which raw files to use per test: a later directory replaces all files of a test."""
    chosen = {}
    for raw in raw_dirs:
        fresh = collections.defaultdict(list)
        for fn in sorted(glob.glob(os.path.join(raw, "*.json"))):
            head = os.path.basename(fn).split(".", 1)[0]
            if head.isdigit() and int(head) < len(ids):
                fresh[ids[int(head)]].append(fn)
            else:
                try:
                    fresh[json.load(open(fn))["id"]].append(fn)
                except Exception:
                    continue
        chosen.update(fresh)
    return chosen


def untrusted_record(r):
    return r["outcome"] != "passed" or "call" in r.get("duckdb", [])


def expand(data):
    """Turn a compact maps dict back into plain bitset tables (for merging)."""
    sets = data["sets"]
    files = data["files"]
    call = {(k, line): sets[s] for k, per in data["call"].items() for line, s in per.items()}
    imp = {(k, line): sets[s] for k, per in data["import"].items() for line, s in per.items()}
    return {
        "files": list(files),
        "call": call,
        "import": imp,
        "imported": {k: sets[s] for k, s in data["imported"].items()},
        "reads": {p: sets[s] for p, s in data["reads"].items()},
        "reads_lc": {p: sets[s] for p, s in data["reads_lc"].items()},
        "listed": {p: sets[s] for p, s in data["listed"].items()},
        "mapped": data["mapped"],
        "untrusted": data["untrusted"],
        "class_users": {k: sets[s] for k, s in data.get("class_users", {}).items()},
        "classes_at_import": data.get("classes_at_import", ()),
        "call_at_import": [tuple(x) for x in data.get("call_at_import", ())],
        "names": data.get("names", {}),
        "dynamic": data.get("dynamic", []),
        "class_info": data.get("class_info", {}),
        "sets": sets,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tests", required=True)
    ap.add_argument("--raw", action="append", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--base", default=None, help="existing maps.bin; tests found in --raw replace theirs")
    ap.add_argument("--commit", default="eb11aec4be8cb2122afe713a4816662c3f0fb2fa")
    ap.add_argument("--static", default=None, help="tools/static_index.py output")
    ap.add_argument("--classes", default=None, help="tools/class_info.py output")
    args = ap.parse_args()

    tests = json.load(open(args.tests))
    ids = [t["id"] for t in tests["tests"]]
    index = {tid: i for i, tid in enumerate(ids)}
    per_test = plan_records(args.raw, ids)

    files = []
    file_index = {}
    base = None
    if args.base:
        base_data = marshal.loads(zlib.decompress(open(args.base, "rb").read()))
        if base_data["ids"] != ids:
            sys.exit("base maps were built for a different tests.json")
        base = expand(base_data)
        for f in base["files"]:
            file_index[f] = len(files)
            files.append(f)

    def fidx(path):
        if path not in file_index:
            file_index[path] = len(files)
            files.append(path)
        return file_index[path]

    call = collections.defaultdict(int)
    imp = collections.defaultdict(int)
    imported = collections.defaultdict(int)
    reads = collections.defaultdict(int)
    reads_lc = collections.defaultdict(int)
    listed = collections.defaultdict(int)
    mapped = 0
    bad = 0
    outcomes = {}
    class_users = collections.defaultdict(int)
    classes_at_import = set()
    call_at_import = set()
    if base is not None:
        for k, bits in base.get("class_users", {}).items():
            class_users[k] = bits
        classes_at_import |= set(base.get("classes_at_import", ()))
        call_at_import |= set(base.get("call_at_import", ()))
        replaced = 0
        for tid in per_test:
            if tid in index:
                replaced |= 1 << index[tid]
        keep = ~replaced
        for src, dst in ((base["call"], call), (base["import"], imp), (base["imported"], imported),
                         (base["reads"], reads), (base["reads_lc"], reads_lc), (base["listed"], listed)):
            for key, bits in src.items():
                bits &= keep
                if bits:
                    dst[key] = bits
        mapped = base["mapped"] & keep
        bad = base["untrusted"] & keep
        for k in list(class_users):
            class_users[k] &= keep
    for tid, record_files in per_test.items():
        i = index.get(tid)
        if i is None:
            continue
        bit = 1 << i
        mapped |= bit
        for fn in record_files:
            try:
                r = json.load(open(fn))
            except Exception as error:
                print(f"skip {fn}: {error}", file=sys.stderr)
                bad |= bit
                continue
            if untrusted_record(r):
                bad |= bit
            for f, line in r["call"]:
                call[(fidx(f), line)] |= bit
            seen_files = set()
            for f, line in r["import"]:
                k = fidx(f)
                imp[(k, line)] |= bit
                seen_files.add(k)
            for k in seen_files:
                imported[k] |= bit
            for path, how in r["reads"].items():
                if how == "data":
                    reads[path] |= bit
                else:
                    reads_lc[path] |= bit
            for d in r["listed"]:
                listed[d] |= bit
            for c in r.get("classes", []):
                class_users[c] |= bit
            classes_at_import.update(r.get("classes_at_import", []))
            for f, line in r.get("call_at_import", []):
                call_at_import.add((fidx(f), line))
            del r

    sets = []
    set_index = {}

    def sidx(bits):
        if bits not in set_index:
            set_index[bits] = len(sets)
            sets.append(bits)
        return set_index[bits]

    def by_file(table):
        out = collections.defaultdict(dict)
        for (k, line), bits in table.items():
            out[k][line] = sidx(bits)
        return dict(out)

    names = {}
    dynamic = []
    if args.static:
        static = json.load(open(args.static))
        bound = set(static["class_bound"])
        for name, loads in static["loads"].items():
            bits = 0
            imports, calls_at_import = set(), set()
            for f, line, scope, base_chain, owner in loads:
                k = file_index.get(f)
                if scope == "call":
                    if k is not None:
                        bits |= call.get((k, line), 0)
                        if (k, line) in call_at_import:
                            calls_at_import.add((base_chain, owner))
                elif name in bound:
                    module = f[:-3].replace("/", ".")
                    if module.endswith(".__init__"):
                        module = module[: -len(".__init__")]
                    imports.add((f"{module}:{owner}" if owner else "", base_chain))
            if bits or imports or calls_at_import:
                names[name] = (sidx(bits), tuple(sorted(imports)), tuple(sorted(calls_at_import)))
        for f, line, pattern, base_kind, encl in static["dynamic"]:
            k = file_index.get(f)
            bits = call.get((k, line), 0) | imp.get((k, line), 0) if k is not None else 0
            dynamic.append((pattern, sidx(bits), base_kind, encl))
    if base is not None and not args.static:
        # keep the base index, remapping its set indices into the new table
        names = {n: (sidx(base["sets"][r]), i, c) for n, (r, i, c) in base["names"].items()}
        dynamic = [(p, sidx(base["sets"][b]), *rest) for p, b, *rest in base["dynamic"]]
    class_info = dict(base["class_info"]) if base is not None else {}
    if args.classes:
        for key, info in json.load(open(args.classes)).items():
            class_info[key] = (bool(info["trackable"]), tuple(info["mro"]))

    data = {
        "format": FORMAT,
        "class_users": {k: sidx(v) for k, v in class_users.items()},
        "classes_at_import": tuple(sorted(classes_at_import)),
        "call_at_import": tuple(sorted(call_at_import)),
        "names": names,
        "dynamic": dynamic,
        "class_info": class_info,
        "commit": args.commit,
        "ids": ids,
        "files": files,
        "call": by_file(call),
        "import": by_file(imp),
        "imported": {k: sidx(v) for k, v in imported.items()},
        "reads": {p: sidx(v) for p, v in reads.items()},
        "reads_lc": {p: sidx(v) for p, v in reads_lc.items()},
        "listed": {p: sidx(v) for p, v in listed.items()},
        "mapped": mapped,
        "untrusted": bad,
        "sets": sets,
    }
    blob = zlib.compress(marshal.dumps(data), 9)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "wb") as fh:
        fh.write(blob)
    n_un = bin(bad).count("1")
    print(
        f"{len(per_test)} tests traced, {bin(mapped).count('1')} mapped, {n_un} untrusted, "
        f"{len(files)} files, {len(call)} call lines, {len(imp)} import lines, {len(sets)} distinct sets, "
        f"{len(blob) / 1e6:.2f} MB",
        file=sys.stderr,
    )
    if n_un:
        names = [ids[i] for i in range(len(ids)) if bad >> i & 1]
        print("untrusted: " + ", ".join(names[:12]) + (" ..." if len(names) > 12 else ""), file=sys.stderr)


if __name__ == "__main__":
    main()
