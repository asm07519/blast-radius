#!/usr/bin/env python3
"""Describe every class defined in the pinned sqlglot (built once, shipped in the maps).

    python3 tools/class_info.py --repo /src/sqlglot --out work/classes.json

For each class "module:qualname": its MRO keys and whether objects of it can be
seen by the tracer (some class in its MRO defines __init__ or __new__ in sqlglot
code, and it is not an Enum).
"""

import argparse
import enum
import importlib
import json
import os
import pkgutil
import sys
import types


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    repo = os.path.abspath(args.repo)
    sys.path.insert(0, repo)
    os.environ.setdefault("SKIP_INTEGRATION", "0")
    import sqlglot

    modules = [sqlglot]
    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name.endswith("__main__"):
            continue
        try:
            modules.append(importlib.import_module(info.name))
        except Exception as error:
            print(f"skip {info.name}: {error!r}", file=sys.stderr)

    def key(c):
        return f"{c.__module__}:{c.__qualname__}"

    def trackable(c):
        if issubclass(c, enum.Enum):
            return False
        for k in c.__mro__:
            if k is object or not getattr(k, "__module__", "").startswith("sqlglot"):
                continue
            for name in ("__init__", "__new__"):
                fn = vars(k).get(name)
                if isinstance(fn, staticmethod):
                    fn = fn.__func__
                if isinstance(fn, types.FunctionType):
                    return True
        return False

    out = {}
    seen = set()

    def visit(c):
        if c in seen or not c.__module__.startswith("sqlglot"):
            return
        seen.add(c)
        out[key(c)] = {
            "mro": [key(k) for k in c.__mro__ if k is not object],
            "trackable": trackable(c),
            "enum": issubclass(c, enum.Enum),
        }
        for v in list(vars(c).values()):
            if isinstance(v, type) and v.__qualname__.startswith(c.__qualname__ + "."):
                visit(v)

    for m in modules:
        for v in list(vars(m).values()):
            if isinstance(v, type) and v.__module__ == m.__name__:
                visit(v)
    with open(args.out, "w") as fh:
        json.dump(out, fh)
    print(f"{len(out)} classes, {sum(v['trackable'] for v in out.values())} trackable", file=sys.stderr)


if __name__ == "__main__":
    main()
