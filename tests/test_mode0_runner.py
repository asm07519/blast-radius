"""Mode 0 runner: result protocol, outcome mapping, caps, flags and the overlay.

Runs a tiny synthetic repository (built under work/) through selector/replica_runner.py
exactly as the selector launches it. The pytest engine is exercised when pytest can be
found: in this interpreter, or via BR_MODE0_PYTHON / BR_MODE0_SITE (an interpreter and its
site-packages directory, e.g. the labelling venv).

    python3 -m unittest discover -s tests -t .
"""

import base64
import json
import os
import shutil
import signal
import sys
import tempfile
import textwrap
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from selector import mode0_runner as m0  # noqa: E402

PYTHON = os.environ.get("BR_MODE0_PYTHON") or sys.executable


def _pytest_site():
    env = os.environ.get("BR_MODE0_SITE")
    if env:
        return [env] if os.path.isdir(os.path.join(env, "pytest")) else None
    if os.environ.get("BR_MODE0_PYTHON"):
        return None
    try:
        import importlib.util

        spec = importlib.util.find_spec("pytest")
    except Exception:
        return None
    if spec is None or not spec.origin:
        return None
    return [os.path.dirname(os.path.dirname(spec.origin))]


PYTEST_SITE = _pytest_site()

FILES = {
    "pkg/__init__.py": "",
    "pkg/core.py": "def value():\n    return 1\n",
    "tests/__init__.py": "",
    "tests/test_basic.py": '''
        import os
        import subprocess
        import sys
        import unittest

        from pkg import core


        def helper():
            return 1


        def test_function():
            assert True


        class Helper:
            def test_x(self):
                pass


        class TestBasic(unittest.TestCase):
            def test_pass(self):
                self.assertEqual(core.value(), 1)

            def test_fail(self):
                self.assertEqual(1, 2)

            def test_error(self):
                raise ValueError("boom")

            def test_subtest_fail(self):
                for i in range(3):
                    with self.subTest(i=i):
                        self.assertNotEqual(i, 1)

            def test_subtest_pass(self):
                for i in range(3):
                    with self.subTest(i=i):
                        self.assertGreaterEqual(i, 0)

            @unittest.skip("not today")
            def test_skip(self):
                pass

            def test_skiptest(self):
                self.skipTest("later")

            @unittest.expectedFailure
            def test_xfail(self):
                self.assertEqual(1, 2)

            @unittest.expectedFailure
            def test_xpass(self):
                pass

            def test_print(self):
                chunk = "x" * 1024 + "\\n"
                for _ in range(4096):
                    sys.stdout.write(chunk)
                    sys.stderr.write(chunk)
                print("done")

            def test_spawn(self):
                subprocess.run([sys.executable, "-c", "pass"], check=True)

            def test_read_source(self):
                with open(core.__file__) as fh:
                    self.assertIn("def value", fh.read())

            def test_cleanup_fails(self):
                self.addCleanup(lambda: 1 / 0)

            def test_setup_error_free(self):
                pass

            def not_a_test(self):
                pass


        class TestSetUpClassFails(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                raise RuntimeError("no class")

            def test_a(self):
                pass

            def test_b(self):
                pass


        class TestTearDownClassFails(unittest.TestCase):
            @classmethod
            def tearDownClass(cls):
                raise RuntimeError("no teardown")

            def test_a(self):
                pass

            def test_b(self):
                pass


        @unittest.skip("whole class")
        class TestSkippedClass(unittest.TestCase):
            def test_a(self):
                pass


        class TestSetUpFails(unittest.TestCase):
            def setUp(self):
                raise RuntimeError("no setUp")

            def test_a(self):
                pass
    ''',
    "tests/test_module_fixture.py": '''
        import unittest


        def setUpModule():
            raise RuntimeError("no module")


        class TestM(unittest.TestCase):
            def test_a(self):
                pass
    ''',
    "tests/test_broken.py": '''
        import unittest

        raise ImportError("cannot collect")


        class TestB(unittest.TestCase):
            def test_a(self):
                pass
    ''',
    "tests/test_slow.py": '''
        import re
        import time
        import unittest


        class TestSlow(unittest.TestCase):
            def test_quick(self):
                pass

            def test_sleep(self):
                time.sleep(60)

            def test_busy(self):
                while True:
                    pass

            def test_busy_catching(self):
                while True:
                    try:
                        while True:
                            pass
                    except BaseException:
                        pass

            def test_after(self):
                pass
    ''',
    "tests/test_mp.py": '''
        import multiprocessing
        import unittest

        from pkg import core


        class TestMP(unittest.TestCase):
            def test_spawn_pool(self):
                with multiprocessing.get_context("spawn").Pool(1) as pool:
                    self.assertEqual(pool.apply(core.value), 1)
    ''',
    "tests/test_state.py": '''
        import os
        import sys
        import unittest


        class TestState(unittest.TestCase):
            def test_drop_finder(self):
                sys.meta_path.pop(0)

            def test_chdir(self):
                os.chdir(os.path.dirname(os.getcwd()))

            def test_after(self):
                pass
    ''',
}

B = "tests/test_basic.py::TestBasic::"
MATRIX = {
    B + "test_pass": "passed",
    B + "test_fail": "failed",
    B + "test_error": "failed",
    B + "test_subtest_fail": "failed",
    B + "test_subtest_pass": "passed",
    B + "test_skip": "skipped",
    B + "test_skiptest": "skipped",
    B + "test_xfail": "skipped",
    B + "test_xpass": "failed",
    B + "test_print": "passed",
    B + "test_cleanup_fails": "failed",
    B + "test_setup_error_free": "passed",
    "tests/test_basic.py::TestSetUpClassFails::test_a": "error",
    "tests/test_basic.py::TestSetUpClassFails::test_b": "error",
    "tests/test_basic.py::TestTearDownClassFails::test_a": "passed",
    "tests/test_basic.py::TestTearDownClassFails::test_b": "error",
    "tests/test_basic.py::TestSkippedClass::test_a": "skipped",
    "tests/test_basic.py::TestSetUpFails::test_a": "failed",
    "tests/test_module_fixture.py::TestM::test_a": "error",
    "tests/test_broken.py::TestB::test_a": "error",
    "tests/test_slow.py::TestSlow::test_quick": "passed",
}
UNKNOWN = [
    B + "test_nope",
    B + "not_a_test",
    "tests/test_basic.py::Helper::test_x",
    "tests/test_missing.py::TestX::test_y",
]


class Repo:
    def __init__(self):
        base = os.path.join(ROOT, "work")
        os.makedirs(base, exist_ok=True)
        self.root = tempfile.mkdtemp(prefix="m0-repo-", dir=base)
        for rel, text in FILES.items():
            path = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write(textwrap.dedent(text).lstrip("\n"))
        self.work = os.path.join(self.root, "_work")
        os.makedirs(self.work)

    def patched(self, rel, text):
        name = rel[:-3].replace("/", ".")
        if name.endswith(".__init__"):
            name = name[: -len(".__init__")]
        return {name: [os.path.join(self.root, rel), base64.b64encode(text.encode()).decode()]}

    def run(self, engine, ids, cap=20.0, patched=None, timeout=60.0, site=None, caps=None):
        spec = {"kind": "mode0", "src": self.root, "patched": patched or {}, "engine": engine,
                "site": site if site is not None else (PYTEST_SITE or []),
                "tests": [{"id": i, "cap": (caps or {}).get(i, cap)} for i in ids]}
        return m0.execute(spec, timeout, python=PYTHON, workdir=self.work)

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


class EngineMixin:
    engine = None

    @classmethod
    def setUpClass(cls):
        cls.repo = Repo()

    @classmethod
    def tearDownClass(cls):
        cls.repo.close()

    def run_ids(self, ids, **kw):
        return self.repo.run(self.engine, ids, **kw)

    def test_matrix(self):
        res = self.run_ids(list(MATRIX))
        self.assertEqual(res["returncode"], 0)
        self.assertTrue(res["done"]["complete"])
        lines = {i: res["lines"][i]["outcome"] for i in MATRIX}
        self.assertEqual(lines, MATRIX)
        out = res["outcomes"]
        self.assertEqual({i for i, o in out.items() if o == "passed"},
                         {i for i, o in MATRIX.items() if o == "passed"})
        self.assertIn("xfail", res["lines"][B + "test_xfail"]["flags"])
        self.assertEqual(res["done"]["engine"], self.engine)
        self.assertGreaterEqual(res["done"]["startup_cpu"], 0)
        self.assertGreater(res["cpu"], 0)

    def test_isolated_outcomes_match_matrix(self):
        # One test per process gives the same outcomes (incl. class fixtures).
        for tid in (B + "test_pass", "tests/test_basic.py::TestTearDownClassFails::test_a",
                    "tests/test_basic.py::TestTearDownClassFails::test_b"):
            expected = MATRIX[tid] if not tid.endswith("TestTearDownClassFails::test_a") else "error"
            res = self.run_ids([tid])
            self.assertEqual(res["lines"][tid]["outcome"], expected, tid)

    def test_unknown_ids_are_errors(self):
        res = self.run_ids(UNKNOWN + [B + "test_pass"])
        for tid in UNKNOWN:
            self.assertNotEqual(res["outcomes"][tid], "passed", tid)
            self.assertIn(res["lines"].get(tid, {}).get("outcome", "notrun"), ("error", "notrun"), tid)

    def test_module_function(self):
        # A plain pytest function: the unittest engine does not reproduce it (error); pytest runs it.
        tid = "tests/test_basic.py::test_function"
        res = self.run_ids([tid])
        self.assertEqual(res["outcomes"][tid], "error" if self.engine == "unittest" else "passed")

    def test_sleep_past_cap(self):
        ids = ["tests/test_slow.py::TestSlow::test_quick", "tests/test_slow.py::TestSlow::test_sleep",
               "tests/test_slow.py::TestSlow::test_after"]
        t0 = time.monotonic()
        res = self.run_ids(ids, cap=1.0)
        self.assertLess(time.monotonic() - t0, 8.0)
        self.assertEqual(res["returncode"], m0.EXIT_TIMEOUT)
        self.assertEqual(res["lines"][ids[0]]["outcome"], "passed")
        self.assertEqual(res["lines"][ids[1]]["outcome"], "timeout")
        self.assertEqual(res["lines"][ids[2]]["outcome"], "notrun")
        self.assertFalse(res["done"]["complete"])
        self.assertNotIn("passed", res["outcomes"].values())

    def test_busy_loop_past_cap(self):
        for name in ("test_busy", "test_busy_catching"):
            ids = [f"tests/test_slow.py::TestSlow::{name}", "tests/test_slow.py::TestSlow::test_after"]
            t0 = time.monotonic()
            res = self.run_ids(ids, cap=1.0)
            self.assertLess(time.monotonic() - t0, 8.0)
            self.assertEqual(res["returncode"], m0.EXIT_TIMEOUT, name)
            self.assertEqual(res["lines"][ids[0]]["outcome"], "timeout")
            self.assertEqual(res["lines"][ids[1]]["outcome"], "notrun")
            self.assertNotIn("passed", res["outcomes"].values())

    def test_spawn_flag(self):
        tid = B + "test_spawn"
        res = self.run_ids([tid, B + "test_pass"])
        self.assertEqual(res["lines"][tid]["outcome"], "passed")
        self.assertIn("spawn", res["lines"][tid]["flags"])
        self.assertEqual(res["outcomes"][tid], "flagged")
        self.assertEqual(res["outcomes"][B + "test_pass"], "passed")
        self.assertNotIn("spawn", res["lines"][B + "test_pass"]["flags"])

    def test_multiprocessing_spawn_flag_and_no_bytecode(self):
        # A spawn-context child imports pkg.core from disk (unpatched): the pass is not evidence.
        tid = "tests/test_mp.py::TestMP::test_spawn_pool"
        res = self.run_ids([tid])
        self.assertEqual(res["lines"][tid]["outcome"], "passed")
        self.assertIn("spawn", res["lines"][tid]["flags"])
        self.assertEqual(res["outcomes"][tid], "flagged")
        written = [os.path.join(r, n) for r, _, names in os.walk(self.repo.root) for n in names
                   if n.endswith(".pyc")]
        self.assertEqual(written, [])

    def test_prints_megabytes(self):
        tid = B + "test_print"
        res = self.run_ids([tid])
        self.assertEqual(res["outcomes"][tid], "passed")

    def test_overlay_is_imported(self):
        tid = B + "test_pass"
        self.assertEqual(self.run_ids([tid])["outcomes"][tid], "passed")
        patched = self.repo.patched("pkg/core.py", "def value():\n    return 2\n")
        res = self.run_ids([tid], patched=patched)
        self.assertEqual(res["outcomes"][tid], "failed")

    def test_overlay_of_test_module(self):
        tid = B + "test_pass"
        source = textwrap.dedent(FILES["tests/test_basic.py"]).replace(
            "self.assertEqual(core.value(), 1)", "self.assertEqual(core.value(), 3)")
        res = self.run_ids([tid], patched=self.repo.patched("tests/test_basic.py", source))
        self.assertEqual(res["outcomes"][tid], "failed")

    def test_read_patched_flag(self):
        tid = B + "test_read_source"
        res = self.run_ids([tid])
        self.assertEqual(res["outcomes"][tid], "passed")
        patched = self.repo.patched("pkg/core.py", "def value():\n    return 1\n# changed\n")
        res = self.run_ids([tid], patched=patched)
        self.assertIn("read_patched", res["lines"][tid]["flags"])
        self.assertEqual(res["outcomes"][tid], "flagged")

    def test_finder_removed_stops(self):
        ids = ["tests/test_state.py::TestState::test_drop_finder", "tests/test_state.py::TestState::test_after"]
        res = self.run_ids(ids)
        self.assertIn("meta_path", res["lines"][ids[0]]["flags"])
        self.assertEqual(res["outcomes"][ids[0]], "flagged")
        self.assertEqual(res["lines"][ids[1]]["outcome"], "notrun")

    def test_chdir_stops(self):
        ids = ["tests/test_state.py::TestState::test_chdir", "tests/test_state.py::TestState::test_after"]
        res = self.run_ids(ids)
        self.assertIn("cwd", res["lines"][ids[0]]["flags"])
        self.assertNotEqual(res["outcomes"][ids[0]], "passed")
        self.assertEqual(res["lines"][ids[1]]["outcome"], "notrun")


class TestUnittestEngine(EngineMixin, unittest.TestCase):
    engine = "unittest"

    def test_gil_holding_c_code_is_killed(self):
        # Catastrophic backtracking runs in C without releasing the GIL: the watchdog thread
        # cannot run, the kernel SIGALRM backstop kills the process.
        source = textwrap.dedent(FILES["tests/test_slow.py"]).replace(
            "time.sleep(60)", "re.match(r'(a+)+$', 'a' * 40 + 'b')")
        tid = "tests/test_slow.py::TestSlow::test_sleep"
        t0 = time.monotonic()
        res = self.repo.run("unittest", [tid, "tests/test_slow.py::TestSlow::test_after"], cap=1.0,
                            patched=self.repo.patched("tests/test_slow.py", source))
        self.assertLess(time.monotonic() - t0, 10.0)
        self.assertIn(res["returncode"], (-signal.SIGALRM, m0.EXIT_TIMEOUT))
        self.assertNotIn("passed", res["outcomes"].values())

    def test_bad_spec(self):
        res = self.repo.run("unittest", [B + "test_pass"], cap=-1)
        self.assertEqual(res["returncode"], m0.EXIT_BADSPEC)
        self.assertNotEqual(res["outcomes"][B + "test_pass"], "passed")

    def test_no_pytest_needed(self):
        res = self.repo.run("unittest", [B + "test_pass"], site=[])
        self.assertEqual(res["outcomes"][B + "test_pass"], "passed")


@unittest.skipIf(PYTEST_SITE is None, "pytest not found (set BR_MODE0_PYTHON and BR_MODE0_SITE)")
class TestPytestEngine(EngineMixin, unittest.TestCase):
    engine = "pytest"

    def test_engines_agree(self):
        ids = list(MATRIX) + UNKNOWN[:3]
        a = self.repo.run("unittest", ids)
        b = self.repo.run("pytest", ids)
        for tid in MATRIX:
            self.assertEqual(a["lines"][tid]["outcome"], b["lines"][tid]["outcome"], tid)
        self.assertEqual(a["outcomes"], b["outcomes"] | {t: a["outcomes"][t] for t in UNKNOWN[:3]})
        self.assertTrue(b["done"]["pytest"])


class TestParseResults(unittest.TestCase):
    def setUp(self):
        os.makedirs(os.path.join(ROOT, "work"), exist_ok=True)
        fd, self.path = tempfile.mkstemp(dir=os.path.join(ROOT, "work"), prefix="m0-parse-")
        os.close(fd)
        self.ids = ["a", "b", "c"]

    def tearDown(self):
        os.remove(self.path)

    def write(self, objs, tail="\n", raw=None):
        with open(self.path, "w") as fh:
            if raw is not None:
                fh.write(raw)
                return
            fh.write("\n".join(json.dumps(o) for o in objs) + tail)

    def good(self, **done):
        lines = [{"start": True, "schema": m0.SCHEMA, "engine": "unittest", "n": 3},
                 {"id": "a", "outcome": "passed", "cpu": 0, "wall": 0, "flags": []},
                 {"id": "b", "outcome": "failed", "cpu": 0, "wall": 0, "flags": []},
                 {"id": "c", "outcome": "passed", "cpu": 0, "wall": 0, "flags": ["spawn"]}]
        lines.append(dict({"done": True, "complete": True, "n": 3}, **done))
        return lines

    def test_good(self):
        self.write(self.good())
        self.assertEqual(m0.parse_results(self.path, self.ids, 0),
                         {"a": "passed", "b": "failed", "c": "flagged"})

    def test_nonzero_exit(self):
        self.write(self.good())
        self.assertNotIn("passed", m0.parse_results(self.path, self.ids, 1).values())
        self.assertNotIn("passed", m0.parse_results(self.path, self.ids, -9).values())

    def test_truncated(self):
        self.write(self.good(), tail="")
        self.assertNotIn("passed", m0.parse_results(self.path, self.ids).values())
        text = "\n".join(json.dumps(o) for o in self.good()) + "\n"
        for cut in range(1, len(text) - 1, 7):
            self.write(None, raw=text[:cut])
            self.assertNotIn("passed", m0.parse_results(self.path, self.ids).values(), cut)

    def test_missing_done(self):
        self.write(self.good()[:-1])
        self.assertNotIn("passed", m0.parse_results(self.path, self.ids).values())

    def test_incomplete_or_wrong_count(self):
        self.write(self.good(complete=False))
        self.assertNotIn("passed", m0.parse_results(self.path, self.ids).values())
        self.write(self.good(n=2))
        self.assertNotIn("passed", m0.parse_results(self.path, self.ids).values())

    def test_garbled(self):
        lines = self.good()
        text = "\n".join(json.dumps(o) for o in lines[:2]) + "\n{not json\n" + \
            "\n".join(json.dumps(o) for o in lines[2:]) + "\n"
        self.write(None, raw=text)
        self.assertNotIn("passed", m0.parse_results(self.path, self.ids).values())
        self.write(None, raw=text.replace("{not json", '{"id": "x", "outcome": "great", "flags": []}'))
        self.assertNotIn("passed", m0.parse_results(self.path, self.ids).values())
        self.write(None, raw="\n".join(json.dumps(o) for o in lines).replace('"cpu": 0', '"cpu": NaN') + "\n")
        self.assertNotIn("passed", m0.parse_results(self.path, self.ids).values())
        self.write(None, raw="\xff\xfe")
        self.assertNotIn("passed", m0.parse_results(self.path, self.ids).values())
        with open(self.path, "wb") as fh:
            fh.write(b"\xff\n")
        self.assertNotIn("passed", m0.parse_results(self.path, self.ids).values())

    def test_duplicates_and_unknown(self):
        lines = self.good()
        lines.insert(2, {"id": "a", "outcome": "passed", "cpu": 0, "wall": 0, "flags": []})
        lines.insert(2, {"id": "zzz", "outcome": "passed", "cpu": 0, "wall": 0, "flags": []})
        lines[-1]["n"] = 5
        self.write(lines)
        out = m0.parse_results(self.path, self.ids)
        self.assertEqual(out["a"], "duplicate")
        self.assertNotIn("zzz", out)
        self.assertEqual(out["b"], "failed")

    def test_missing_line_and_file(self):
        lines = self.good()
        del lines[1]
        lines[-1]["n"] = 2
        self.write(lines)
        self.assertEqual(m0.parse_results(self.path, self.ids)["a"], "missing")
        self.assertNotIn("passed", m0.parse_results(self.path + ".nope", self.ids).values())

    def test_no_start_line(self):
        self.write(self.good()[1:])
        self.assertNotIn("passed", m0.parse_results(self.path, self.ids).values())


class TestParentSide(unittest.TestCase):
    def test_import_is_light(self):
        import subprocess

        code = ("import sys; sys.path.insert(0, %r); import selector.mode0_runner; "
                "print(sorted(m for m in ('unittest', 'pytest', '_pytest') if m in sys.modules))" % ROOT)
        out = subprocess.run([sys.executable, "-I", "-S", "-c", code], capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "[]", out.stderr)

    def test_launch_argv(self):
        argv, env = m0.launch_argv("/x/spec.json", python="/py")
        self.assertEqual(argv, ["/py", "-I", "-S", "-B", m0.REPLICA_RUNNER, "/x/spec.json"])
        argv, env = m0.launch_argv("/x/spec.json", python="/py", hash_seed=1,
                                   env={"PYTHONPATH": "/evil", "PYTHONHASHSEED": "7", "HOME": "/h"})
        self.assertEqual(argv, ["/py", "-s", "-P", "-S", "-B", m0.REPLICA_RUNNER, "/x/spec.json"])
        self.assertEqual(env, {"PYTHONHASHSEED": "1", "HOME": "/h"})

    def test_digests(self):
        self.assertEqual(len(m0.runner_digest()), 64)
        tests = [{"number": 0, "id": "a", "seconds": 1.0}, {"number": 1, "id": "b", "seconds": 2.0}]
        same = [dict(t, seconds=9.0) for t in tests]
        self.assertEqual(m0.tests_identity(tests), m0.tests_identity(same))
        self.assertNotEqual(m0.tests_identity(tests), m0.tests_identity(tests[::-1]))


if __name__ == "__main__":
    unittest.main()
