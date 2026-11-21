"""Run one sqlglot test method on the patched code, restricted to some of its subtests.

Invoked by selector/replica.py as:
    python -I -S replica_runner.py <spec.json>
spec: {"src": pinned source root, "patched": {module: [path, base64 source]},
       "module": ..., "cls": ..., "method": ..., "restrict": {...}}
Prints PASS or FAIL on stdout. Any exception is a FAIL.

A spec with "kind": "mode0" runs whole tests instead (selector/mode0_runner.py documents
that spec and its result file); nothing is printed and the exit code matters.

multiprocessing spawn/forkserver children re-import this file as __mp_main__; under an
fs-overlay Mode 0 runner (env BR_MODE0_FS) they install the runner's audit hook first.
"""

import base64
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import sys
import unittest


def install_overlay(src, patched_spec):
    """Serve the patched modules from memory, then make `src` importable and the cwd.

    patched_spec: {module: [path, base64 source]}. Returns the finder (sys.meta_path[0]).
    """
    patched = {name: (path, base64.b64decode(source)) for name, (path, source) in patched_spec.items()}

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

    finder = Finder()
    sys.dont_write_bytecode = True
    sys.meta_path.insert(0, finder)
    sys.path.insert(0, src)
    os.chdir(src)
    os.environ.setdefault("SKIP_INTEGRATION", "0")
    return finder


def _load_mode0_runner():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mode0_runner.py")
    mspec = importlib.util.spec_from_file_location("_br_mode0_runner", path)
    module = importlib.util.module_from_spec(mspec)
    mspec.loader.exec_module(module)
    return module


def run_mode0(spec):
    """Never returns: the exit code is the protocol (0 = the result file is complete)."""
    code = 70
    try:
        module = _load_mode0_runner()
        code = module.run(spec, install_overlay)
    except BaseException:  # noqa: BLE001 - the result file then has no complete done line
        code = 70
    finally:
        os._exit(code)


def mp_child():
    """This file re-imported as __mp_main__ by a multiprocessing spawn/forkserver child.

    A Mode 0 runner in fs-overlay mode exports BR_MODE0_FS; its children audit themselves
    (mode0_runner.child_init) before any task is unpickled, or die (exit 86) trying."""
    env_value = os.environ.get("BR_MODE0_FS")
    if not env_value:
        return
    try:
        _load_mode0_runner().child_init(env_value)
    except BaseException:  # noqa: BLE001 - an unaudited child must not run tasks
        os._exit(86)


def main():
    spec = json.load(open(sys.argv[1]))
    if spec.get("kind") == "mode0":
        run_mode0(spec)

    install_overlay(spec["src"], spec["patched"])

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


if __name__ == "__mp_main__":
    mp_child()

if __name__ == "__main__":
    try:
        main()
    except BaseException as error:  # noqa: BLE001 - any failure means "cannot prove"
        print(f"FAIL {error!r}")
