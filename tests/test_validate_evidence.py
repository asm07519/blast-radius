"""Tests for tools/validate_evidence.py on synthetic tiny maps (stdlib only, no Docker).

    python3 -m unittest tests.test_validate_evidence
"""

import contextlib
import copy
import io
import json
import marshal
import os
import platform
import re
import sys
import tempfile
import unittest
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import validate_evidence as ve  # noqa: E402

IDS = ["tests/test_a.py::A::test_one", "tests/test_a.py::A::test_two", "tests/test_b.py::B::test_three"]
EXEC_TEST = IDS[2]
SUB_TEST = IDS[0]


def tiny_maps(**extra):
    """A complete, well-formed compact map (format 3) for the three IDS."""
    sets = [0b001, 0b011, 0b111, 0b100]
    data = {
        "format": 3,
        "exec_phases": {EXEC_TEST: {
            "suite": {"test": EXEC_TEST, "fixture": "f.sql", "schema": "S", "tables": "t"},
            "fingerprint": {"queries": [{"skip": True}, {"tables": ["x"], "sta": "()", "optimized": "ab",
                                                        "rendered": "ok", "duck": "cd"}]},
            "exposed": {"sqlglot/a.py": [1, 2]}}},
        "subtests": {SUB_TEST: {"outside": {"sqlglot/a.py": [1]},
                                "windows": [["duckdb", {"sqlglot/a.py": [2]}], ["hive", {"sqlglot/b.py": [3]}]]}},
        "typeprof": {"sqlglot/a.py:2:4": (("ident", "none", False),)},
        "typeprof_incomplete": 0,
        "class_users": {"sqlglot.a:A": 1},
        "classes_at_import": ("sqlglot.a:A",),
        "call_at_import": ((0, 2),),
        # sqlglot really has names `label` and `patch`: they must not look like evaluation data
        "names": {"label": (0, (), ()), "patch": (1, (("sqlglot.a:A", ""),), ()), "x": (2, (), (("self", "f"),))},
        "dynamic": [("^.*_sql$", 1, "self", "")],
        "class_info": {"sqlglot.a:A": (True, ("sqlglot.a:A",))},
        "commit": ve.SQLGLOT_COMMIT,
        "ids": list(IDS),
        "files": ["sqlglot/a.py", "sqlglot/b.py", "tests/test_a.py"],
        "call": {0: {2: 1}, 1: {3: 3}},
        "import": {0: {1: 2}, 2: {1: 0}},
        "imported": {0: 2, 2: 0},
        "reads": {"tests/fixtures/f.sql": 3},
        "reads_lc": {},
        "listed": {"tests/fixtures": 3},
        "mapped": 0b111,
        "untrusted": 0,
        "sets": sets,
    }
    data.update(extra)
    return data


class Fixture:
    """A temp checkout: maps/, a pinned 'sqlglot' tree, Dockerfiles and requirements.lock."""

    def __init__(self, tmp):
        self.tmp = tmp
        self.root = os.path.join(tmp, "root")
        self.repo = os.path.join(tmp, "src")
        self.maps = os.path.join(self.root, "maps", "maps.bin")
        self.tests = os.path.join(self.root, "maps", "tests.json")
        self.manifest = os.path.join(self.root, "maps", "manifest.json")
        for d in ("maps", "tools", "selector"):
            os.makedirs(os.path.join(self.root, d), exist_ok=True)
        os.makedirs(os.path.join(self.repo, "sqlglot"), exist_ok=True)
        self.write(os.path.join(self.repo, "sqlglot", "a.py"), "A = 1\n")
        self.write(os.path.join(self.repo, "sqlglot", "b.py"), "B = 2\n")
        self.write(os.path.join(self.root, "Dockerfile"), "FROM python:3.13-slim@sha256:" + "a" * 64 + "\n")
        self.write(os.path.join(self.root, "tools", "Dockerfile.tools"), "FROM python:3.13-slim@sha256:" + "a" * 64 + "\n")
        self.write(os.path.join(self.root, "requirements.lock"), "pytest==9.2.0\n")
        self.write(os.path.join(self.root, "tools", "compact_maps.py"), "FORMAT = 3\n")
        self.write(os.path.join(self.root, "tools", "build_maps.py"), "# generator\n")
        tests = {"tests": [{"number": i, "id": t, "file": t.split("::")[0], "seconds": 0.1} for i, t in enumerate(IDS)],
                 "skipped": []}
        self.write(self.tests, json.dumps(tests))
        self.save_maps(tiny_maps())

    @staticmethod
    def write(path, text):
        with open(path, "w") as fh:
            fh.write(text)

    def save_maps(self, data, level=9):
        with open(self.maps, "wb") as fh:
            fh.write(zlib.compress(marshal.dumps(data), level))

    def args(self, *more, repo=True):
        base = ["--maps", self.maps, "--tests", self.tests, "--manifest", self.manifest, "--root", self.root,
                "--check-python"]
        return base + (["--repo", self.repo] if repo else []) + list(more)

    def run(self, *more, repo=True):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = ve.main(self.args(*more, repo=repo))
        return rc, err.getvalue()

    def write_manifest(self):
        rc, log = self.run("--write")
        assert rc == 0, log
        with open(self.manifest) as fh:
            return json.load(fh)

    def save_manifest(self, m):
        with open(self.manifest, "w") as fh:
            json.dump(m, fh)


def leaves(obj, path=()):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from leaves(v, path + (k,))
    else:
        yield path, obj


def tampered(value):
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, str):
        return value + "x"
    if value is None:
        return 0
    if isinstance(value, list):
        return value + ["x"]
    raise TypeError(value)


class ValidateEvidenceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = Fixture(self._tmp.name)
        self.manifest = self.fx.write_manifest()

    def tearDown(self):
        self._tmp.cleanup()

    def assertValid(self, *more, repo=True):
        rc, log = self.fx.run(*more, repo=repo)
        self.assertEqual(rc, 0, log)
        return log

    def assertInvalid(self, needle, *more, repo=True):
        rc, log = self.fx.run(*more, repo=repo)
        self.assertEqual(rc, 1, log)
        self.assertIn(needle, log)
        return log

    def test_fresh_manifest_validates(self):
        log = self.assertValid()
        self.assertIn("[evidence] ok: 3 ids, 3 mapped, 0 untrusted", log)
        v = self.manifest["verified"]
        self.assertEqual(v["counts"]["trusted"], 3)
        self.assertEqual(v["environment"]["python"], platform.python_version())
        self.assertEqual(v["source"]["tree_entries"], 2)
        self.assertEqual(sorted(v["sections"]), sorted(tiny_maps()))

    def test_manifest_is_reproducible(self):
        with open(self.fx.manifest, "rb") as fh:
            first = fh.read()
        self.fx.write_manifest()
        with open(self.fx.manifest, "rb") as fh:
            self.assertEqual(fh.read(), first)
        self.assertNotIn("time", json.dumps(self.manifest).lower())

    def test_every_verified_field_is_checked(self):
        for path, value in leaves(self.manifest["verified"]):
            with self.subTest(field=".".join(path)):
                m = copy.deepcopy(self.manifest)
                node = m["verified"]
                for k in path[:-1]:
                    node = node[k]
                node[path[-1]] = tampered(value)
                self.fx.save_manifest(m)
                self.assertInvalid(".".join(path))
        self.fx.save_manifest(self.manifest)
        self.assertValid()

    def test_provenance_is_not_fatal(self):
        m = copy.deepcopy(self.manifest)
        m["provenance"]["seeds"] = {"build_maps": [7]}
        self.fx.save_manifest(m)
        self.assertValid()
        with open(os.path.join(self.fx.root, "tools", "build_maps.py"), "a") as fh:
            fh.write("# edited\n")
        self.assertIn("generator sources changed", self.assertValid())

    def test_changed_test_set_changes_one_section(self):
        data = tiny_maps()
        data["sets"] = [0b001, 0b011, 0b111, 0b110]
        self.fx.save_maps(data)
        log = self.assertInvalid("sections.sets")
        self.assertIn("maps.sha256", log)
        changed = set(re.findall(r"ERROR: sections\.(\w+):", log))
        # set 3 is used by call, reads and listed: exactly those sections (and sets) change
        self.assertEqual(changed, {"call", "listed", "reads", "sets"})

    def test_reinterning_keeps_section_checksums(self):
        a = tiny_maps()
        b = copy.deepcopy(a)
        perm = [2, 0, 3, 1]  # new index of old set i
        b["sets"] = [None] * 4
        for old, new in enumerate(perm):
            b["sets"][new] = a["sets"][old]
        remap = lambda i: perm[i]  # noqa: E731
        b["call"] = {k: {line: remap(i) for line, i in per.items()} for k, per in a["call"].items()}
        b["import"] = {k: {line: remap(i) for line, i in per.items()} for k, per in a["import"].items()}
        for key in ("imported", "reads", "reads_lc", "listed", "class_users"):
            b[key] = {k: remap(i) for k, i in a[key].items()}
        b["names"] = {n: (remap(r), i, c) for n, (r, i, c) in a["names"].items()}
        b["dynamic"] = [(p, remap(s), *rest) for p, s, *rest in a["dynamic"]]
        self.assertEqual(ve.section_checksums(a), ve.section_checksums(b))
        self.fx.save_maps(b)
        log = self.assertInvalid("maps.sha256")
        self.assertNotIn("sections.", log)

    def test_recompressed_maps_fail_on_digest_only(self):
        self.fx.save_maps(tiny_maps(), level=1)
        log = self.assertInvalid("maps.sha256")
        self.assertNotIn("sections.", log)

    def test_tests_json_tamper(self):
        with open(self.fx.tests) as fh:
            tests = json.load(fh)
        tests["tests"][0]["seconds"] = 9.9
        Fixture.write(self.fx.tests, json.dumps(tests))
        log = self.assertInvalid("tests.sha256")
        self.assertNotIn("catalog_sha256", log)

    def test_reordered_catalog(self):
        with open(self.fx.tests) as fh:
            tests = json.load(fh)
        a, b = tests["tests"][0]["id"], tests["tests"][1]["id"]
        tests["tests"][0]["id"], tests["tests"][1]["id"] = b, a
        Fixture.write(self.fx.tests, json.dumps(tests))
        log = self.assertInvalid("tests.catalog_sha256")
        self.assertIn("ids differ", log)

    def test_source_tree_tamper(self):
        Fixture.write(os.path.join(self.fx.repo, "sqlglot", "a.py"), "A = 2\n")
        self.assertInvalid("source.tree_sha256")

    def test_source_tree_ignores_bytecode_caches(self):
        os.makedirs(os.path.join(self.fx.repo, "sqlglot", "__pycache__"))
        Fixture.write(os.path.join(self.fx.repo, "sqlglot", "__pycache__", "a.cpython-313.pyc"), "x")
        self.assertValid()

    def test_without_repo_tree_is_not_checked(self):
        Fixture.write(os.path.join(self.fx.repo, "sqlglot", "a.py"), "A = 2\n")
        self.assertIn("tree digest not checked", self.assertValid(repo=False))

    def test_environment_tamper(self):
        Fixture.write(os.path.join(self.fx.root, "Dockerfile"), "FROM python:3.13-slim@sha256:" + "b" * 64 + "\n")
        self.assertInvalid("environment.base_image")
        Fixture.write(os.path.join(self.fx.root, "Dockerfile"), "FROM python:3.13-slim@sha256:" + "a" * 64 + "\n")
        Fixture.write(os.path.join(self.fx.root, "requirements.lock"), "pytest==9.2.1\n")
        self.assertInvalid("environment.requirements_lock_sha256")

    def test_python_version(self):
        m = copy.deepcopy(self.manifest)
        m["verified"]["environment"]["python"] = "3.0.0"
        self.fx.save_manifest(m)
        self.assertInvalid("environment.python")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = ve.main([a for a in self.fx.args() if a != "--check-python"])
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn("environment.python not checked", err.getvalue())

    def test_untrusted(self):
        self.fx.save_maps(tiny_maps(untrusted=0b010))
        self.fx.write_manifest()
        self.assertInvalid("1 untrusted and 0 unmapped")
        self.assertValid("--allow-untrusted")

    def test_unmapped(self):
        self.fx.save_maps(tiny_maps(mapped=0b011))
        self.fx.write_manifest()
        self.assertInvalid("0 untrusted and 1 unmapped")

    def test_unknown_top_level_key(self):
        self.fx.save_maps(tiny_maps(extra_section={}))
        self.assertInvalid("unknown top-level map keys: ['extra_section']")
        rc, log = self.fx.run("--write")
        self.assertEqual(rc, 1)
        self.assertIn("refusing to write", log)

    def test_missing_required_key(self):
        data = tiny_maps()
        del data["reads"]
        self.fx.save_maps(data)
        self.assertInvalid("missing top-level map keys: ['reads']")

    def test_evaluation_looking_data(self):
        cases = {
            "top-level answers": tiny_maps(answers={"2haz": [12]}),
            "failing key in subtests": tiny_maps(subtests={SUB_TEST: {
                "outside": {}, "windows": [], "failing": [1]}}),
            "labels key in exec query": tiny_maps(exec_phases={EXEC_TEST: {
                "suite": {"test": EXEC_TEST}, "fingerprint": {"queries": [{"labels": 1}]}, "exposed": {}}}),
            "mutants key in class_info": tiny_maps(class_info={"mutants": (True, ())}),
            "patch path in reads": tiny_maps(reads={"data/commits/2haz.patch": 3}),
            "answers file in listed": tiny_maps(listed={"sample/answers.json": 3}),
            "mutant id in names": tiny_maps(names={"x": (0, (), ()), "failing": (1, (), ())}),
        }
        for name, data in cases.items():
            with self.subTest(case=name):
                self.fx.save_maps(data)
                rc, log = self.fx.run()
                self.assertEqual(rc, 1, log)
                self.assertTrue("evaluation-looking" in log or "unexpected" in log or "unknown" in log, log)

    def test_broken_references(self):
        cases = {
            "set index": tiny_maps(reads={"tests/fixtures/f.sql": 99}),
            "file index": tiny_maps(call={7: {1: 0}}),
            "bitset too wide": tiny_maps(mapped=0b1111),
            "unknown exec test": tiny_maps(exec_phases={"tests/x.py::X::y": tiny_maps()["exec_phases"][EXEC_TEST]}),
        }
        for name, data in cases.items():
            with self.subTest(case=name):
                self.fx.save_maps(data)
                self.assertInvalid("ERROR")

    def test_garbage_maps(self):
        with open(self.fx.maps, "wb") as fh:
            fh.write(b"not a map")
        self.assertInvalid("not a zlib-compressed marshal map")

    def test_missing_manifest(self):
        os.remove(self.fx.manifest)
        self.assertInvalid("missing (run make manifest)")

    def test_write_needs_repo(self):
        rc, log = self.fx.run("--write", repo=False)
        self.assertEqual(rc, 1)
        self.assertIn("--write needs --repo", log)


@unittest.skipUnless(os.path.exists(os.path.join(ROOT, "maps", "manifest.json")), "no committed manifest")
class CommittedEvidenceTest(unittest.TestCase):
    def test_committed_maps_match_manifest(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = ve.main(["--maps", os.path.join(ROOT, "maps", "maps.bin"),
                          "--tests", os.path.join(ROOT, "maps", "tests.json"),
                          "--manifest", os.path.join(ROOT, "maps", "manifest.json"), "--root", ROOT])
        self.assertEqual(rc, 0, err.getvalue())


if __name__ == "__main__":
    unittest.main()
