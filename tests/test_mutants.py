"""Tests for the v2 validation corpus generator (tools/mutants.py) and the labeller (tools/ground_truth.py).

    python3 -m unittest tests.test_mutants

Evaluation-only tooling; nothing here is imported by selector/. Scratch files go under
the repository's ignored work/ directory. Set BR_SQLGLOT_SRC to the pinned sqlglot
checkout to also regenerate the committed manifests (slow, about a minute per set).
"""

import argparse
import gzip
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import ground_truth as gt  # noqa: E402
import mutants as mu  # noqa: E402

WORK = os.path.join(ROOT, "work", "unittest")
VALIDATION = os.path.join(ROOT, "validation")
GIT = shutil.which("git")


def scratch():
    os.makedirs(WORK, exist_ok=True)
    return tempfile.mkdtemp(prefix="t-", dir=WORK)


def slurp(path, mode="r"):
    with open(path, mode) as fh:
        return fh.read()


def git_apply(tree, patch_bytes, check=False):
    pf = os.path.join(tree, "..", os.path.basename(tree) + ".patch")
    with open(pf, "wb") as fh:
        fh.write(patch_bytes)
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=os.path.dirname(tree))
    cmd = ["git", "apply"] + (["--check"] if check else []) + [os.path.abspath(pf)]
    return subprocess.run(cmd, cwd=tree, env=env, capture_output=True)


class SplitRule(unittest.TestCase):
    def test_split_is_a_pure_function_of_the_key(self):
        self.assertEqual(mu.split_of("sqlglot/parser.py#3"), mu.split_of("sqlglot/parser.py#3"))
        keys = [f"sqlglot/parser.py#{k}" for k in range(23)]
        sides = {mu.split_of(k) for k in keys}
        self.assertEqual(sides, {"dev", "holdout"})  # a huge file is shared between the sets

    def test_regions_bucket_huge_files_only(self):
        repo = scratch()
        os.makedirs(os.path.join(repo, "sqlglot", "expressions"))
        with open(os.path.join(repo, "sqlglot", "big.py"), "w") as fh:
            fh.write("x = 1\n" * 2000)
        with open(os.path.join(repo, "sqlglot", "small.py"), "w") as fh:
            fh.write("x = 1\n" * 10)
        with open(os.path.join(repo, "sqlglot", "expressions", "e.py"), "w") as fh:
            fh.write("x = 1\n" * 600)
        ctx = mu.Ctx(repo, "dev")
        self.assertEqual(ctx.region("sqlglot/big.py", 1), "sqlglot/big.py#0")
        self.assertEqual(ctx.region("sqlglot/big.py", 501), "sqlglot/big.py#1")
        self.assertEqual(ctx.region("sqlglot/small.py", 9), "sqlglot/small.py")
        self.assertEqual(ctx.region("sqlglot/expressions/e.py", 550), "sqlglot/expressions/e.py#1")
        self.assertEqual(ctx.region("sqlglot/new.py", 1, new=True), "sqlglot/new.py")
        self.assertEqual(len(ctx.all_regions("sqlglot/big.py")), 5)


class Payloads(unittest.TestCase):
    def test_stored_streams_decompress(self):
        for data in (b"", b"abc", bytes(range(256)) * 400):
            self.assertEqual(zlib.decompress(mu._zlib_stored(data)), data)
            self.assertEqual(gzip.decompress(mu._gzip_stored(data)), data)

    def test_lines_split_on_newline_only(self):
        self.assertEqual(mu._lines("a\x0cb\nc"), ["a\x0cb\n", "c"])
        self.assertEqual("".join(mu._lines("x\n\ny\n")), "x\n\ny\n")


@unittest.skipUnless(GIT, "git not installed")
class GitApply(unittest.TestCase):
    def setUp(self):
        self.tree = scratch()
        with open(os.path.join(self.tree, "a.txt"), "w") as fh:
            fh.write("one\ntwo\nthree\n")
        with open(os.path.join(self.tree, "b.bin"), "wb") as fh:
            fh.write(mu._gzip_stored(b"1|x|\n2|y|\n"))

    def test_text_diff_round_trip_including_missing_newline(self):
        new = "one\nTWO\nthree"
        res = git_apply(self.tree, mu._udiff("a.txt", "one\ntwo\nthree\n", new).encode())
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(slurp(os.path.join(self.tree, "a.txt")), new)

    def test_binary_modify_add_delete(self):
        old = slurp(os.path.join(self.tree, "b.bin"), "rb")
        new = mu._gzip_stored(b"1|x9|\n2|y|\n")
        self.assertEqual(git_apply(self.tree, mu.binary_diff("b.bin", old, new)).returncode, 0)
        self.assertEqual(slurp(os.path.join(self.tree, "b.bin"), "rb"), new)
        blob = bytes(range(256))
        self.assertEqual(git_apply(self.tree, mu.binary_diff("c.bin", None, blob)).returncode, 0)
        self.assertEqual(slurp(os.path.join(self.tree, "c.bin"), "rb"), blob)
        self.assertEqual(git_apply(self.tree, mu.binary_diff("c.bin", blob, None)).returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.tree, "c.bin")))

    def test_rename_and_mode(self):
        old = "one\ntwo\nthree\n"
        self.assertEqual(git_apply(self.tree, mu.mode_diff("a.txt", old).encode()).returncode, 0)
        self.assertTrue(os.stat(os.path.join(self.tree, "a.txt")).st_mode & stat.S_IXUSR)
        res = git_apply(self.tree, mu.rename_diff("a.txt", "d.txt", old, old + "four\n").encode())
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(slurp(os.path.join(self.tree, "d.txt")), old + "four\n")


class Manifests(unittest.TestCase):
    """Properties of the committed (frozen) manifests; no labels may appear in them."""

    def load(self, split):
        path = os.path.join(VALIDATION, f"{split}.manifest.json")
        if not os.path.exists(path):
            self.skipTest("no committed manifest")
        with open(path) as fh:
            return json.load(fh)

    def test_sets_are_disjoint_by_region_and_family(self):
        dev, hold = self.load("dev"), self.load("holdout")
        self.assertGreaterEqual(dev["count"], 300)
        self.assertGreaterEqual(hold["count"], 300)
        dev_regions = {r for p in dev["patches"] for r in p["regions"]}
        hold_regions = {r for p in hold["patches"] for r in p["regions"]}
        self.assertFalse(dev_regions & hold_regions)
        for r in dev_regions:
            self.assertEqual(mu.split_of(r), "dev", r)
        for r in hold_regions:
            self.assertEqual(mu.split_of(r), "holdout", r)
        self.assertFalse({p["family"] for p in dev["patches"]} & set(mu.HOLDOUT_ONLY))
        self.assertTrue(set(mu.HOLDOUT_ONLY) <= {p["family"] for p in hold["patches"]})
        ids = [p["id"] for p in dev["patches"] + hold["patches"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_mode0_share_and_hashes(self):
        for split in ("dev", "holdout"):
            man = self.load(split)
            m0 = sum(p["family"] == "m0_op" for p in man["patches"])
            self.assertGreaterEqual(m0 / man["count"], 0.35)
            self.assertEqual(mu.set_digest(man["patches"]), man["set_sha256"])
            text = json.dumps(man)
            for word in ('"failing"', '"passed"', '"outcome"', '"answers"'):
                self.assertNotIn(word, text)

    @unittest.skipUnless(os.environ.get("BR_SQLGLOT_SRC"), "set BR_SQLGLOT_SRC to regenerate")
    def test_regenerates_byte_identical(self):
        for split in ("dev", "holdout"):
            man = self.load(split)
            patches = mu.build_set(os.environ["BR_SQLGLOT_SRC"], man["split"], man["seed"])
            self.assertEqual(mu.set_digest(patches), man["set_sha256"])


class Judge(unittest.TestCase):
    def test_incomplete_reports_never_pass(self):
        self.assertEqual(gt._judge([("setup", "passed")]), "incomplete")
        self.assertEqual(gt._judge([("setup", "passed"), ("call", "passed")]), "incomplete")
        self.assertEqual(gt._judge([("setup", "passed"), ("call", "passed"), ("teardown", "passed")]), "passed")
        self.assertEqual(gt._judge([("setup", "passed"), ("call", "failed")]), "failed")
        self.assertEqual(gt._judge([("setup", "skipped"), ("teardown", "passed")]), "skipped")
        self.assertEqual(gt._judge([("setup", "passed"), ("call", "passed"), ("call", "failed"),
                                    ("teardown", "passed")]), "failed")  # a failing subtest
        self.assertEqual(gt._judge([]), "incomplete")

    def test_directory_collection_errors_cover_their_files(self):
        files = ["tests/test_a.py", "tests/dialects/test_b.py", "tests/dialects_x/test_c.py"]
        self.assertEqual(gt._collect_error_hits("tests/dialects", files), {"tests/dialects/test_b.py"})
        self.assertEqual(gt._collect_error_hits("tests/test_a.py::TestA", files), {"tests/test_a.py"})
        self.assertEqual(gt._collect_error_hits("", files), set(files))


FAKE_PYTEST = r'''#!/usr/bin/env python3
"""Stands in for `python -m pytest`; FAKE_MODE says how it ends."""
import json, os, sys
mode = os.environ["FAKE_MODE"]
files = [a for a in sys.argv if a.endswith(".py")]
reports = []
ids = {"t/test_a.py": ["t/test_a.py::A::test_1", "t/test_a.py::A::test_2"], "t/test_b.py": ["t/test_b.py::B::test_1"]}
for f in files:
    for i, nodeid in enumerate(ids[f]):
        if mode == "interrupted" and nodeid.endswith("test_2"):
            reports.append([nodeid, "setup", "passed", "TestReport"])
            break
        reports += [[nodeid, w, "passed", "TestReport"] for w in ("setup", "call", "teardown")]
if mode == "crash":
    os._exit(3)
with open(os.environ["BR_RESULT_FILE"], "w") as fh:
    json.dump({"reports": reports, "collect_errors": []}, fh)
sys.exit({"ok": 0, "interrupted": 2, "silent_missing": 0}[mode])
'''


class RunSuite(unittest.TestCase):
    """The labeller's handling of chunks that do not finish normally, with a stand-in pytest."""

    def setUp(self):
        self.tree = scratch()
        os.makedirs(os.path.join(self.tree, "t"))
        for f in ("test_a.py", "test_b.py"):
            open(os.path.join(self.tree, "t", f), "w").close()
        self.fake = os.path.join(self.tree, "fake_python")
        with open(self.fake, "w") as fh:
            fh.write(FAKE_PYTEST.replace("#!/usr/bin/env python3", "#!" + sys.executable))
        os.chmod(self.fake, 0o755)
        self.tests = [{"id": "t/test_a.py::A::test_1", "file": "t/test_a.py", "seconds": 0.1, "number": 0},
                      {"id": "t/test_a.py::A::test_2", "file": "t/test_a.py", "seconds": 0.1, "number": 1},
                      {"id": "t/test_b.py::B::test_1", "file": "t/test_b.py", "seconds": 0.1, "number": 2}]

    def run_mode(self, mode, j=1):
        os.environ["FAKE_MODE"] = mode
        args = argparse.Namespace(j=j, serial_order=False, stub=None, test_timeout=5, suite_timeout=60,
                                  python=self.fake, hash_seed=None, tmp_dir=WORK)
        return gt.run_suite(self.tree, self.tests, args)

    def test_normal(self):
        status, info = self.run_mode("ok")
        self.assertEqual(set(status.values()), {"passed"})
        self.assertEqual(info["lost"], [])

    def test_interrupted_run_is_lost_not_passed(self):
        status, info = self.run_mode("interrupted")
        self.assertEqual(status["t/test_a.py::A::test_2"], "failed")
        self.assertIn("t/test_b.py::B::test_1", info["lost"])

    def test_crash_is_lost(self):
        status, info = self.run_mode("crash", j=2)
        self.assertEqual(set(status.values()), {"failed"})
        self.assertEqual(len(info["lost_chunks"]), 2)

    def test_deleted_test_file_is_missing(self):
        os.remove(os.path.join(self.tree, "t", "test_b.py"))
        status, _ = self.run_mode("ok")
        self.assertEqual(status["t/test_b.py::B::test_1"], "missing")
        self.assertEqual(status["t/test_a.py::A::test_1"], "passed")


class Cache(unittest.TestCase):
    def test_truncated_or_stale_cache_is_recomputed(self):
        d = scratch()
        path = os.path.join(d, "x.json")
        settings = {"version": gt.CACHE_VERSION}
        gt.write_json_atomic(path, {"cache_version": gt.CACHE_VERSION, "settings": settings,
                                    "patch_sha256": "abc", "status": {"t1": "passed"}, "info": {}})
        self.assertEqual(os.listdir(d), ["x.json"])
        self.assertEqual(gt.load_cached(path, settings, "abc", ["t1"], False)[0], {"t1": "passed"})
        self.assertIsNone(gt.load_cached(path, settings, "other", ["t1"], False))
        self.assertIsNone(gt.load_cached(path, {"version": 0}, "abc", ["t1"], False))
        self.assertIsNone(gt.load_cached(path, settings, "abc", ["t1", "t2"], False))
        data = slurp(path)
        with open(path, "w") as fh:
            fh.write(data[: len(data) // 2])
        self.assertIsNone(gt.load_cached(path, settings, "abc", ["t1"], False))
        with open(path, "w") as fh:
            json.dump({"t1": "passed"}, fh)
        self.assertIsNone(gt.load_cached(path, settings, "abc", ["t1"], False))
        self.assertEqual(gt.load_cached(path, settings, "abc", ["t1"], True)[0], {"t1": "passed"})


if __name__ == "__main__":
    unittest.main()
