"""Run one sqlglot test method on the patched code, restricted to some of its subtests.

Invoked by selector/replica.py as:
    python -I -S replica_runner.py <spec.json>
spec: {"src": pinned source root, "patched": {module: [path, base64 source]},
       "module": ..., "cls": ..., "method": ..., "restrict": {...}}
Prints PASS or FAIL on stdout. Any exception is a FAIL.
"""

import base64
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import sys
import unittest


def main():
    spec = json.load(open(sys.argv[1]))
    patched = {name: (path, base64.b64decode(src)) for name, (path, src) in spec["patched"].items()}

    class Loader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            return compile(patched[fullname][1], self.path, "exec", dont_inherit=True)

        def get_source(self, fullname):
            return importlib.util.decode_source(patched[fullname][1])

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path, target=None):
            if fullname not in patched:
                return None
            origin = patched[fullname][0]
            loader = Loader(fullname, origin)
            search = [os.path.dirname(origin)] if origin.endswith("__init__.py") else None
            return importlib.util.spec_from_file_location(fullname, origin, loader=loader,
                                                          submodule_search_locations=search)

    sys.dont_write_bytecode = True
    sys.meta_path.insert(0, Finder())
    sys.path.insert(0, spec["src"])
    os.chdir(spec["src"])
    os.environ.setdefault("SKIP_INTEGRATION", "0")

    if spec.get("kind") == "exec_fp":
        sys.path.insert(1, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from selector.exec_fingerprint import fingerprint_suite

        for suite, expected in spec["suites"]:
            fp, _, _ = fingerprint_suite(spec["src"], suite)
            if fp != expected:
                diff = [i for i, (a, b) in enumerate(zip(fp["queries"], expected["queries"])) if a != b]
                print(f"FAIL {suite['fixture']} queries {diff[:5]}")
                return
        print("PASS")
        return

    mod = importlib.import_module(spec["module"])
    restrict = spec["restrict"]
    if restrict["kind"] == "dialects":
        from sqlglot.dialects import Dialects

        setattr(mod, restrict["global"], [Dialects(v) for v in restrict["values"]])
    else:
        raise ValueError(restrict["kind"])
    case = getattr(mod, spec["cls"])(spec["method"])
    result = unittest.TestResult()
    case.run(result)
    print("PASS" if result.wasSuccessful() and result.testsRun == 1 else "FAIL")


if __name__ == "__main__":
    try:
        main()
    except BaseException as error:  # noqa: BLE001 - any failure means "cannot prove"
        print(f"FAIL {error!r}")
