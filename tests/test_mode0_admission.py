"""Mode 0 admission (selector/mode0.py): contract, eligibility file, EV gate, budgets, results.

Unit tests use fake eligibility dicts, fake impact results and an injected execute(); the
end-to-end tests run the selector (`python -I -S run.py`) on a synthetic mini repository with a
hand-built maps file, exactly as the image runs it.

    python3 -m unittest discover -s tests -t .
"""

import base64
import contextlib
import hashlib
import importlib.machinery
import io
import json
import marshal
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zlib
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from selector import mode0, schedule  # noqa: E402
from selector import mode0_runner as m0  # noqa: E402
from selector.impact import Result  # noqa: E402
from selector.patch import FilePatch, parse  # noqa: E402

PY = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
RUNS = [{"name": "iso"}, {"name": "seed0"}, {"name": "seed1"}]


def write_tree(root, files):
    for rel, text in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(textwrap.dedent(text).lstrip("\n"))


def engine_info(cpu=0.01, proc=0.1, flags=(), outs=None, ok=True, why=None):
    return {"o": outs or {"iso": "passed", "seed0": "passed", "seed1": "passed"},
            "cpu": cpu, "proc": proc, "flags": list(flags), "ok": ok, "why": why}


def entry(n, s=0.01, cpu=0.01, proc=0.1, **kw):
    e = {"n": n, "s": s, "unittest": engine_info(cpu, proc), "pytest": engine_info(cpu, proc + 0.2),
         "batch": {"unittest": "passed", "pytest": "passed"}}
    e.update(kw)
    return e


def elig_file(tests, src, entries=None, **header):
    h = {"python": PY, "implementation": sys.implementation.name, "machine": os.uname().machine,
         "engines": {"unittest": {"python": PY}, "pytest": {"python": PY, "pytest": "9"}},
         "source_digest": m0.source_digest(src), "tests_identity": m0.tests_identity(tests),
         "runner_digest": m0.runner_digest(), "runs": RUNS, "seeds": [0, 1], "also_batch": True}
    h.update(header)
    if entries is None:
        entries = {t["id"]: entry(t["number"], t["seconds"]) for t in tests}
    return {"schema": mode0.SCHEMA, "header": h, "summary": {}, "tests": entries}


def catalog(n, seconds=0.01):
    return [{"number": i, "id": f"tests/test_x.py::TestX::test_{i}", "file": "tests/test_x.py",
             "seconds": seconds} for i in range(n)]


def config(**over):
    env = {mode0.TUNABLES[k][0]: str(v) for k, v in over.items() if k in mode0.TUNABLES}
    env.update({k: v for k, v in over.items() if k.startswith("BR_")})
    return mode0.Config(env)


class TmpDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="br-m0a-")
        self.addCleanup(shutil.rmtree, self.tmp, True)


# ---------------------------------------------------------------------------- contract

SRC_FILES = {
    "sqlglot/__init__.py": "",
    "sqlglot/a.py": "def f():\n    return 1\n",
    "sqlglot/sub/__init__.py": "",
    "sqlglot/sub/b.py": "X = 1\n",
    "sqlglot/nopkg/c.py": "X = 1\n",
    "sqlglot/shadow.py": "X = 1\n",
    "sqlglot/shadow/__init__.py": "",
    "sqlglot/ext.py": "X = 1\n",
    "tests/__init__.py": "",
    "tests/test_x.py": "import unittest\n",
}


def fp(old, new=None, mode_only=False, binary=False, copy=False):
    f = FilePatch()
    f.old_path, f.new_path = old, (old if new is None else new)
    f.mode_only, f.binary = mode_only, binary
    if copy:
        f.copy = True
    return f


def result(paths, call=True, imports=False, ok=True, new=b"X = 2\n"):
    r = Result()
    r.replica_ok = ok
    for p in paths:
        r.patched[p] = new
    if call:
        r.call_sites.append((paths[0] if paths else "sqlglot/a.py", {2}))
    if imports:
        r.import_files.add(paths[0])
    return r


class ContractTest(TmpDir):
    def setUp(self):
        super().setUp()
        write_tree(self.tmp, SRC_FILES)
        with open(os.path.join(self.tmp, "sqlglot", "ext" + importlib.machinery.EXTENSION_SUFFIXES[0]), "wb"):
            pass

    def check(self, fps, res):
        return mode0.contract(fps, res, self.tmp)

    def test_admitted(self):
        self.assertIsNone(self.check([fp("sqlglot/a.py")], result(["sqlglot/a.py"])))
        self.assertIsNone(self.check([fp("sqlglot/sub/b.py")], result(["sqlglot/sub/b.py"], call=False, imports=True)))
        two = ["sqlglot/a.py", "sqlglot/sub/b.py"]
        self.assertIsNone(self.check([fp(p) for p in two], result(two)))

    def test_static_preconditions(self):
        self.assertIsNotNone(self.check([fp("sqlglot/a.py")], result(["sqlglot/a.py"], ok=False)))
        self.assertIsNotNone(self.check([fp("sqlglot/a.py")], result([])))
        self.assertIsNotNone(self.check([fp("sqlglot/a.py")], result(["sqlglot/a.py"], call=False)))

    def test_entry_shapes(self):
        res = result(["sqlglot/a.py"])
        for entries in ([fp("sqlglot/a.py"), fp("sqlglot/x.py", mode_only=True)],
                        [fp("sqlglot/a.py", binary=True)],
                        [fp("sqlglot/a.py", copy=True)],
                        [fp("sqlglot/a.py"), added("sqlglot/new.py")],
                        [fp("sqlglot/a.py"), deleted("sqlglot/sub/b.py")],
                        [fp("sqlglot/a.py", "sqlglot/a2.py")],
                        [fp("sqlglot/a.py"), fp("sqlglot/a.py")],
                        [fp("sqlglot/a.py"), fp("sqlglot/sub/b.py")]):  # b.py not in patched
            with self.subTest(entries=entries):
                self.assertIsNotNone(self.check(entries, res))

    def test_module_shapes(self):
        for path in ("sqlglot/__init__.py", "sqlglot/sub/__init__.py", "sqlglot/nopkg/c.py",
                     "sqlglot/shadow.py", "sqlglot/ext.py", "sqlglot/missing.py", "tests/test_x.py",
                     "setup.py", "sqlglot/bad-name.py"):
            with self.subTest(path=path):
                self.assertIsNotNone(self.check([fp(path)], result([path])))

    def test_symlink(self):
        link = os.path.join(self.tmp, "sqlglot", "link.py")
        os.symlink(os.path.join(self.tmp, "sqlglot", "a.py"), link)
        self.assertIsNotNone(self.check([fp("sqlglot/link.py")], result(["sqlglot/link.py"])))


def deleted(path):
    f = FilePatch()
    f.old_path, f.new_path = path, None
    return f


def added(path):
    f = FilePatch()
    f.old_path, f.new_path = None, path
    return f


class ImpactContractTest(unittest.TestCase):
    """The contract on real sample patches (needs the pinned checkout and the maps)."""

    SRC = os.environ.get("BR_SRC", "")

    @unittest.skipUnless(os.path.isdir(os.path.join(os.environ.get("BR_SRC", "/nonexistent"), "sqlglot")),
                         "set BR_SRC to the pinned sqlglot checkout")
    def test_sample(self):
        from selector.impact import commit_impact
        from selector.maps import Maps

        maps = Maps(os.path.join(ROOT, "maps", "maps.bin"))
        want = {"fryt": True, "ktz3": True, "gbav": True, "2qn5": True,  # call/import edits
                "2vdj": False, "6vvp": False, "986c": False,  # fixture / multi with fixture
                "2haz": False, "9u5k": False}  # proven no-op swap; comment only
        for cid, ok in want.items():
            with self.subTest(cid=cid):
                with open(os.path.join(ROOT, "sample", "commits", cid + ".patch"), "rb") as fh:
                    fps = parse(fh.read())
                res = commit_impact(fps, self.SRC, maps)
                self.assertEqual(mode0.contract(fps, res, self.SRC) is None, ok)


# ------------------------------------------------------------------------- eligibility

class EligibilityTest(TmpDir):
    def setUp(self):
        super().setUp()
        self.src = os.path.join(self.tmp, "src")
        write_tree(self.src, {"sqlglot/__init__.py": "", "tests/__init__.py": "", "tests/test_x.py": ""})
        self.tests = catalog(4)
        self.path = os.path.join(self.tmp, "mode0.json")

    def save(self, data, raw=None):
        with open(self.path, "w") as fh:
            fh.write(raw if raw is not None else json.dumps(data))

    def load(self, engine="unittest", python=None):
        return mode0.load_eligibility(self.path, self.tests, self.src, engine, python=python)

    def test_valid(self):
        self.save(elig_file(self.tests, self.src))
        got = self.load()
        self.assertEqual(sorted(got), sorted(t["id"] for t in self.tests))
        self.assertEqual(got[self.tests[0]["id"]], (0.01, 0.1))
        self.assertEqual(len(self.load("pytest")), 4)

    def refused(self, data=None, raw=None, **kw):
        self.save(data, raw)
        with self.assertRaises(mode0.Refused):
            self.load(**kw)

    def test_refusals(self):
        good = elig_file(self.tests, self.src)
        self.refused(dict(good, schema="br-mode0-eligibility/2"))
        for key, value in (("runner_digest", "0" * 64), ("source_digest", "0" * 64),
                           ("tests_identity", "0" * 64), ("python", "2.7.18"), ("python", None),
                           ("python", "garbage"), ("implementation", "pypy"), ("machine", "vax"),
                           ("also_batch", False),
                           ("runs", [{"name": "iso"}]), ("runs", "iso"),
                           ("engines", {"unittest": {}})):
            with self.subTest(key=key, value=value):
                self.refused(dict(good, header=dict(good["header"], **{key: value})))
        self.refused(raw="{not json")
        self.refused(raw='{"schema": NaN}')
        self.refused(raw="[]")
        self.refused(dict(good, header=[]))
        self.refused(dict(good, tests=[]))
        os.remove(self.path)
        with self.assertRaises(mode0.Refused):
            self.load()

    def test_python_minor_mismatch(self):
        self.save(elig_file(self.tests, self.src))
        with self.assertRaises(mode0.Refused):
            self.load(python=(sys.version_info[0], sys.version_info[1] + 1))

    def test_identity_follows_current_tests(self):
        self.save(elig_file(self.tests, self.src))
        self.tests = self.tests[:3] + [dict(self.tests[3], id="tests/test_x.py::TestX::test_other")]
        with self.assertRaises(mode0.Refused):
            self.load()

    def test_source_change_refuses(self):
        self.save(elig_file(self.tests, self.src))
        with open(os.path.join(self.src, "sqlglot", "__init__.py"), "w") as fh:
            fh.write("# changed\n")
        with self.assertRaises(mode0.Refused):
            self.load()

    def test_pytest_only_certification(self):
        good = elig_file(self.tests, self.src)
        only_pytest = dict(good, header=dict(good["header"], engines={"pytest": {}}))
        self.save(only_pytest)
        self.assertEqual(len(self.load("pytest")), 4)
        with self.assertRaises(mode0.Refused):
            self.load("unittest")

    def test_per_test_rules(self):
        ids = [t["id"] for t in self.tests]

        def eligible(e, engine="unittest"):
            entries = {ids[0]: e}
            self.save(elig_file(self.tests, self.src, entries=entries))
            return ids[0] in self.load(engine)

        self.assertTrue(eligible(entry(0)))
        self.assertTrue(eligible(dict(entry(0), unittest=engine_info(flags=["rtracker"]))))
        bad = {
            "pytest disagrees": dict(entry(0), pytest=engine_info(ok=False, why="failed (iso)",
                                                                  outs={"iso": "failed"})),
            "spawn": dict(entry(0), unittest=engine_info(flags=["spawn"])),
            "read_patched": dict(entry(0), pytest=engine_info(flags=["read_patched"])),
            "unknown flag": dict(entry(0), unittest=engine_info(flags=["xfail"])),
            "seed run failed": dict(entry(0), unittest=engine_info(
                outs={"iso": "passed", "seed0": "passed", "seed1": "failed"})),
            "seed run missing": dict(entry(0), unittest=engine_info(outs={"iso": "passed"})),
            "order sensitive": dict(entry(0), order_sensitive=True),
            "disagree": dict(entry(0), disagree=True),
            "batch failed": dict(entry(0), batch={"unittest": "passed", "pytest": "failed"}),
            "no batch": {k: v for k, v in entry(0).items() if k != "batch"},
            "ok false": dict(entry(0), unittest=engine_info(ok=False, why="x")),
            "no cpu": dict(entry(0), unittest=dict(engine_info(), cpu=None)),
            "negative proc": dict(entry(0), unittest=dict(engine_info(), proc=-1)),
            "not a dict": "passed",
        }
        for why, e in bad.items():
            with self.subTest(why=why):
                self.assertFalse(eligible(e))
        self.assertFalse(eligible(dict(entry(0), unittest=engine_info(flags=["spawn"])), "pytest"))

    def test_mapped_by_id_not_number(self):
        ids = [t["id"] for t in self.tests]
        # Entry numbers disagree with the catalog; only ids matter.
        entries = {ids[2]: entry(0, cpu=0.3), ids[0]: entry(2, cpu=0.1),
                   "tests/test_x.py::TestX::test_gone": entry(1)}
        self.save(elig_file(self.tests, self.src, entries=entries))
        got = self.load()
        self.assertEqual(set(got), {ids[0], ids[2]})
        self.assertEqual(got[ids[2]][0], 0.3)
        self.assertEqual(got[ids[0]][0], 0.1)


# -------------------------------------------------------------------------------- gate

class GateTest(unittest.TestCase):
    def test_arithmetic_and_boundaries(self):
        ms = {0: 1000}
        info = [(0.25, 0.5)]  # startup 0.25 + cpu 0.25 = 0.5; factor 1 -> 0.5
        # delta = cost([[0]]) - 0 = 3000 ms; benefit = 0.5 * 3 = 1.5; net 1.0; ratio 3.0
        d = mode0.gate({0}, [0], ms, info, config(cpu_factor=1, min_gain=1.0, ratio=3.0))
        self.assertTrue(d.admit, d.why)
        self.assertEqual(d.delta, 3000)
        self.assertAlmostEqual(d.cpu, 0.5)
        self.assertAlmostEqual(d.benefit, 1.5)
        self.assertFalse(mode0.gate({0}, [0], ms, info, config(cpu_factor=1, min_gain=1.01, ratio=3.0)).admit)
        self.assertFalse(mode0.gate({0}, [0], ms, info, config(cpu_factor=1, min_gain=1.0, ratio=3.01)).admit)
        # The safety factor multiplies the whole estimate.
        d = mode0.gate({0}, [0], ms, info, config(cpu_factor=2, min_gain=0.5, ratio=1.5))
        self.assertAlmostEqual(d.cpu, 1.0)
        self.assertTrue(d.admit)  # 1.5 - 1.0 = 0.5 >= 0.5 and 1.5 >= 1.5 * 1.0
        self.assertFalse(mode0.gate({0}, [0], ms, info, config(cpu_factor=2.01)).admit)
        # P scales the benefit.
        self.assertFalse(mode0.gate({0}, [0], ms, info, config(cpu_factor=1, p=0.3)).admit)
        self.assertTrue(mode0.gate({0}, [0], ms, info, config(cpu_factor=1, p=1.0)).admit)

    def test_startup_is_max_over_r(self):
        ms = {0: 100, 1: 100, 2: 100}
        info = [(0.1, 0.2), (0.05, 0.4), (0.02, 0.1)]  # proc - cpu: 0.1, 0.35, 0.08
        d = mode0.gate({0, 1, 2}, [0, 1, 2], ms, info, config(cpu_factor=1))
        self.assertAlmostEqual(d.cpu, 0.35 + 0.17)

    def test_count_and_seconds_thresholds(self):
        ms = {i: 10 for i in range(41)}
        info = [(0.0, 0.0)] * 41
        self.assertTrue(mode0.gate(set(range(40)), list(range(40)), ms, info[:40], config()).admit)
        d = mode0.gate(set(range(41)), list(range(41)), ms, info, config())
        self.assertFalse(d.admit)
        self.assertEqual(d.why, "too many tests")
        self.assertTrue(mode0.gate(set(range(41)), list(range(41)), ms, info, config(max_tests=41)).admit)
        ms = {0: 1000, 1: 500, 2: 1}
        self.assertTrue(mode0.gate({0, 1}, [0, 1], ms, info[:2], config()).admit)  # exactly 1.5 s
        d = mode0.gate({0, 1, 2}, [0, 1, 2], ms, info[:3], config())
        self.assertFalse(d.admit)
        self.assertEqual(d.why, "too many recorded seconds")

    def test_partial_eligibility_shrinks_delta(self):
        ms = {0: 3000, 1: 100, 2: 100}
        full = schedule.cost(schedule.split({0, 1, 2}, ms), ms)
        rest = schedule.cost(schedule.split({0}, ms), ms)
        d = mode0.gate({0, 1, 2}, [1, 2], ms, [(0.01, 0.1)] * 2, config())
        self.assertEqual(d.delta, full - rest)
        self.assertEqual(d.delta, 200)
        self.assertFalse(d.admit)

    def test_empty(self):
        self.assertFalse(mode0.gate({0}, [], {0: 1}, [], config()).admit)

    def test_config_validation(self):
        for var, value in (("BR_MODE0_P", "1.5"), ("BR_MODE0_P", "nan"), ("BR_MODE0_MAX_TESTS", "x"),
                           ("BR_MODE0_CPU_FACTOR", "-1"), ("BR_MODE0_ENGINE", "nose")):
            with self.subTest(var=var, value=value):
                with self.assertRaises(ValueError):
                    mode0.Config({var: value})
        cfg = mode0.Config({})
        self.assertEqual((cfg.max_tests, cfg.max_seconds, cfg.cpu_factor, cfg.p, cfg.min_gain, cfg.ratio,
                          cfg.cpu_budget, cfg.engine), (40, 1.5, 1.5, 0.5, 0.5, 1.5, 30.0, "unittest"))
        self.assertEqual(cfg.cap(0.0), 2.0)
        self.assertEqual(cfg.cap(0.5), 6.0)


# ----------------------------------------------------------------------------- session

class FakeExecute:
    def __init__(self, outcomes=None, rc=0, timed_out=False, cpu=0.2, flags=(), raises=None, ret=None):
        self.outcomes, self.rc, self.timed_out, self.cpu = outcomes or {}, rc, timed_out, cpu
        self.flags, self.raises, self.ret = list(flags), raises, ret
        self.calls = []
        self.kwargs = []  # keyword arguments of each call (hash_seed, cpu_count)
        self.during = None  # optional callable(spec, kwargs) run inside each call

    def __call__(self, spec, timeout, **kwargs):
        self.calls.append((spec, timeout))
        self.kwargs.append(kwargs)
        if self.during:
            self.during(spec, kwargs)
        if self.raises:
            raise self.raises
        if self.ret is not None:
            return self.ret
        ids = [t["id"] for t in spec["tests"]]
        outcomes = {i: self.outcome(i, kwargs) for i in ids}
        lines = {i: {"id": i, "outcome": o, "flags": self.flags} for i, o in outcomes.items()
                 if o in m0.OUTCOMES}
        return {"outcomes": outcomes, "returncode": self.rc, "cpu": self.cpu, "wall": 0.3,
                "timed_out": self.timed_out, "done": {"done": True}, "lines": lines}

    def outcome(self, tid, kwargs):
        o = self.outcomes.get(tid, "passed")
        if isinstance(o, dict):  # per hash seed: {seed: outcome}
            o = o.get(kwargs.get("hash_seed"), "passed")
        return o


# sqlglot/a.py of the session tests: "def f():\n    return 1\n" -> "...return 2\n", exactly.
A_PATCH = b"""diff --git a/sqlglot/a.py b/sqlglot/a.py
--- a/sqlglot/a.py
+++ b/sqlglot/a.py
@@ -1,2 +1,2 @@
 def f():
-    return 1
+    return 2
"""
A_NEW = b"def f():\n    return 2\n"


def a_result(**kw):
    return result(["sqlglot/a.py"], new=A_NEW, **kw)


def a_fps():
    return parse(A_PATCH)


class SessionBase(TmpDir):
    def setUp(self):
        super().setUp()
        self.src = os.path.join(self.tmp, "src")
        write_tree(self.src, {"sqlglot/__init__.py": "", "sqlglot/a.py": "def f():\n    return 1\n",
                              "tests/__init__.py": "", "tests/test_x.py": ""})
        self.site = os.path.join(self.tmp, "site")
        os.makedirs(self.site)
        self.tests = catalog(6, seconds=0.05)
        self.ms = {t["number"]: round(t["seconds"] * 1000) for t in self.tests}
        self.elig = os.path.join(self.tmp, "mode0.json")
        with open(self.elig, "w") as fh:
            json.dump(elig_file(self.tests, self.src), fh)
        self.logs = []
        self.now = 100.0

    def session(self, execute, tests=None, started=None, **over):
        env = {"BR_MODE0_ELIG": self.elig, "BR_MODE0_SITE": self.site}
        env.update({mode0.TUNABLES[k][0]: str(v) for k, v in over.items()})
        return mode0.Session(mode0.Config(env), tests or self.tests, self.src,
                             started if started is not None else self.now, 80.0, 140.0, self.logs.append,
                             execute=execute, clock=lambda: self.now)

    def verify(self, s, selected, res=None, fps=None):
        res = res or a_result()
        return s.verify("c1", set(selected), res, fps or a_fps(), self.ms)

    def ids(self, nums):
        return [self.tests[n]["id"] for n in nums]


class SessionTest(SessionBase):
    def test_all_pass_dropped(self):
        ex = FakeExecute()
        s = self.session(ex)
        self.assertEqual(self.verify(s, {0, 1, 2}), set())
        spec, timeout = ex.calls[0]
        self.assertEqual(spec["kind"], "mode0")
        self.assertEqual(spec["engine"], "unittest")
        self.assertEqual(spec["site"], [os.path.realpath(self.site)])
        self.assertEqual(list(spec["patched"]), ["sqlglot.a"])
        self.assertEqual(spec["patched"]["sqlglot.a"][0], os.path.join(os.path.realpath(self.src), "sqlglot/a.py"))
        self.assertEqual([t["cap"] for t in spec["tests"]], [2.0] * 3)
        self.assertAlmostEqual(timeout, 10.0 + 6.0 + 2.0)
        self.assertAlmostEqual(s.spent, 0.2)
        self.assertTrue(any("mode0 admit" in line for line in self.logs))

    def test_non_passed_outcomes_keep(self):
        for outcome in ("failed", "error", "skipped", "timeout", "notrun", "unproven", "missing",
                        "duplicate", "flagged", None):
            with self.subTest(outcome=outcome):
                ex = FakeExecute(outcomes={self.tests[1]["id"]: outcome})
                self.assertEqual(self.verify(self.session(ex), {0, 1, 2}), {1})

    def test_run_level_failures_keep_everything(self):
        cases = {
            "rc": FakeExecute(rc=3),
            "rc none": FakeExecute(rc=None),
            "signal": FakeExecute(rc=-9),
            "timed out": FakeExecute(timed_out=True),
            "unsafe flag": FakeExecute(flags=["spawn"]),
            "read_patched": FakeExecute(flags=["read_patched"]),
            "raises": FakeExecute(raises=RuntimeError("boom")),
            "garbage": FakeExecute(ret="nonsense"),
            "no outcomes": FakeExecute(ret={"returncode": 0, "timed_out": False, "cpu": 0.1}),
            "no lines": FakeExecute(ret={"returncode": 0, "timed_out": False, "cpu": 0.1,
                                         "outcomes": {i: "passed" for i in self.ids([0, 1, 2])}}),
        }
        for why, ex in cases.items():
            with self.subTest(why=why):
                self.assertEqual(self.verify(self.session(ex), {0, 1, 2}), {0, 1, 2})
                self.assertEqual(len(ex.calls), 1)

    def test_exception_anywhere_leaves_selection(self):
        s = self.session(FakeExecute())
        with mock.patch.object(mode0, "gate", side_effect=ZeroDivisionError):
            self.assertEqual(self.verify(s, {0, 1}), {0, 1})
        with mock.patch.object(mode0, "contract", side_effect=KeyError):
            self.assertEqual(self.verify(s, {0, 1}), {0, 1})

    def test_mixed_failing_commit(self):
        ex = FakeExecute(outcomes={self.tests[2]["id"]: "failed"})
        self.assertEqual(self.verify(self.session(ex), {0, 2, 4}), {2})

    def test_catalog_order(self):
        tests = [self.tests[i] for i in (3, 0, 5, 1, 4, 2)]  # catalog order is list order
        with open(self.elig, "w") as fh:
            json.dump(elig_file(tests, self.src), fh)
        ex = FakeExecute()
        s = self.session(ex, tests=tests)
        s.verify("c", {0, 1, 2, 3, 4, 5}, a_result(), a_fps(), self.ms)
        self.assertEqual([t["id"] for t in ex.calls[0][0]["tests"]], [t["id"] for t in tests])

    def test_only_eligible_candidates_run(self):
        data = elig_file(self.tests, self.src)
        data["tests"][self.tests[1]["id"]]["unittest"]["flags"] = ["spawn"]
        with open(self.elig, "w") as fh:
            json.dump(data, fh)
        ex = FakeExecute()
        got = self.verify(self.session(ex, min_gain=-100, ratio=0), {0, 1, 2})
        self.assertEqual([t["id"] for t in ex.calls[0][0]["tests"]], self.ids([0, 2]))
        self.assertEqual(got, {1})

    def test_rejected_by_gate_no_run(self):
        ex = FakeExecute()
        s = self.session(ex, min_gain=100)
        self.assertEqual(self.verify(s, {0, 1}), {0, 1})
        self.assertEqual(ex.calls, [])
        self.assertTrue(any("mode0 reject (gain below minimum)" in line for line in self.logs))

    def test_contract_failure_no_run(self):
        ex = FakeExecute()
        s = self.session(ex)
        self.assertEqual(self.verify(s, {0, 1}, res=a_result(ok=False)), {0, 1})
        self.assertEqual(ex.calls, [])
        self.assertIsNone(s._elig)  # eligibility is not even loaded

    def test_cpu_budget(self):
        ex = FakeExecute(cpu=0.6)
        s = self.session(ex, cpu_budget=0.7)  # estimate per single test: 1.5 * (0.09 + 0.01)
        self.assertEqual(self.verify(s, {0}), set())
        self.assertEqual(self.verify(s, {1}), {1})  # 0.6 + 0.15 > 0.7: not admitted
        self.assertIsNone(s.off)
        self.assertEqual(len(ex.calls), 1)
        # Failed and timed-out attempts count too, and an exhausted budget stops Mode 0.
        ex = FakeExecute(cpu=1.0, timed_out=True, rc=-9)
        s = self.session(ex, cpu_budget=1.0)
        self.assertEqual(self.verify(s, {0}), {0})
        self.assertEqual(s.spent, 1.0)
        self.assertIsNotNone(s.off)
        self.assertEqual(self.verify(s, {1}), {1})
        self.assertEqual(len(ex.calls), 1)
        # An exception in the parent is charged the estimate.
        ex = FakeExecute(raises=OSError("no fork"))
        s = self.session(ex)
        self.verify(s, {0})
        self.assertGreater(s.spent, 0)

    def test_estimate_must_fit_cpu_budget(self):
        ex = FakeExecute()
        self.assertEqual(self.verify(self.session(ex, cpu_budget=0.05), {0}), {0})
        self.assertEqual(ex.calls, [])

    def test_wall_budget(self):
        ex = FakeExecute()
        s = self.session(ex, started=self.now - 79.0)  # 1 s of the 80 s replay budget left
        self.assertEqual(self.verify(s, {0}), {0})
        self.assertEqual(ex.calls, [])
        s = self.session(ex, started=self.now - 60.0)  # 20 s left: admitted, timeout cut to it
        self.verify(s, {0, 1, 2, 3, 4, 5})
        self.assertLessEqual(ex.calls[0][1], 20.0 + 1e-9)
        # Hard budget minus the output reserve also limits the wall.
        s = mode0.Session(mode0.Config({"BR_MODE0_ELIG": self.elig, "BR_MODE0_SITE": self.site}),
                          self.tests, self.src, self.now - 5, 80.0, 16.0, self.logs.append,
                          execute=ex, clock=lambda: self.now)
        self.assertEqual(self.verify(s, {0}), {0})
        self.assertEqual(len(ex.calls), 1)

    def test_refused_file_turns_off_once(self):
        with open(self.elig, "w") as fh:
            fh.write("garbage")
        ex = FakeExecute()
        s = self.session(ex)
        self.assertEqual(self.verify(s, {0}), {0})
        self.assertEqual(self.verify(s, {1}), {1})
        self.assertEqual(ex.calls, [])
        self.assertEqual(sum("mode0 off" in line for line in self.logs), 1)

    def test_unknown_overlay(self):
        ex = FakeExecute()
        s = self.session(ex)
        got = s.verify("c", {0}, a_result(), a_fps(), self.ms, overlay="fs")
        self.assertEqual(got, {0})
        self.assertEqual(ex.calls, [])

    def test_flag_off(self):
        self.assertIsNone(mode0.from_env(self.tests, self.src, 0, 80, 140, self.logs.append, env={}))
        self.assertIsNone(mode0.from_env(self.tests, self.src, 0, 80, 140, self.logs.append,
                                         env={"BR_MODE0": "0"}))
        on = {"BR_MODE0": "1", "BR_MODE0_SITE": self.site, "BR_MODE0_ELIG": self.elig}
        self.assertIsNotNone(mode0.from_env(self.tests, self.src, 0, 80, 140, self.logs.append, env=on))
        self.assertIsNone(mode0.from_env(self.tests, self.src, 0, 80, 140, self.logs.append,
                                         env=dict(on, BR_MODE0_SITE=os.path.join(self.tmp, "nope"))))
        self.assertIsNone(mode0.from_env(self.tests, self.src, 0, 80, 140, self.logs.append,
                                         env=dict(on, BR_MODE0_P="2")))


    def test_flags_off_execute_sequence(self):
        """No new flag: one positional call per attempt, no keyword arguments, same spec."""
        ex = FakeExecute(outcomes={self.tests[1]["id"]: "failed"})
        s = self.session(ex)
        self.assertEqual(self.verify(s, {0, 1, 2}), {1})
        self.assertEqual(len(ex.calls), 1)
        self.assertEqual(ex.kwargs, [{}])
        spec, timeout = ex.calls[0]
        self.assertEqual(list(spec), ["kind", "src", "patched", "engine", "site", "startup_cap", "tests"])
        self.assertEqual(spec["patched"], {"sqlglot.a": [os.path.join(os.path.realpath(self.src), "sqlglot/a.py"),
                                                        base64.b64encode(A_NEW).decode()]})
        self.assertEqual(spec["tests"], [{"id": i, "cap": 2.0} for i in self.ids([0, 1, 2])])
        self.assertAlmostEqual(timeout, 10.0 + 6.0 + 2.0)
        # The estimate is the legacy one: 1.5 * (max(proc - cpu) + sum(cpu)).
        self.assertTrue(any("cpu~0.180" in line for line in self.logs), self.logs)


# --------------------------------------------------------------- exact application (BR_MODE0_EXACT)

OLD = b"".join(b"line %d\n" % i for i in range(1, 21))


def one_file_patch(body, path="sqlglot/a.py"):
    return parse(b"diff --git a/%s b/%s\n--- a/%s\n+++ b/%s\n" % ((path.encode(),) * 4) + body)[0]


class ExactApplyTest(unittest.TestCase):
    def test_exact(self):
        fp_ = one_file_patch(b"@@ -4,3 +4,3 @@\n line 4\n-line 5\n+LINE 5\n line 6\n")
        self.assertEqual(mode0.exact_apply(fp_, OLD), OLD.replace(b"line 5\n", b"LINE 5\n"))
        two = one_file_patch(b"@@ -1,2 +1,2 @@\n-line 1\n+LINE 1\n line 2\n"
                             b"@@ -19,2 +19,3 @@\n line 19\n line 20\n+line 21\n")
        self.assertEqual(mode0.exact_apply(two, OLD),
                         OLD.replace(b"line 1\n", b"LINE 1\n", 1) + b"line 21\n")

    def test_offset_hunk_refused(self):
        from selector.patch import apply

        fp_ = one_file_patch(b"@@ -6,3 +6,3 @@\n line 4\n-line 5\n+LINE 5\n line 6\n")  # really at 4
        self.assertIsNotNone(apply(fp_, OLD))  # patch.apply finds it 2 lines up
        self.assertIsNone(mode0.exact_apply(fp_, OLD))

    def test_crlf_tolerant_match_refused(self):
        from selector.patch import apply

        crlf = OLD.replace(b"\n", b"\r\n")
        fp_ = one_file_patch(b"@@ -4,3 +4,3 @@\n line 4\n-line 5\n+LINE 5\n line 6\n")
        self.assertIsNotNone(apply(fp_, crlf))  # last-resort line-ending tolerance
        self.assertIsNone(mode0.exact_apply(fp_, crlf))
        crlf_patch = one_file_patch(b"@@ -4,3 +4,3 @@\r\n line 4\r\n-line 5\r\n+LINE 5\r\n line 6\r\n")
        self.assertIsNone(mode0.exact_apply(crlf_patch, crlf))  # any CR is refused

    def test_fuzzy_context_refused(self):
        fp_ = one_file_patch(b"@@ -4,3 +4,3 @@\n line  4\n-line 5\n+LINE 5\n line 6\n")
        self.assertIsNone(mode0.exact_apply(fp_, OLD))
        fp_ = one_file_patch(b"@@ -4,3 +4,3 @@\n line 4 \n-line 5\n+LINE 5\n line 6\n")
        self.assertIsNone(mode0.exact_apply(fp_, OLD))

    def test_shapes_git_rejects(self):
        cases = {
            "header counts": b"@@ -4,4 +4,4 @@\n line 4\n-line 5\n+LINE 5\n line 6\n",
            "no trailing context, not at EOF": b"@@ -3,3 +3,3 @@\n line 3\n line 4\n-line 5\n+LINE 5\n",
            "pure insertion": b"@@ -4,0 +5,1 @@\n+new\n",
            "overlapping hunks": b"@@ -4,3 +4,3 @@\n line 4\n-line 5\n+LINE 5\n line 6\n"
                                 b"@@ -5,3 +5,3 @@\n line 5\n-line 6\n+LINE 6\n line 7\n",
            "past EOF": b"@@ -19,3 +19,3 @@\n line 19\n-line 20\n+LINE 20\n line 21\n",
        }
        for why, body in cases.items():
            with self.subTest(why=why):
                self.assertIsNone(mode0.exact_apply(one_file_patch(body), OLD))
        at_eof = one_file_patch(b"@@ -18,3 +18,3 @@\n line 18\n line 19\n-line 20\n+LINE 20\n")
        self.assertEqual(mode0.exact_apply(at_eof, OLD), OLD.replace(b"line 20\n", b"LINE 20\n"))

    def test_no_newline_at_eof(self):
        old = b"a\nb\nc"
        fp_ = one_file_patch(b"@@ -1,3 +1,3 @@\n a\n b\n-c\n\\ No newline at end of file\n+C\n"
                             b"\\ No newline at end of file\n")
        self.assertEqual(mode0.exact_apply(fp_, old), b"a\nb\nC")
        self.assertIsNone(mode0.exact_apply(fp_, b"a\nb\nc\n"))


class ExactProblemTest(TmpDir):
    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.tmp, "sqlglot"))
        with open(os.path.join(self.tmp, "sqlglot", "a.py"), "wb") as fh:
            fh.write(OLD)

    def problem(self, body, new):
        fp_ = one_file_patch(body)
        res = result(["sqlglot/a.py"], new=new)
        return mode0.exact_problem([fp_], res, self.tmp)

    def test_exact_admitted(self):
        body = b"@@ -4,3 +4,3 @@\n line 4\n-line 5\n+LINE 5\n line 6\n"
        self.assertIsNone(self.problem(body, OLD.replace(b"line 5\n", b"LINE 5\n")))

    def test_refusals(self):
        body = b"@@ -4,3 +4,3 @@\n line 4\n-line 5\n+LINE 5\n line 6\n"
        self.assertIn("differ", self.problem(body, OLD.replace(b"line 5\n", b"LINE 6\n")))
        self.assertIn("exactly", self.problem(b"@@ -7,3 +7,3 @@\n line 4\n-line 5\n+LINE 5\n line 6\n", OLD))
        self.assertIn("unchanged", self.problem(b"@@ -4,3 +4,3 @@\n line 4\n-line 5\n+line 5\n line 6\n", OLD))
        mode_only = FilePatch()
        mode_only.old_path = mode_only.new_path = "sqlglot/a.py"
        mode_only.mode_only = True
        self.assertIsNotNone(mode0.exact_problem([mode_only], result(["sqlglot/a.py"]), self.tmp))
        with open(os.path.join(self.tmp, "sqlglot", "a.py"), "wb") as fh:
            fh.write(OLD.replace(b"line 9\n", b"line 9\r\n"))
        self.assertIn("CR", self.problem(body, OLD.replace(b"line 5\n", b"LINE 5\n")))

    def test_session_refuses_inexact(self):
        """BR_MODE0_EXACT is on by default; =0 restores the old (patch.apply) contract."""
        src = os.path.join(self.tmp, "src")
        write_tree(src, {"sqlglot/__init__.py": "", "tests/__init__.py": "", "tests/test_x.py": ""})
        with open(os.path.join(src, "sqlglot", "a.py"), "wb") as fh:
            fh.write(OLD)
        tests = catalog(2)
        elig = os.path.join(self.tmp, "e.json")
        with open(elig, "w") as fh:
            json.dump(elig_file(tests, src), fh)
        offset = one_file_patch(b"@@ -6,3 +6,3 @@\n line 4\n-line 5\n+LINE 5\n line 6\n")
        new = OLD.replace(b"line 5\n", b"LINE 5\n")
        ms = {0: 10, 1: 10}
        for exact, runs in (("1", 0), ("", 0), ("0", 1)):
            with self.subTest(exact=exact):
                ex, logs = FakeExecute(), []
                env = {"BR_MODE0_ELIG": elig, "BR_MODE0_SITE": self.tmp, "BR_MODE0_EXACT": exact}
                s = mode0.Session(mode0.Config(env), tests, src, 0.0, 80.0, 140.0, logs.append,
                                  execute=ex, clock=lambda: 1.0)
                s.verify("c", {0}, result(["sqlglot/a.py"], new=new), [offset], ms)
                self.assertEqual(len(ex.calls), runs, logs)


# ------------------------------------------------------------------------ no-site shape (NOSITE)

class NoSiteTest(SessionBase):
    def test_contract(self):
        res = a_result(call=False)
        self.assertEqual(mode0.contract(a_fps(), res, self.src), "no call/import site")
        self.assertIsNone(mode0.contract(a_fps(), res, self.src, allow_no_site=True))
        # Everything else still holds under the flag.
        self.assertIsNotNone(mode0.contract(a_fps(), a_result(call=False, ok=False), self.src, allow_no_site=True))
        empty = Result()
        self.assertIsNotNone(mode0.contract(a_fps(), empty, self.src, allow_no_site=True))

    def test_session(self):
        for flag, runs in (("", 0), ("1", 1)):
            with self.subTest(flag=flag):
                ex = FakeExecute()
                s = mode0.Session(mode0.Config({"BR_MODE0_ELIG": self.elig, "BR_MODE0_SITE": self.site,
                                                "BR_MODE0_NOSITE": flag}),
                                  self.tests, self.src, self.now, 80.0, 140.0, self.logs.append,
                                  execute=ex, clock=lambda: self.now)
                got = s.verify("c", {0, 1, 2}, a_result(call=False), a_fps(), self.ms)
                self.assertEqual(len(ex.calls), runs)
                self.assertEqual(got, {0, 1, 2} if not runs else set())

    @unittest.skipUnless(os.path.isdir(os.path.join(os.environ.get("BR_SRC", "/nonexistent"), "sqlglot")),
                         "set BR_SRC to the pinned sqlglot checkout")
    def test_sample_2haz(self):
        """2haz: an is/== swap proven a no-op; nothing but the selection's sources are patched."""
        from selector.impact import commit_impact
        from selector.maps import Maps

        src = os.environ["BR_SRC"]
        with open(os.path.join(ROOT, "sample", "commits", "2haz.patch"), "rb") as fh:
            fps = parse(fh.read())
        res = commit_impact(fps, src, Maps(os.path.join(ROOT, "maps", "maps.bin")))
        self.assertFalse(res.call_sites or res.import_files)
        self.assertTrue(res.bits)
        self.assertEqual(mode0.contract(fps, res, src), "no call/import site")
        self.assertIsNone(mode0.contract(fps, res, src, allow_no_site=True))
        self.assertIsNone(mode0.exact_problem(fps, res, src))


# ------------------------------------------------------------------------- hash seeds (SEEDS)

class SeedsTest(SessionBase):
    def setUp(self):
        super().setUp()
        runs = [{"name": "iso", "hash_seed": "random"}, {"name": "seed0", "hash_seed": 0},
                {"name": "seed1", "hash_seed": 1}]
        with open(self.elig, "w") as fh:
            json.dump(elig_file(self.tests, self.src, runs=runs), fh)

    def seeded(self, ex, seeds="0,1", **over):
        env = {"BR_MODE0_ELIG": self.elig, "BR_MODE0_SITE": self.site, "BR_MODE0_SEEDS": seeds}
        env.update({mode0.TUNABLES[k][0]: str(v) for k, v in over.items()})
        return mode0.Session(mode0.Config(env), self.tests, self.src, self.now, 80.0, 140.0, self.logs.append,
                             execute=ex, clock=lambda: self.now)

    def test_parse(self):
        self.assertEqual(mode0.parse_seeds(""), [])
        self.assertEqual(mode0.parse_seeds(None), [])
        self.assertEqual(mode0.parse_seeds(" 0, 1 "), [0, 1])
        self.assertEqual(mode0.parse_seeds("0 1"), [0, 1])
        for bad in ("x", "-1", "0,0", "1.5", str(2 ** 32), "0,,1", "0 1,2", "0 0"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    mode0.parse_seeds(bad)
        self.assertEqual(mode0.Config({}).runs, [None])
        self.assertEqual(mode0.Config({"BR_MODE0_SEEDS": "1,0"}).runs, [1, 0])

    def test_dropped_only_if_passed_under_every_seed(self):
        t = self.ids([0, 1, 2, 3])
        ex = FakeExecute(outcomes={t[1]: {0: "failed"}, t[2]: {1: "failed"}})
        got = self.verify(self.seeded(ex), {0, 1, 2, 3})
        self.assertEqual(got, {1, 2})
        self.assertEqual(ex.kwargs, [{"hash_seed": 0}, {"hash_seed": 1}])
        self.assertEqual([x["id"] for x in ex.calls[0][0]["tests"]], t)
        self.assertEqual([x["id"] for x in ex.calls[1][0]["tests"]], [t[0], t[2], t[3]])  # survivors only

    def test_nothing_passed_no_later_seed(self):
        ex = FakeExecute(outcomes={i: "failed" for i in self.ids([0, 1])})
        self.assertEqual(self.verify(self.seeded(ex), {0, 1}), {0, 1})
        self.assertEqual(ex.kwargs, [{"hash_seed": 0}])

    def test_run_failure_in_later_seed_keeps_all(self):
        calls = []

        def boom(spec, kwargs):
            calls.append(kwargs)
            if kwargs.get("hash_seed") == 1:
                raise OSError("no fork")

        ex = FakeExecute()
        ex.during = boom
        self.assertEqual(self.verify(self.seeded(ex), {0, 1}), {0, 1})
        self.assertEqual(len(calls), 2)

    def test_uncertified_seed_refused(self):
        ex = FakeExecute()
        s = self.seeded(ex, seeds="0,7")
        self.assertEqual(self.verify(s, {0, 1}), {0, 1})
        self.assertEqual(ex.calls, [])
        self.assertIn("seed 7 not certified", s.off)
        # A run named seed<N> whose recorded hash seed differs is not a certification of N.
        with open(self.elig, "w") as fh:
            json.dump(elig_file(self.tests, self.src, runs=[{"name": "iso"}, {"name": "seed0", "hash_seed": 5},
                                                          {"name": "seed1", "hash_seed": 1}]), fh)
        s = self.seeded(FakeExecute(), seeds="0")
        self.assertEqual(self.verify(s, {0}), {0})
        self.assertIsNotNone(s.off)

    def test_gate_budgets_every_run(self):
        one = self.seeded(FakeExecute(), seeds="")
        two = self.seeded(FakeExecute())
        self.verify(one, {0, 1})
        self.verify(two, {0, 1})
        est = [float(line.split("cpu~")[1].split()[0]) for line in self.logs if "cpu~" in line]
        self.assertAlmostEqual(est[-1], 2 * est[0], places=3)
        # CPU budget enough for one run but not for two: not admitted at all.
        ex = FakeExecute()
        s = self.seeded(ex, cpu_budget=est[0] * 1.5)
        self.assertEqual(self.verify(s, {0, 1}), {0, 1})
        self.assertEqual(ex.calls, [])

    def test_later_seed_reads_the_clock(self):
        """The wall left is read again before each run; a later run that does not fit keeps."""
        ex = FakeExecute()

        def slow(spec, kwargs):
            self.now += 18.5  # the first run used most of the wall budget

        ex.during = slow
        env = {"BR_MODE0_ELIG": self.elig, "BR_MODE0_SITE": self.site, "BR_MODE0_SEEDS": "0,1"}
        s = mode0.Session(mode0.Config(env), self.tests, self.src, self.now - 60.0, 80.0, 140.0,
                          self.logs.append, execute=ex, clock=lambda: self.now)
        self.assertEqual(self.verify(s, {0, 1}), {0, 1})
        self.assertEqual(len(ex.calls), 1)
        self.assertTrue(any("seed 1 skipped (wall budget" in line for line in self.logs), self.logs)


# ------------------------------------------------------------------------------- CPU estimate

class EstimateTest(unittest.TestCase):
    def test_legacy_matches_gate(self):
        cfg = config()
        info = [(0.1, 0.2), (0.05, 0.4), (0.02, 0.1)]
        items = [(c, p, None, 0) for c, p in info]
        d = mode0.gate({0, 1, 2}, [0, 1, 2], {0: 100, 1: 100, 2: 100}, info, cfg)
        self.assertEqual(mode0.estimate(items, cfg), d.cpu)
        self.assertEqual(mode0.estimate(items, cfg, warm=True), d.cpu)  # no bcpu: legacy

    def test_warm(self):
        cfg = config(BR_MODE0_WARM="1")
        self.assertEqual(cfg.cpu_factor, mode0.WARM_FACTOR)
        self.assertEqual(cfg.legacy_factor, 1.5)
        items = [(0.10, 0.40, 0.02, 0), (0.06, 0.35, 0.03, 0), (0.05, 0.30, 0.01, 0)]
        # startup max(0.30, 0.29, 0.25) + warm 0.06 + largest first use 0.08
        self.assertAlmostEqual(mode0.estimate(items, cfg, warm=True), mode0.WARM_FACTOR * (0.30 + 0.06 + 0.08))
        # One test without a warm number: the legacy model and factor.
        items[1] = (0.06, 0.35, None, 0)
        self.assertAlmostEqual(mode0.estimate(items, cfg, warm=True), 1.5 * (0.30 + 0.21))
        # A warm number above the cold one counts as the cold one.
        self.assertAlmostEqual(mode0.estimate([(0.1, 0.4, 0.5, 0)], cfg, warm=True), mode0.WARM_FACTOR * 0.4)
        self.assertEqual(config(BR_MODE0_WARM="1", cpu_factor=2).cpu_factor, 2.0)
        # The compile term (patched source the child compiles), warm model only.
        one = [(0.1, 0.4, 0.05, 0)]
        self.assertAlmostEqual(mode0.estimate(one, cfg, warm=True, compiled=2 ** 20),
                               mode0.WARM_FACTOR * (0.3 + 0.05 + 0.05 + mode0.COMPILE_S_PER_MB))
        self.assertAlmostEqual(mode0.estimate([(0.1, 0.4, None, 0)], cfg, warm=True, compiled=2 ** 20), 1.5 * 0.4)

    def test_children(self):
        cfg = config(cpu_factor=1)
        # Two tests that start pool workers: their CPU beyond the startup reference is summed.
        items = [(0.1, 2.1, None, 2), (0.2, 1.2, None, 2), (0.05, 0.35, None, 0)]
        self.assertAlmostEqual(mode0.estimate(items, cfg, startup_ref=0.3),
                               0.3 + 0.35 + (2.0 - 0.3) + (1.0 - 0.3))
        # Without a reference the whole extra CPU of such tests is summed (conservative).
        self.assertAlmostEqual(mode0.estimate(items, cfg), 0.3 + 0.35 + 2.0 + 1.0)


# ------------------------------------------------------------------------ fs overlay (BR_MODE0_FS)

def fs_header(cpu_count=2, **over):
    with open(mode0.ovl.__file__, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    h = {"overlays": ["memory", "fs"],
         "fs": {"cpu_count": cpu_count, "overlay_digest": digest, "mirror": list(mode0.ovl.MIRROR_DIRS),
                "build_cpu": {"median": 0.03, "p90": 0.04, "n": 9}, "remove_cpu": {"median": 0.02, "p90": 0.03, "n": 9}},
         "startup_cpu": {"unittest": {"median": 0.1, "p90": 0.2}, "pytest": {"median": 0.3, "p90": 0.4}}}
    h["fs"].update(over)
    return h


def fs_entries(tests, spawn=(), not_fs=()):
    """Memory and fs certification; `spawn` numbers start processes (fs only), `not_fs` fail fs."""
    out = {}
    for t in tests:
        e = entry(t["number"], t["seconds"], proc=0.1)
        fs = {"unittest": dict(engine_info(0.01, 0.11), children=0), "pytest": dict(engine_info(0.01, 0.31), children=0)}
        if t["number"] in spawn:
            e["unittest"] = engine_info(flags=["spawn", "rtracker"], ok=False, why="flagged spawn (iso)",
                                        outs={"iso": "flagged"})
            fs = {eng: dict(engine_info(0.01, 0.31, flags=["mp_spawn", "rtracker"]), children=2)
                  for eng in ("unittest", "pytest")}
        if t["number"] in not_fs:
            fs["unittest"] = engine_info(ok=False, why="failed (iso)", outs={"iso": "failed"})
        e["fs"] = fs
        out[t["id"]] = e
    return out


class FsTest(SessionBase):
    def setUp(self):
        super().setUp()
        self.work = os.path.join(self.tmp, "ovwork")
        os.makedirs(self.work)
        self.write_elig()

    def write_elig(self, spawn=(1,), not_fs=(), **hdr):
        h = fs_header(**hdr)
        with open(self.elig, "w") as fh:
            json.dump(elig_file(self.tests, self.src, entries=fs_entries(self.tests, spawn, not_fs), **h), fh)

    def fs_session(self, ex, fs="1", **over):
        env = {"BR_MODE0_ELIG": self.elig, "BR_MODE0_SITE": self.site, "BR_MODE0_FS": fs,
               mode0.TUNABLES["min_gain"][0]: "-100", mode0.TUNABLES["ratio"][0]: "0"}
        env.update({mode0.TUNABLES[k][0]: str(v) for k, v in over.items() if k in mode0.TUNABLES})
        env.update({k: v for k, v in over.items() if k.startswith("BR_")})
        s = mode0.Session(mode0.Config(env), self.tests, self.src, self.now, 80.0, 140.0, self.logs.append,
                          execute=ex, clock=lambda: self.now)
        s.fs_workdir = self.work
        return s

    def leftovers(self):
        return os.listdir(self.work)

    def test_eligibility(self):
        e = mode0.load(self.elig, self.tests, self.src, "unittest", fs_cpus=2)
        self.assertIsNone(e.fs_why)
        self.assertEqual(len(e.memory), 5)
        self.assertEqual(len(e.fs), 6)
        self.assertEqual(e.children[self.tests[1]["id"]], 2)
        self.assertAlmostEqual(e.fs_overhead, 0.07)
        self.assertEqual(len(mode0.load_eligibility(self.elig, self.tests, self.src, "unittest", overlay="fs")), 6)
        self.assertEqual(mode0.load(self.elig, self.tests, self.src, "unittest").fs, {})
        for kw, why in (({"cpu_count": 3}, "cpu_count"), ({"overlay_digest": "0" * 64}, "digest"),
                        ({"mirror": ["sqlglot"]}, "mirror")):
            with self.subTest(why=why):
                self.write_elig(**kw)
                e = mode0.load(self.elig, self.tests, self.src, "unittest", fs_cpus=2)
                self.assertIn(why, e.fs_why)
                self.assertEqual(e.fs, {})
                self.assertEqual(len(e.memory), 5)
        self.write_elig()
        self.assertIn("cpu_count", mode0.load(self.elig, self.tests, self.src, "unittest", fs_cpus=3).fs_why)

    def test_fs_entry_rules(self):
        base = fs_entries(self.tests[:1])[self.tests[0]["id"]]
        tid = self.tests[0]["id"]

        def fs_ok(e):
            with open(self.elig, "w") as fh:
                json.dump(elig_file(self.tests, self.src, entries={tid: e}, **fs_header()), fh)
            return tid in mode0.load(self.elig, self.tests, self.src, "unittest", fs_cpus=2).fs

        self.assertTrue(fs_ok(base))
        bad = {
            "no fs block": {k: v for k, v in base.items() if k != "fs"},
            "spawn": dict(base, fs=dict(base["fs"], unittest=engine_info(flags=["spawn"]))),
            "read_shadowed": dict(base, fs=dict(base["fs"], pytest=engine_info(flags=["read_shadowed"]))),
            "pytest fails fs": dict(base, fs=dict(base["fs"], pytest=engine_info(ok=False, why="x",
                                                                               outs={"iso": "failed"}))),
            "fs disagree": dict(base, fs=dict(base["fs"], disagree=True)),
            "order sensitive": dict(base, order_sensitive=True),
            "batch failed": dict(base, batch={"unittest": "failed", "pytest": "passed"}),
        }
        for why, e in bad.items():
            with self.subTest(why=why):
                self.assertFalse(fs_ok(e))
        self.assertTrue(fs_ok(dict(base, fs=dict(base["fs"], unittest=engine_info(flags=["mp_fork"])))))

    def test_spawn_candidate_runs_whole_set_in_fs(self):
        seen = {}

        def inside(spec, kwargs):
            root = spec["src"]
            with open(os.path.join(root, "sqlglot", "a.py"), "rb") as fh:
                seen["a"] = fh.read()
            seen["link"] = os.path.islink(os.path.join(root, "sqlglot", "__init__.py"))
            seen["listed"] = self.leftovers()

        ex = FakeExecute()
        ex.during = inside
        got = self.verify(self.fs_session(ex), {0, 1, 2})
        self.assertEqual(got, set())
        self.assertEqual(len(ex.calls), 1)
        spec, timeout = ex.calls[0]
        self.assertEqual(ex.kwargs, [{"cpu_count": 2}])
        self.assertEqual(spec["overlay"], "fs")
        self.assertEqual(spec["patched"], {})
        self.assertEqual(spec["pinned"], os.path.realpath(self.src))
        self.assertEqual(spec["shadowed"], [os.path.join(os.path.realpath(self.src), "sqlglot", "a.py")])
        self.assertEqual([t["id"] for t in spec["tests"]], self.ids([0, 1, 2]))
        self.assertEqual(seen["a"], A_NEW)
        self.assertTrue(seen["link"])
        self.assertEqual(len(seen["listed"]), 1)
        self.assertEqual(self.leftovers(), [])
        # The estimate: one run (children summed) plus the overlay build/remove p90s.
        self.assertTrue(any("mode0 fs admit" in line for line in self.logs), self.logs)

    def test_cpu_count_setting(self):
        self.write_elig(cpu_count=4)
        ex = FakeExecute()
        self.verify(self.fs_session(ex, BR_MODE0_FS_CPUS="4"), {0, 1})
        self.assertEqual(ex.kwargs, [{"cpu_count": 4}])

    def test_memory_only_set_stays_in_memory(self):
        ex = FakeExecute()
        self.assertEqual(self.verify(self.fs_session(ex), {0, 2}), set())
        self.assertEqual(ex.kwargs, [{}])
        self.assertNotIn("overlay", ex.calls[0][0])
        self.assertEqual(self.leftovers(), [])

    def test_flag_off_runs_memory_part(self):
        ex = FakeExecute()
        self.assertEqual(self.verify(self.fs_session(ex, fs=""), {0, 1, 2}), {1})
        self.assertEqual(ex.kwargs, [{}])
        self.assertEqual([t["id"] for t in ex.calls[0][0]["tests"]], self.ids([0, 2]))

    def test_fs_not_certified_runs_memory_part(self):
        self.write_elig(cpu_count=3)
        ex = FakeExecute()
        self.assertEqual(self.verify(self.fs_session(ex), {0, 1, 2}), {1})
        self.assertEqual(ex.kwargs, [{}])

    def test_fs_uncertified_test_left_out(self):
        self.write_elig(spawn=(1,), not_fs=(2,))
        ex = FakeExecute()
        got = self.verify(self.fs_session(ex), {0, 1, 3})
        self.assertEqual(ex.kwargs, [{"cpu_count": 2}])
        self.assertEqual(got, set())
        ex = FakeExecute()
        got = self.verify(self.fs_session(ex), {0, 1, 2})  # memory {0, 2} or fs {0, 1}: best net
        self.assertEqual(len(ex.calls), 1)
        self.assertIn(got, ({1}, {2}))

    def test_failure_keeps_and_cleans(self):
        ex = FakeExecute(outcomes={self.tests[1]["id"]: "failed"})
        self.assertEqual(self.verify(self.fs_session(ex), {0, 1, 2}), {1})
        self.assertEqual(self.leftovers(), [])

    def test_exception_in_execute_cleans(self):
        ex = FakeExecute(raises=RuntimeError("boom"))
        s = self.fs_session(ex)
        self.assertEqual(self.verify(s, {0, 1, 2}), {0, 1, 2})
        self.assertEqual(self.leftovers(), [])
        self.assertGreater(s.spent, 0)
        # Even an exception that escapes the run (e.g. in the result handling) cleans up.
        ex = FakeExecute()
        with mock.patch.object(mode0, "_passed", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.verify(self.fs_session(ex), {0, 1, 2})
        self.assertEqual(self.leftovers(), [])

    def test_overlay_errors_keep(self):
        for error in (mode0.ovl.OverlayError("no space"), OSError("ro"), ValueError("x")):
            with self.subTest(error=error):
                ex = FakeExecute()
                with mock.patch.object(mode0.ovl, "build", side_effect=error):
                    self.assertEqual(self.verify(self.fs_session(ex), {0, 1, 2}), {0, 1, 2})
                self.assertEqual(ex.calls, [])
        ex = FakeExecute()
        s = self.fs_session(ex)
        s.fs_workdir = os.path.join(self.tmp, "missing")
        self.assertEqual(self.verify(s, {0, 1, 2}), {0, 1, 2})
        self.assertEqual(ex.calls, [])

    def test_remove_failure_turns_fs_off(self):
        ex = FakeExecute()
        s = self.fs_session(ex)
        real_remove = mode0.ovl.remove
        roots = []

        def bad_remove(root):
            roots.append(root)
            raise mode0.ovl.OverlayError("busy")

        with mock.patch.object(mode0.ovl, "remove", side_effect=bad_remove):
            self.assertEqual(self.verify(s, {0, 1, 2}), set())  # the run itself was sound
        self.assertTrue(s.fs_broken)
        real_remove(roots[0])
        ex2 = FakeExecute()
        s.execute = ex2
        self.assertEqual(self.verify(s, {0, 1, 2}), {1})  # memory part only from now on
        self.assertEqual(ex2.kwargs, [{}])

    def test_seeds_in_fs_share_one_overlay(self):
        runs = [{"name": "iso"}, {"name": "seed0", "hash_seed": 0}, {"name": "seed1", "hash_seed": 1}]
        with open(self.elig, "w") as fh:
            json.dump(elig_file(self.tests, self.src, entries=fs_entries(self.tests, (1,)), runs=runs,
                                **fs_header()), fh)
        roots = []
        ex = FakeExecute(outcomes={self.tests[0]["id"]: {1: "failed"}})
        ex.during = lambda spec, kwargs: roots.append(spec["src"])
        got = self.verify(self.fs_session(ex, BR_MODE0_SEEDS="0,1"), {0, 1, 2})
        self.assertEqual(got, {0})
        self.assertEqual(ex.kwargs, [{"hash_seed": 0, "cpu_count": 2}, {"hash_seed": 1, "cpu_count": 2}])
        self.assertEqual(len(set(roots)), 1)
        self.assertEqual(self.leftovers(), [])


class SiteTest(TmpDir):
    def test_env(self):
        a, b = os.path.join(self.tmp, "a"), os.path.join(self.tmp, "b")
        os.makedirs(a)
        self.assertEqual(mode0.site_dirs(os.pathsep.join([a, b, a])), [os.path.realpath(a)])
        self.assertEqual(mode0.site_dirs(b), [])

    def test_default_is_existing_dirs(self):
        for d in mode0.site_dirs(None):
            self.assertTrue(os.path.isdir(d))

    def test_venv_without_site(self):
        """Under -S a venv's sys.prefix is the base interpreter; pyvenv.cfg finds the venv."""
        import sysconfig

        venv = os.path.join(self.tmp, "venv")
        exe = os.path.join(venv, "bin", "python")
        site = sysconfig.get_paths(scheme="venv", vars={"base": venv, "platbase": venv})["purelib"]
        self.assertTrue(site.startswith(venv), site)
        os.makedirs(site)
        os.makedirs(os.path.dirname(exe), exist_ok=True)
        with open(os.path.join(venv, "pyvenv.cfg"), "w") as fh:
            fh.write("home = /usr/bin\ninclude-system-site-packages = false\n")
        with mock.patch.object(sys, "executable", exe):
            self.assertEqual(mode0.site_dirs(None), [os.path.realpath(site)])


# -------------------------------------------------------------------------- end to end

MINI = {
    "sqlglot/__init__.py": "",
    "sqlglot/core.py": "def value():\n    return 1\n",
    "tests/__init__.py": "",
    "tests/test_core.py": """
        import unittest

        from sqlglot import core


        class TestCore(unittest.TestCase):
            def test_positive(self):
                self.assertGreater(core.value(), 0)

            def test_exact(self):
                self.assertEqual(core.value(), 1)
        """,
    "tests/test_other.py": """
        import unittest


        class TestOther(unittest.TestCase):
            def test_a(self):
                pass
        """,
    "README.md": "mini\n",
}
PATCH = b"""diff --git a/sqlglot/core.py b/sqlglot/core.py
--- a/sqlglot/core.py
+++ b/sqlglot/core.py
@@ -1,2 +1,2 @@
 def value():
-    return 1
+    return 2
"""
DOCS = b"""diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-mini
+mini repo
"""


class EndToEndTest(TmpDir):
    def setUp(self):
        super().setUp()
        self.src = os.path.join(self.tmp, "src")
        write_tree(self.src, MINI)
        self.tests = [
            {"number": 0, "id": "tests/test_core.py::TestCore::test_positive", "file": "tests/test_core.py",
             "seconds": 0.5},
            {"number": 1, "id": "tests/test_core.py::TestCore::test_exact", "file": "tests/test_core.py",
             "seconds": 0.5},
            {"number": 2, "id": "tests/test_other.py::TestOther::test_a", "file": "tests/test_other.py",
             "seconds": 0.1},
        ]
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(os.path.join(self.data, "commits"))
        with open(os.path.join(self.data, "tests.json"), "w") as fh:
            json.dump({"tests": self.tests}, fh)
        for name, body in (("core", PATCH), ("docs", DOCS)):
            with open(os.path.join(self.data, "commits", name + ".patch"), "wb") as fh:
                fh.write(body)
        # Both TestCore tests ran line 2 of sqlglot/core.py inside value(); test_other never
        # touches sqlglot; nobody reads files as text. "Every test" would be [0, 1, 2].
        maps = {"ids": [t["id"] for t in self.tests], "files": ["sqlglot/core.py"],
                "call": {0: {2: 0}}, "import": {0: {1: 0}}, "imported": {0: 0}, "reads": {},
                "reads_lc": {}, "listed": {}, "sets": [0b11], "mapped": 0b111, "untrusted": 0}
        self.maps = os.path.join(self.tmp, "maps.bin")
        with open(self.maps, "wb") as fh:
            fh.write(zlib.compress(marshal.dumps(maps)))
        self.elig = os.path.join(self.tmp, "mode0.json")
        with open(self.elig, "w") as fh:
            json.dump(elig_file(self.tests, self.src), fh)
        self.site = os.path.join(self.tmp, "site")
        os.makedirs(self.site)

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith("BR_")}
        env.update(BR_DATA=self.data, BR_MAPS=self.maps, BR_SRC=self.src, BR_MODE0_ELIG=self.elig,
                   BR_MODE0_SITE=self.site)
        env.update(extra)
        return env

    def run_selector(self, **extra):
        proc = subprocess.run([sys.executable, "-I", "-S", os.path.join(ROOT, "run.py")],
                              capture_output=True, env=self.env(**extra), timeout=120, cwd=ROOT)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        text = proc.stdout.decode("utf-8")
        self.assertTrue(text.endswith("\n"))
        out = {}
        for line in text[:-1].split("\n"):
            obj = json.loads(line)
            self.assertEqual(set(obj), {"commit", "workers"})
            out[obj["commit"]] = sorted(n for w in obj["workers"] for n in w)
        self.assertEqual(len(out), 2)
        return out, proc.stderr.decode()

    def test_passing_dropped_failing_kept(self):
        out, err = self.run_selector(BR_MODE0="1")
        self.assertEqual(out, {"core": [1], "docs": []}, err)
        self.assertIn("core: mode0 admit", err)

    def test_flag_off_is_static_selection(self):
        out, err = self.run_selector()
        self.assertEqual(out, {"core": [0, 1], "docs": []})
        self.assertNotIn("mode0", err)

    def test_refused_file_is_static_selection(self):
        with open(self.elig, "w") as fh:
            fh.write("{}")
        out, err = self.run_selector(BR_MODE0="1")
        self.assertEqual(out, {"core": [0, 1], "docs": []})
        self.assertIn("mode0 off", err)

    def test_flag_off_never_executes(self):
        """In-process main(): with BR_MODE0 unset the runner is never launched."""
        from selector import main as selector_main

        env = self.env()
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(m0, "execute", side_effect=AssertionError("executed")) as ex, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            selector_main.main()
        ex.assert_not_called()
        lines = stdout.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        with mock.patch.dict(os.environ, dict(env, BR_MODE0="1"), clear=True), \
                mock.patch.object(m0, "execute", side_effect=RuntimeError("boom")) as ex, \
                contextlib.redirect_stdout(io.StringIO()) as out2, contextlib.redirect_stderr(io.StringIO()):
            selector_main.main()
        ex.assert_called_once()
        # An exception inside execute leaves the static selection (not "every test").
        self.assertEqual(out2.getvalue().splitlines(), lines)
        core = [json.loads(line) for line in lines if '"core"' in line][0]
        self.assertEqual(sorted(n for w in core["workers"] for n in w), [0, 1])


SPAWN_TEST = """
    import multiprocessing
    import unittest
    from concurrent.futures import ProcessPoolExecutor


    def child_value():
        from sqlglot import core

        return core.value()


    class TestSpawn(unittest.TestCase):
        def run_in_child(self):
            with ProcessPoolExecutor(1, mp_context=multiprocessing.get_context("spawn")) as pool:
                return pool.submit(child_value).result()

        def test_child_positive(self):
            self.assertGreater(self.run_in_child(), 0)

        def test_child_exact(self):
            self.assertEqual(self.run_in_child(), 1)
    """


@unittest.skipUnless(sys.version_info >= (3, 13), "-X cpu_count needs Python 3.13")
class EndToEndFsTest(EndToEndTest):
    """BR_MODE0_FS=1 with the real runner: tests that start multiprocessing children run in a
    filesystem overlay, and the children import the PATCHED module (a child that saw the
    pinned core.value() == 1 would let test_child_exact pass and be dropped: a miss)."""

    def setUp(self):
        super().setUp()
        write_tree(self.src, {"tests/test_spawn.py": SPAWN_TEST})
        self.tests += [
            {"number": 3, "id": "tests/test_spawn.py::TestSpawn::test_child_positive", "file": "tests/test_spawn.py",
             "seconds": 0.2},
            {"number": 4, "id": "tests/test_spawn.py::TestSpawn::test_child_exact", "file": "tests/test_spawn.py",
             "seconds": 0.2},
        ]
        with open(os.path.join(self.data, "tests.json"), "w") as fh:
            json.dump({"tests": self.tests}, fh)
        maps = {"ids": [t["id"] for t in self.tests], "files": ["sqlglot/core.py"],
                "call": {0: {2: 0}}, "import": {0: {1: 0}}, "imported": {0: 0}, "reads": {},
                "reads_lc": {}, "listed": {}, "sets": [0b11011], "mapped": 0b11111, "untrusted": 0}
        with open(self.maps, "wb") as fh:
            fh.write(zlib.compress(marshal.dumps(maps)))
        entries = fs_entries(self.tests, spawn=(3, 4))
        with open(self.elig, "w") as fh:
            json.dump(elig_file(self.tests, self.src, entries=entries, **fs_header()), fh)
        self.tmpdir = os.path.join(self.tmp, "tmpdir")
        os.makedirs(self.tmpdir)

    def test_passing_dropped_failing_kept(self):
        out, err = self.run_selector(BR_MODE0="1", BR_MODE0_FS="1", TMPDIR=self.tmpdir,
                                     BR_MODE0_MIN_GAIN="-100", BR_MODE0_RATIO="0")
        self.assertEqual(out, {"core": [1, 4], "docs": []}, err)
        self.assertIn("core: mode0 fs admit", err)
        self.assertEqual(os.listdir(self.tmpdir), [])

    def test_flag_off_is_static_selection(self):
        out, err = self.run_selector()
        self.assertEqual(out, {"core": [0, 1, 3, 4], "docs": []})
        self.assertNotIn("mode0", err)

    def test_refused_file_is_static_selection(self):
        with open(self.elig, "w") as fh:
            fh.write("{}")
        out, err = self.run_selector(BR_MODE0="1", BR_MODE0_FS="1")
        self.assertEqual(out, {"core": [0, 1, 3, 4], "docs": []})

    def test_memory_only_without_fs(self):
        out, err = self.run_selector(BR_MODE0="1", BR_MODE0_MIN_GAIN="-100", BR_MODE0_RATIO="0")
        self.assertEqual(out, {"core": [1, 3, 4], "docs": []}, err)

    def test_passing_dropped_failing_kept_seeds(self):
        runs = [{"name": "iso"}, {"name": "seed0", "hash_seed": 0}, {"name": "seed1", "hash_seed": 1}]
        with open(self.elig, "w") as fh:
            json.dump(elig_file(self.tests, self.src, entries=fs_entries(self.tests, spawn=(3, 4)), runs=runs,
                                **fs_header()), fh)
        out, err = self.run_selector(BR_MODE0="1", BR_MODE0_FS="1", TMPDIR=self.tmpdir, BR_MODE0_SEEDS="0,1",
                                     BR_MODE0_MIN_GAIN="-100", BR_MODE0_RATIO="0", BR_MODE0_CPU_FACTOR="0.1")
        self.assertEqual(out, {"core": [1, 4], "docs": []}, err)
        self.assertEqual(err.count("core: mode0 fs admit"), 2, err)
        self.assertEqual(os.listdir(self.tmpdir), [])

    test_flag_off_never_executes = None


if __name__ == "__main__":
    unittest.main()
