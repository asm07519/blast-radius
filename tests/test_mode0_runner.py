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
from selector import overlay  # noqa: E402

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
    "tests/test_fs.py": '''
        import os
        import subprocess
        import sys
        import time
        import unittest
        from concurrent.futures import ProcessPoolExecutor
        from multiprocessing import get_context

        from pkg import core

        # The pinned tree when this file is a symlink in an fs overlay.
        HERE = os.path.dirname(os.path.realpath(__file__))
        KEEP = []


        def real_core_text():
            with open(os.path.join(HERE, "..", "pkg", "core.py")) as fh:
                return fh.read()


        def crash():
            os._exit(3)


        def burn(seconds):
            end = time.process_time() + seconds
            while time.process_time() < end:
                pass
            return os.getpid()


        def record_and_sleep(where):
            open(os.path.join(where, str(os.getpid())), "w").close()
            time.sleep(600)


        def exit_with_value():
            sys.exit(core.value())


        def pool(n=1):
            return ProcessPoolExecutor(max_workers=n, mp_context=get_context("spawn"))


        class TestFs(unittest.TestCase):
            def test_pool_value(self):
                with pool() as p:
                    self.assertEqual(p.submit(core.value).result(), 1)

            def test_read_real_path(self):
                self.assertIn("def value", real_core_text())

            def test_worker_reads_real_path(self):
                with pool() as p:
                    self.assertIn("def value", p.submit(real_core_text).result())

            def test_subprocess(self):
                subprocess.run([sys.executable, "-c", "pass"], check=True)

            def test_pool_crash(self):
                with pool() as p:
                    p.submit(crash).result()

            def test_pool_hang(self):
                p = pool(2)
                KEEP.append(p)
                futures = [p.submit(record_and_sleep, os.environ["M0_PIDS"]) for _ in range(2)]
                for f in futures:
                    f.result()

            def test_pool_left_running(self):
                p = pool(1)
                KEEP.append(p)  # never shut down: the runner reaps the worker
                self.assertGreater(p.submit(burn, 0.6).result(), 0)

            def test_cpu_count(self):
                n = int(os.environ["M0_CPUS"])
                self.assertEqual(os.process_cpu_count(), n)
                with ProcessPoolExecutor(mp_context=get_context("spawn")) as p:
                    self.assertEqual(p._max_workers, n)
                    self.assertEqual(p.submit(core.value).result(), 1)
                    self.assertEqual(len(set(p.map(burn, [0.2] * 8))), n)

            def test_fork_value(self):
                proc = get_context("fork").Process(target=exit_with_value)
                proc.start()
                proc.join()
                self.assertEqual(proc.exitcode, 1)

            def test_fork_reads_real_path(self):
                proc = get_context("fork").Process(target=real_core_text)
                proc.start()
                proc.join()
                self.assertEqual(proc.exitcode, 0)

            def test_forkserver(self):
                proc = get_context("forkserver").Process(target=exit_with_value)
                proc.start()
                proc.join()
                self.assertEqual(proc.exitcode, 1)

            def test_plain(self):
                self.assertEqual(core.value(), 1)
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

    def run(self, engine, ids, cap=20.0, patched=None, timeout=60.0, site=None, caps=None, **kw):
        spec = {"kind": "mode0", "src": self.root, "patched": patched or {}, "engine": engine,
                "site": site if site is not None else (PYTEST_SITE or []),
                "tests": [{"id": i, "cap": (caps or {}).get(i, cap)} for i in ids]}
        return m0.execute(spec, timeout, python=PYTHON, workdir=self.work, **kw)

    def run_fs(self, engine, ids, changes=None, cap=20.0, timeout=60.0, site=None, **kw):
        """Build an fs overlay (outside the repo), run, remove it."""
        ovdir = self.root + "_ov"
        os.makedirs(ovdir, exist_ok=True)
        root = overlay.build(self.root, {k: v.encode() for k, v in (changes or {}).items()},
                             ovdir, mirror=("pkg", "tests"))
        try:
            spec = {"kind": "mode0", "engine": engine,
                    "site": site if site is not None else (PYTEST_SITE or []),
                    "tests": [{"id": i, "cap": cap} for i in ids], **overlay.spec_fields(root)}
            return m0.execute(spec, timeout, python=PYTHON, workdir=self.work, **kw)
        finally:
            overlay.remove(root)

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.root + "_ov", ignore_errors=True)


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


F = "tests/test_fs.py::TestFs::"
PATCH_CORE = {"pkg/core.py": "def value():\n    return 2\n"}
COMMENT_CORE = {"pkg/core.py": "def value():\n    return 1\n# changed\n"}


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class TestFsMode(unittest.TestCase):
    """"overlay": "fs": the patched tree on disk, multiprocessing children audited."""

    @classmethod
    def setUpClass(cls):
        cls.repo = Repo()

    @classmethod
    def tearDownClass(cls):
        cls.repo.close()

    def run_fs(self, ids, **kw):
        return self.repo.run_fs(kw.pop("engine", "unittest"), ids, **kw)

    def test_unpatched_passes(self):
        ids = [F + "test_pool_value", F + "test_plain", F + "test_read_real_path", B + "test_pass",
               B + "test_read_source"]
        res = self.run_fs(ids)
        self.assertEqual(res["returncode"], 0)
        self.assertEqual(set(res["outcomes"].values()), {"passed"}, res["lines"])
        self.assertEqual(res["done"]["overlay"], "fs")
        self.assertEqual(res["done"]["mp_children"], 1)
        self.assertIn("mp_spawn", res["lines"][F + "test_pool_value"]["flags"])
        self.assertFalse(m0.UNSAFE_FLAGS & set(res["lines"][F + "test_pool_value"]["flags"]))

    def test_patched_module_fails(self):
        res = self.run_fs([B + "test_pass"], changes=PATCH_CORE)
        self.assertEqual(res["outcomes"][B + "test_pass"], "failed")

    def test_patched_test_module_fails(self):
        source = textwrap.dedent(FILES["tests/test_basic.py"]).lstrip("\n").replace(
            "self.assertEqual(core.value(), 1)", "self.assertEqual(core.value(), 3)")
        res = self.run_fs([B + "test_pass"], changes={"tests/test_basic.py": source})
        self.assertEqual(res["outcomes"][B + "test_pass"], "failed")

    def test_spawn_worker_sees_patched_code(self):
        tid = F + "test_pool_value"
        res = self.run_fs([tid], changes=PATCH_CORE)
        self.assertEqual(res["lines"][tid]["outcome"], "failed", res["lines"][tid])
        self.assertIn("2 != 1", res["lines"][tid]["detail"])
        # The same edit through the in-memory overlay: the worker imports the unpatched file
        # and the test passes, which is why such a pass is flagged "spawn" there.
        res = self.repo.run("unittest", [tid], patched=self.repo.patched("pkg/core.py", PATCH_CORE["pkg/core.py"]))
        self.assertEqual(res["lines"][tid]["outcome"], "passed")
        self.assertIn("spawn", res["lines"][tid]["flags"])
        self.assertEqual(res["outcomes"][tid], "flagged")

    def test_reading_patched_file_is_faithful(self):
        # memory mode flags read_patched; on disk the patched bytes are what a test reads.
        tid = B + "test_read_source"
        res = self.run_fs([tid], changes=COMMENT_CORE)
        self.assertEqual(res["outcomes"][tid], "passed", res["lines"][tid])

    def test_shadowed_read_is_unsafe(self):
        ids = [F + "test_read_real_path", F + "test_plain"]
        res = self.run_fs(ids, changes=COMMENT_CORE)
        self.assertEqual(res["lines"][ids[0]]["outcome"], "passed")
        self.assertIn("read_shadowed", res["lines"][ids[0]]["flags"])
        self.assertEqual(res["outcomes"][ids[0]], "flagged")
        self.assertEqual(res["outcomes"][ids[1]], "flagged")  # sticky: later tests too

    def test_shadowed_read_in_worker_is_unsafe(self):
        tid = F + "test_worker_reads_real_path"
        res = self.run_fs([tid])
        self.assertEqual(res["outcomes"][tid], "passed")
        res = self.run_fs([tid], changes=COMMENT_CORE)
        self.assertEqual(res["lines"][tid]["outcome"], "passed")
        self.assertIn("read_shadowed", res["lines"][tid]["flags"])
        self.assertEqual(res["outcomes"][tid], "flagged")

    def test_raw_subprocess_still_unsafe(self):
        tid = F + "test_subprocess"
        res = self.run_fs([tid, F + "test_plain"])
        self.assertIn("spawn", res["lines"][tid]["flags"])
        self.assertEqual(res["outcomes"][tid], "flagged")
        self.assertEqual(res["outcomes"][F + "test_plain"], "flagged")

    def test_worker_crash_is_not_a_pass(self):
        tid = F + "test_pool_crash"
        res = self.run_fs([tid])
        self.assertEqual(res["lines"][tid]["outcome"], "failed")
        self.assertIn("BrokenProcessPool", res["lines"][tid]["detail"])

    def test_timeout_with_live_workers_kills_them(self):
        base = os.path.join(ROOT, "work")
        where = tempfile.mkdtemp(prefix="m0-pids-", dir=base)
        try:
            tid = F + "test_pool_hang"
            t0 = time.monotonic()
            res = self.run_fs([tid, F + "test_plain"], cap=4.0, env=dict(os.environ, M0_PIDS=where))
            self.assertLess(time.monotonic() - t0, 30)
            self.assertEqual(res["returncode"], m0.EXIT_TIMEOUT, res["lines"])
            self.assertEqual(res["lines"][tid]["outcome"], "timeout")
            self.assertNotIn("passed", res["outcomes"].values())
            pids = [int(n) for n in os.listdir(where)]
            self.assertEqual(len(pids), 2)
            deadline = time.monotonic() + 5
            while any(_alive(p) for p in pids) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse([p for p in pids if _alive(p)])
        finally:
            shutil.rmtree(where, ignore_errors=True)

    def test_children_cpu_is_counted(self):
        tid = F + "test_pool_left_running"
        res = self.run_fs([tid])
        self.assertEqual(res["outcomes"][tid], "passed", res["lines"][tid])
        self.assertGreater(res["cpu"], 0.55)  # the worker's 0.6 s, reaped by the runner

    def test_cpu_count_cap(self):
        tid = F + "test_cpu_count"
        for n in (1, 2):
            res = self.run_fs([tid], cpu_count=n, env=dict(os.environ, M0_CPUS=str(n)))
            self.assertEqual(res["outcomes"][tid], "passed", res["lines"][tid])
            self.assertEqual(res["done"]["mp_children"], n)

    def test_fork_child(self):
        tid = F + "test_fork_value"
        res = self.run_fs([tid])
        self.assertEqual(res["outcomes"][tid], "passed", res["lines"][tid])
        self.assertIn("mp_fork", res["lines"][tid]["flags"])
        res = self.run_fs([tid], changes=PATCH_CORE)
        self.assertEqual(res["lines"][tid]["outcome"], "failed")
        tid = F + "test_fork_reads_real_path"
        res = self.run_fs([tid], changes=COMMENT_CORE)
        self.assertIn("read_shadowed", res["lines"][tid]["flags"])
        self.assertEqual(res["outcomes"][tid], "flagged")

    def test_forkserver_is_unsafe(self):
        tid = F + "test_forkserver"
        res = self.run_fs([tid])
        self.assertIn("spawn", res["lines"][tid]["flags"])
        self.assertNotEqual(res["outcomes"][tid], "passed")

    def test_no_bytecode_written(self):
        self.run_fs([F + "test_pool_value"], changes=PATCH_CORE)
        written = [os.path.join(r, n) for r, _, names in os.walk(self.repo.root) for n in names
                   if n.endswith(".pyc")]
        self.assertEqual(written, [])

    def test_bad_fs_specs(self):
        root = self.repo.root
        for extra in ({"overlay": "disk"}, {"overlay": "fs", "shadowed": "x"},
                      {"overlay": "fs", "shadowed": ["rel/path"]},
                      {"overlay": "fs", "shadowed": [], "patched": self.repo.patched("pkg/core.py", "")}):
            spec = {"kind": "mode0", "src": root, "patched": {}, "engine": "unittest", "site": [],
                    "tests": [{"id": B + "test_pass", "cap": 5.0}], **extra}
            res = m0.execute(spec, 30, python=PYTHON, workdir=self.repo.work)
            self.assertEqual(res["returncode"], m0.EXIT_BADSPEC, extra)

    def test_memory_mode_explicit(self):
        res = self.repo.run("unittest", [B + "test_pass"])
        self.assertNotIn("overlay", res["done"])
        spec = {"kind": "mode0", "overlay": "memory", "src": self.repo.root, "patched": {},
                "engine": "unittest", "site": [], "tests": [{"id": B + "test_pass", "cap": 5.0}]}
        res = m0.execute(spec, 30, python=PYTHON, workdir=self.repo.work)
        self.assertEqual(res["outcomes"][B + "test_pass"], "passed")
        self.assertNotIn("overlay", res["done"])

    @unittest.skipIf(PYTEST_SITE is None, "pytest not found (set BR_MODE0_PYTHON and BR_MODE0_SITE)")
    def test_pytest_engine(self):
        ids = [F + "test_pool_value", B + "test_pass"]
        res = self.run_fs(ids, engine="pytest")
        self.assertEqual(set(res["outcomes"].values()), {"passed"}, res["lines"])
        res = self.run_fs(ids, engine="pytest", changes=PATCH_CORE)
        self.assertEqual({res["lines"][i]["outcome"] for i in ids}, {"failed"}, res["lines"])


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
        argv, _ = m0.launch_argv("/x/spec.json", python="/py", cpu_count=2)
        self.assertEqual(argv, ["/py", "-I", "-S", "-B", "-X", "cpu_count=2", m0.REPLICA_RUNNER, "/x/spec.json"])
        argv, _ = m0.launch_argv("/x/spec.json", "/py", 1, {}, 3)
        self.assertEqual(argv, ["/py", "-s", "-P", "-S", "-B", "-X", "cpu_count=3", m0.REPLICA_RUNNER, "/x/spec.json"])

    def test_digests(self):
        self.assertEqual(len(m0.runner_digest()), 64)
        tests = [{"number": 0, "id": "a", "seconds": 1.0}, {"number": 1, "id": "b", "seconds": 2.0}]
        same = [dict(t, seconds=9.0) for t in tests]
        self.assertEqual(m0.tests_identity(tests), m0.tests_identity(same))
        self.assertNotEqual(m0.tests_identity(tests), m0.tests_identity(tests[::-1]))


if __name__ == "__main__":
    unittest.main()
