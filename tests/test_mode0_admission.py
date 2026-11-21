"""Mode 0 admission (selector/mode0.py): contract, eligibility file, EV gate, budgets, results.

Unit tests use fake eligibility dicts, fake impact results and an injected execute(); the
end-to-end tests run the selector (`python -I -S run.py`) on a synthetic mini repository with a
hand-built maps file, exactly as the image runs it.

    python3 -m unittest discover -s tests -t .
"""

import contextlib
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
    h = {"python": PY, "implementation": sys.implementation.name,
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


def result(paths, call=True, imports=False, ok=True):
    r = Result()
    r.replica_ok = ok
    for p in paths:
        r.patched[p] = b"X = 2\n"
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
                           ("python", "garbage"), ("implementation", "pypy"), ("also_batch", False),
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

    def __call__(self, spec, timeout):
        self.calls.append((spec, timeout))
        if self.raises:
            raise self.raises
        if self.ret is not None:
            return self.ret
        ids = [t["id"] for t in spec["tests"]]
        outcomes = {i: self.outcomes.get(i, "passed") for i in ids}
        lines = {i: {"id": i, "outcome": o, "flags": self.flags} for i, o in outcomes.items()
                 if o in m0.OUTCOMES}
        return {"outcomes": outcomes, "returncode": self.rc, "cpu": self.cpu, "wall": 0.3,
                "timed_out": self.timed_out, "done": {"done": True}, "lines": lines}


class SessionTest(TmpDir):
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
        res = res or result(["sqlglot/a.py"])
        return s.verify("c1", set(selected), res, fps or [fp("sqlglot/a.py")], self.ms)

    def ids(self, nums):
        return [self.tests[n]["id"] for n in nums]

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
        s.verify("c", {0, 1, 2, 3, 4, 5}, result(["sqlglot/a.py"]), [fp("sqlglot/a.py")], self.ms)
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
        self.assertEqual(self.verify(s, {0, 1}, res=result(["sqlglot/a.py"], ok=False)), {0, 1})
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
        got = s.verify("c", {0}, result(["sqlglot/a.py"]), [fp("sqlglot/a.py")], self.ms, overlay="fs")
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


if __name__ == "__main__":
    unittest.main()
