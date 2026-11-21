"""Drop an expensive test by re-running only the subtests a change can reach.

Some slow tests loop over many independent subtests. The maps record which lines
each subtest window runs (tools/subtest_cov.py). When every changed line of a commit
is a call-time line that only runs inside a few windows (never outside one), the
test's outcome can only change in those windows. Running exactly those subtests with
the patched code costs well under a second; if they pass, the test cannot fail.
"""

import base64
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

# Tests we know how to restrict. "global" is the name the test loops over.
REPLICAS = {
    "tests/test_generator.py::TestGenerator::test_generator_memory": {
        "module": "tests.test_generator",
        "cls": "TestGenerator",
        "method": "test_generator_memory",
        "restrict": {"kind": "dialects", "global": "Dialects"},
        "param": "dialect",
    },
}


def affected_windows(info, call_sites, import_files=()):
    """Subtest parameter values whose windows run a changed line, or None if any changed
    line also runs outside every window (then the whole test is exposed).

    A module changed at import time affects the window that first loads it and every
    later window (they may use its classes without running its lines); it must not be
    loaded before the subtest loop.
    """
    outside = info["outside"]
    values = set()
    for path in import_files:
        if outside.get(path):
            return None
        first = next((i for i, (_, files) in enumerate(info["windows"]) if files.get(path)), None)
        if first is None:
            return None
        values.update(value for value, _ in info["windows"][first:])
    for path, lines in call_sites:
        out = outside.get(path)
        if out and any(line in out for line in lines):
            return None
        for value, files in info["windows"]:
            w = files.get(path)
            if w and any(line in w for line in lines):
                values.add(value)
    return values


def module_name(path):
    name = path[:-3].replace("/", ".")
    return name[: -len(".__init__")] if name.endswith(".__init__") else name


def run(test_id, values, patched_files, src_root, timeout=8.0):
    """True if the restricted test passes on the patched code."""
    cfg = REPLICAS[test_id]
    spec = {
        "src": src_root,
        "patched": {module_name(p): [os.path.join(src_root, p), base64.b64encode(src).decode()]
                    for p, src in patched_files.items()},
        "module": cfg["module"],
        "cls": cfg["cls"],
        "method": cfg["method"],
        "restrict": dict(cfg["restrict"], values=sorted(values)),
    }
    fd, path = tempfile.mkstemp(prefix="br-replica-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(spec, fh)
        proc = subprocess.run([sys.executable, "-I", "-S", os.path.join(HERE, "replica_runner.py"), path],
                              capture_output=True, timeout=timeout)
        out = proc.stdout.decode(errors="replace").strip().splitlines()
        return proc.returncode == 0 and bool(out) and out[-1] == "PASS"
    except Exception:
        return False
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def exec_eligible(info, call_sites):
    """True when no changed line runs in the exposed phases of an executor test."""
    exposed = info["exposed"]
    for path, lines in call_sites:
        e = exposed.get(path)
        if e and any(line in e for line in lines):
            return False
    return True


def run_exec(infos, patched_files, src_root, timeout=12.0):
    """True if the cheap-phase fingerprints of these executor tests are unchanged on the patched code."""
    spec = {
        "kind": "exec_fp",
        "src": src_root,
        "patched": {module_name(p): [os.path.join(src_root, p), base64.b64encode(src).decode()]
                    for p, src in patched_files.items()},
        "suites": [[info["suite"], info["fingerprint"]] for info in infos],
    }
    fd, path = tempfile.mkstemp(prefix="br-replica-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(spec, fh)
        env = dict(os.environ, PYTHONHASHSEED="0")
        proc = subprocess.run([sys.executable, "-I", "-S", os.path.join(HERE, "replica_runner.py"), path],
                              capture_output=True, timeout=timeout, env=env)
        out = proc.stdout.decode(errors="replace").strip().splitlines()
        return proc.returncode == 0 and bool(out) and out[-1] == "PASS"
    except Exception:
        return False
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
