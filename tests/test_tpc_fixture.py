"""BR_TPC_FIXTURE=1: a plain edit of tpc-h.sql / tpc-ds.sql keeps only the executor tests whose
part of the loaded pairs can change (selector/tpc_fixture.py).

The facts the rule rests on are checked against the pinned sqlglot checkout (work/sqlglot, or
BR_TEST_SRC); those tests are skipped without it.

Run from the repository root:  python3 -m unittest discover -s tests -t .
"""

import ast
import difflib
import hashlib
import importlib.util
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from selector import impact, tpc_fixture
from selector.maps import Maps
from selector.patch import parse

ROOT = Path(__file__).resolve().parents[1]
SRC = Path(os.environ.get("BR_TEST_SRC", ROOT.parents[2] / "work" / "sqlglot"))
if not (SRC / "tests" / "helpers.py").is_file():
    SRC = ROOT / "work" / "sqlglot"
HAVE_SRC = (SRC / "tests" / "helpers.py").is_file() and (SRC / "tests" / "test_executor.py").is_file()
NEED_SRC = unittest.skipUnless(HAVE_SRC, f"pinned sqlglot checkout not found at {SRC}")
MAPS = ROOT / "maps" / "maps.bin"

TPCH, TPCDS = tpc_fixture.TPCH, tpc_fixture.TPCDS
OPT_TPCDS = "tests/test_optimizer.py::TestOptimizer::test_tpcds"
OPT_TPCH = "tests/test_optimizer.py::TestOptimizer::test_tpch"
SHARED = {"tpch_sqls", "tpcds_sqls", "cache", "cached_execute", "subtestHelper", "_mp_execute",
          "tpch_conn", "tpcds_conn"}


def pinned_helpers():
    """tests/helpers.py of the pinned checkout, imported on its own."""
    spec = importlib.util.spec_from_file_location("br_pinned_helpers", SRC / "tests" / "helpers.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def outcome(fn):
    try:
        return "ok", fn()
    except Exception as error:  # noqa: BLE001
        return "raised", type(error), str(error)


# --- step 1: what the pinned tests read -------------------------------------------------------

@NEED_SRC
class PinnedFactsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (SRC / "tests" / "test_executor.py").read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def test_hashes(self):
        for path, digest in tpc_fixture.PINNED.items():
            with self.subTest(path):
                self.assertEqual(hashlib.sha256((SRC / path).read_bytes()).hexdigest(), digest)

    def functions(self):
        """{qualname: FunctionDef} of every function in the module (methods, nested ones)."""
        out = {}

        def walk(node, prefix):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out[prefix + child.name] = child
                    walk(child, prefix + child.name + ".")
                elif isinstance(child, ast.ClassDef):
                    walk(child, prefix + child.name + ".")
                else:
                    walk(child, prefix)

        walk(self.tree, "")
        return out

    def own_nodes(self, fn):
        """Nodes of fn's body, not of functions nested in it."""
        todo = list(fn.body)
        while todo:
            node = todo.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                continue
            yield node
            todo.extend(ast.iter_child_nodes(node))

    def test_one_class_and_no_computed_access(self):
        classes = [n.name for n in self.tree.body if isinstance(n, ast.ClassDef)]
        self.assertEqual(classes, ["TestExecutor"])
        names = {n.id for n in ast.walk(self.tree) if isinstance(n, ast.Name)}
        attrs = {n.attr for n in ast.walk(self.tree) if isinstance(n, ast.Attribute)}
        for bad in ("getattr", "setattr", "delattr", "vars", "globals", "locals", "exec", "eval", "__import__"):
            self.assertNotIn(bad, names)
        self.assertNotIn("__dict__", attrs)
        strings = {n.value for n in ast.walk(self.tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        self.assertFalse(SHARED & strings, "a shared attribute named in a string")

    def test_who_touches_the_shared_state(self):
        touched = {}
        for name, fn in self.functions().items():
            hits = {n.attr for n in self.own_nodes(fn) if isinstance(n, ast.Attribute) and n.attr in SHARED}
            if hits:
                touched[name] = hits
        self.assertEqual(touched, {
            "TestExecutor.setUpClass": {"tpch_conn", "tpcds_conn", "cache", "tpch_sqls", "tpcds_sqls"},
            "TestExecutor.tearDownClass": {"tpch_conn", "tpcds_conn"},
            "TestExecutor.cached_execute": {"tpch_conn", "tpcds_conn", "cache"},
            "TestExecutor.test_optimized_tpch": {"tpch_sqls", "cached_execute", "tpch_conn"},
            "TestExecutor.subtestHelper": {"tpch_sqls", "tpcds_sqls", "cached_execute"},
            "TestExecutor._mp_execute": {"subtestHelper"},
            "TestExecutor.test_execute_tpch": {"_mp_execute", "tpch_sqls"},
            "TestExecutor.test_execute_tpcds": {"_mp_execute", "tpcds_sqls"},
        })
        # Nested functions (setup() in setUpClass) and module functions touch none of it, and
        # the module-level helpers are used only where expected.
        uses = {}
        for name, fn in self.functions().items():
            for n in self.own_nodes(fn):
                if isinstance(n, ast.Name) and n.id in ("mp_execute", "initializer", "load_sql_fixture_pairs",
                                                         "_tables", "_schema"):
                    uses.setdefault(n.id, set()).add(name)
        self.assertEqual(uses, {
            "mp_execute": {"TestExecutor._mp_execute"},
            "initializer": {"TestExecutor._mp_execute"},
            "load_sql_fixture_pairs": {"TestExecutor.setUpClass"},
            "_tables": {"initializer", "mp_execute"},
            "_schema": {"initializer", "mp_execute"},
        })

    def test_setupclass_loads_both(self):
        fn = self.functions()["TestExecutor.setUpClass"]
        loads = {}
        for n in self.own_nodes(fn):
            if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Attribute) \
                    and n.targets[0].attr in ("tpch_sqls", "tpcds_sqls"):
                loads[n.targets[0].attr] = ast.unparse(n.value)
        self.assertEqual(loads, {
            "tpch_sqls": "list(load_sql_fixture_pairs('optimizer/tpc-h/tpc-h.sql'))",
            "tpcds_sqls": "list(load_sql_fixture_pairs('optimizer/tpc-ds/tpc-ds.sql'))",
        })

    def unpacked(self, fn_name):
        """[(target, iterable/value)] of the tuple unpackings in a function, as source."""
        fn = self.functions()[fn_name]
        out = []
        for n in self.own_nodes(fn):
            if isinstance(n, (ast.For, ast.comprehension)) and isinstance(n.target, ast.Tuple):
                out.append((ast.unparse(n.target), ast.unparse(n.iter)))
            elif isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Tuple):
                out.append((ast.unparse(n.targets[0]), ast.unparse(n.value)))
        return out

    def loads_of(self, fn_name, name):
        fn = self.functions()[fn_name]
        return sum(1 for n in self.own_nodes(fn) if isinstance(n, ast.Name) and n.id == name
                   and isinstance(n.ctx, ast.Load))

    def test_which_element_each_test_uses(self):
        # (meta, sql, expected): test_optimized_tpch drops meta
        self.assertEqual(self.unpacked("TestExecutor.test_optimized_tpch"),
                         [("(i, (_, sql, optimized))", "enumerate(self.tpch_sqls, start=1)")])
        # subtestHelper: sql only
        self.assertEqual(self.unpacked("TestExecutor.subtestHelper"),
                         [("(_, sql, _)", "self.tpch_sqls[i] if tpch else self.tpcds_sqls[i]")])
        # _mp_execute: meta and sql, never expected
        self.assertEqual(sorted(self.unpacked("TestExecutor._mp_execute")),
                         [("(args, sql, _)", "sqls"), ("(i, future)", "enumerate(futures)")])
        for fn in ("TestExecutor.test_optimized_tpch", "TestExecutor.subtestHelper", "TestExecutor._mp_execute"):
            self.assertEqual(self.loads_of(fn, "_"), 0, fn)
        self.assertEqual(self.loads_of("TestExecutor._mp_execute", "sqls"), 1)
        # mp_execute reads meta only through .get("execute") and returns None when it is falsy
        mp = self.functions()["mp_execute"]
        self.assertEqual(ast.unparse(mp.body[0]), "if not meta.get('execute'):\n    return None")
        self.assertEqual(self.loads_of("mp_execute", "meta"), 1)
        # subtestHelper (the only path to cached_execute from _mp_execute) runs only for a table
        self.assertEqual(ast.unparse(self.functions()["TestExecutor._mp_execute"]).split("\n")[-4:], [
            "        for i, future in enumerate(futures):",
            "            table = future.result()",
            "            if table is not None:",
            "                self.subtestHelper(i, table, tpch=tpch)",
        ])

    def test_nothing_else_names_the_executor_state(self):
        for path in sorted((SRC / "tests").rglob("*.py")):
            if path.name == "test_executor.py" and path.parent == SRC / "tests":
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for word in ("TestExecutor", "test_executor", "tpch_sqls", "tpcds_sqls"):
                with self.subTest(path=str(path), word=word):
                    self.assertNotIn(word, text)
        self.assertEqual(list(SRC.rglob("conftest.py")), [])

    def test_pinned_fixtures_do_not_collide(self):
        h = tpc_fixture.load_pairs((SRC / TPCH).read_bytes())
        d = tpc_fixture.load_pairs((SRC / TPCDS).read_bytes())
        self.assertEqual((len(h), len(d)), (22, 99))
        self.assertFalse({s for _, s, _ in h} & {s for _, s, _ in d})
        self.assertFalse(tpc_fixture._executes(h))
        self.assertTrue(tpc_fixture._executes(d))


# --- step 2: the loader -----------------------------------------------------------------------

TRICKY = [
    b"",
    b";",
    b"SELECT 1;\nSELECT 1;\n",
    b"SELECT 1;SELECT 1\n;SELECT 2;\n",  # odd number of statements
    b"SELECT 1 ;  \n  SELECT 1  \t\n;\n",  # trailing whitespace
    b"-- c\nSELECT 1;\n--x\nSELECT 1;\n\n\n",
    b"# execute: true\nSELECT 1;\nSELECT 1;\n",
    b"# execute: true\n# other : a: b \nSELECT 1;\nSELECT 1;\n",
    b"#execute:true\n#execute: false\nSELECT 1;\nSELECT 1;\n",  # duplicate key, last wins
    b"# execute true\nSELECT 1;\nSELECT 1;\n",  # "#" line without colon: ValueError
    b"# a: 1\n# b: 2\n;\nSELECT 1;\n",  # statement that is only "#" lines: IndexError
    b"#\n;x;\n",
    b"SELECT 1;\n# not meta: in expected\nSELECT 1;\n",
    b"SELECT 1;\r\nSELECT 1;\r\n# execute: true\r\nSELECT 2;\r\nSELECT 2;\r\n",  # CRLF
    b"SELECT 1;\rSELECT 1;\r# execute: true\rSELECT 2;\rSELECT 2;",  # lone CR
    b"SELECT 1;\r\r\n\n\rSELECT 1;",
    b"SELECT '\xff';\nSELECT 1;\n",  # not UTF-8
    b"SELECT 1;\nSELECT '\xed\xa0\x80';\n",  # encoded surrogate: not UTF-8 either
    "﻿# execute: true\nSELECT 1;\nSELECT 1;\n".encode(),  # BOM kept: not a meta line
    "SELECT 1\x0b# execute: true\x0cSELECT 2;\x1c-- c\x85SELECT 3 ; x;".encode(),  # str.splitlines
    "SELECT 'é';\n  SELECT 'é' 　;\n".encode(),
    b"\n\n;\n\n;\n\n",
]


@NEED_SRC
class LoaderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers = pinned_helpers()
        cls.tmp = tempfile.mkdtemp(prefix="br-tpc-")
        cls.helpers.FIXTURES_DIR = cls.tmp

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def same(self, data):
        with open(os.path.join(self.tmp, "f.sql"), "wb") as fh:
            fh.write(data)
        real = outcome(lambda: list(self.helpers.load_sql_fixture_pairs("f.sql")))
        ours = outcome(lambda: tpc_fixture.load_pairs(data))
        self.assertEqual(ours, real)
        return ours

    def test_real_fixtures(self):
        for path in (TPCH, TPCDS):
            with self.subTest(path):
                kind, value = self.same((SRC / path).read_bytes())
                self.assertEqual(kind, "ok")
                self.assertGreater(len(value), 20)

    def test_tricky_inputs(self):
        kinds = set()
        for data in TRICKY:
            with self.subTest(data=data):
                result = self.same(data)
                kinds.add(result[:2] if result[0] == "raised" else "ok")
        # every way the pinned loader can fail is exercised
        for error in (ValueError, IndexError, UnicodeDecodeError):
            self.assertIn(("raised", error), kinds)

    def test_mutated_real_fixture(self):
        data = (SRC / TPCDS).read_bytes()
        for old, new in ((b"# execute: true\n", b"# execute true\n"), (b";\n", b";\r\n"), (b"\n", b"\r"),
                         (b"LIMIT 100;", b"LIMIT 100;;"), (b"-- TPC-DS 1\n", b"-- TPC-DS 1\n#x: y\n")):
            with self.subTest(old=old, new=new):
                self.same(data.replace(old, new, 1))
                self.same(data.replace(old, new))


# --- step 3: the rule -------------------------------------------------------------------------

def diff(path, old, new, header=b"index 1111111..2222222 100644\n"):
    p = path.encode()
    body = b"".join(difflib.diff_bytes(difflib.unified_diff, old.splitlines(True), new.splitlines(True),
                                       fromfile=b"a/" + p, tofile=b"b/" + p, n=3))
    return b"diff --git a/" + p + b" b/" + p + b"\n" + header + body


@NEED_SRC
class RuleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.maps = Maps(str(MAPS))
        cls.index = {tid: i for i, tid in enumerate(cls.maps.ids)}
        cls.src = tempfile.mkdtemp(prefix="br-tpc-src-")
        for path in ("tests/__init__.py", "tests/helpers.py", "tests/test_executor.py", "tests/test_optimizer.py",
                     TPCH, TPCDS):
            dest = os.path.join(cls.src, path)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copyfile(SRC / path, dest)
        cls.old = {p: (SRC / p).read_bytes() for p in (TPCH, TPCDS)}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.src, ignore_errors=True)

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"BR_TPC_FIXTURE": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_patch(self, raw, src=None):
        res = impact.commit_impact(parse(raw), src or self.src, self.maps)
        return {self.maps.ids[i] for i in range(len(self.maps.ids)) if res.bits >> i & 1}, res

    def readers(self, path):
        bits = self.maps.reads(path)
        return {self.maps.ids[i] for i in range(len(self.maps.ids)) if bits >> i & 1}

    def edit(self, path, old, new, count=1):
        data = self.old[path]
        self.assertIn(old, data)
        return diff(path, data, data.replace(old, new, count))

    def expect(self, raw, kept, path=TPCDS):
        """`kept` is the expected selection, or None for every reader (fallback)."""
        got, res = self.run_patch(raw)
        if kept is None:
            self.assertEqual(got, self.readers(path))
            self.assertIn("narrowing off", " ".join(res.notes))
        else:
            self.assertEqual(got, set(kept))
            self.assertIn("TPC fixture: dropped", " ".join(res.notes))
        self.assertFalse(res.replica_ok)
        self.assertTrue(res.nontrivial)
        return res

    def test_readers_are_the_class_and_one_optimizer_test(self):
        for path, opt in ((TPCDS, OPT_TPCDS), (TPCH, OPT_TPCH)):
            readers = self.readers(path)
            self.assertIn(opt, readers)
            self.assertEqual(len({r for r in readers if r.startswith(tpc_fixture.CLASS)}), 60)

    def test_flag_off_is_unchanged(self):
        raw = self.edit(TPCDS, b"LIMIT 100;\nWITH", b"LIMIT 100 ;\nWITH")
        with mock.patch.dict(os.environ):
            os.environ.pop("BR_TPC_FIXTURE")
            got, res = self.run_patch(raw)
        self.assertEqual(got, self.readers(TPCDS))
        self.assertEqual(res.notes, [f"{TPCDS}: data readers"])

    def test_tpcds_expected_only(self):
        # the optimized statement of query 1: test_execute_tpcds never reads it
        raw = self.edit(TPCDS, b'AVG("ctr2"."ctr_total_return") * 1.2', b'AVG("ctr2"."ctr_total_return") * 1.3')
        self.expect(raw, {OPT_TPCDS})

    def test_tpcds_comment_and_whitespace(self):
        self.expect(self.edit(TPCDS, b"-- TPC-DS 1\n", b"-- TPC-DS one\n"), {OPT_TPCDS})
        self.expect(self.edit(TPCDS, b"LIMIT 100;\n", b"LIMIT 100;   \n\n"), {OPT_TPCDS})

    def test_tpcds_sql_or_meta_change(self):
        self.expect(self.edit(TPCDS, b"AND d_year = 2001", b"AND d_year = 2002"), {OPT_TPCDS, tpc_fixture.EXECUTE_TPCDS})
        self.expect(self.edit(TPCDS, b"# execute: true\n", b""), {OPT_TPCDS, tpc_fixture.EXECUTE_TPCDS})
        self.expect(self.edit(TPCDS, b"# execute: true\n", b"# execute: true\n# other: x\n"),
                    {OPT_TPCDS, tpc_fixture.EXECUTE_TPCDS})
        # an extra pair, a removed pair (one more ";" shifts every later pair)
        data = self.old[TPCDS]
        self.expect(diff(TPCDS, data, data + b"SELECT 1;\nSELECT 1;\n"), {OPT_TPCDS, tpc_fixture.EXECUTE_TPCDS})
        self.expect(self.edit(TPCDS, b"LIMIT 100;\nWITH", b"LIMIT 100;;\nWITH"), {OPT_TPCDS, tpc_fixture.EXECUTE_TPCDS})

    def test_tpcds_loader_failure_or_collision_falls_back(self):
        self.expect(self.edit(TPCDS, b"# execute: true\n", b"# execute true\n"), None)
        self.expect(self.edit(TPCDS, b"-- TPC-DS 1\n", b"-- TPC-DS 1\n# a: b\n;\n"), None)
        self.expect(self.edit(TPCDS, b"LIMIT 100;\n", "LIMIT 100; -- \udcff\n".encode("utf-8", "surrogateescape")), None)
        tpch_sql = tpc_fixture.load_pairs(self.old[TPCH])[0][1].encode()
        data = self.old[TPCDS]
        self.expect(diff(TPCDS, data, data + tpch_sql + b";\nSELECT 1;\n"), None)

    def test_tpch_rules(self):
        h = (TPCH,)
        # an optimized statement only: test_execute_tpch never reads it, and no tpc-h meta executes
        first_expected = tpc_fixture.load_pairs(self.old[TPCH])[0][2].encode()
        line = first_expected.split(b"\n")[1] + b"\n"
        self.expect(self.edit(TPCH, line, line.replace(b"AS", b"AS ")), {OPT_TPCH, tpc_fixture.OPTIMIZED_TPCH}, *h)
        # the sql of a query: both tpc-h executor tests
        self.expect(self.edit(TPCH, b"        l_returnflag,\n        l_linestatus,\n        sum(l_quantity)",
                              b"        l_linestatus,\n        l_returnflag,\n        sum(l_quantity)"),
                    {OPT_TPCH, tpc_fixture.OPTIMIZED_TPCH, tpc_fixture.EXECUTE_TPCH}, *h)
        # a meta line that makes test_execute_tpch use tpch_conn and the cache: both
        self.expect(self.edit(TPCH, b"-- TPC-H 1\n--------------------------------------\n",
                              b"-- TPC-H 1\n--------------------------------------\n# execute: true\n"),
                    {OPT_TPCH, tpc_fixture.OPTIMIZED_TPCH, tpc_fixture.EXECUTE_TPCH}, *h)
        # a meta line that does not execute: test_optimized_tpch ignores meta
        self.expect(self.edit(TPCH, b"-- TPC-H 1\n--------------------------------------\n",
                              b"-- TPC-H 1\n--------------------------------------\n# note: x\n"),
                    {OPT_TPCH, tpc_fixture.EXECUTE_TPCH}, *h)
        self.expect(self.edit(TPCH, b"-- TPC-H 1\n", b"-- TPC-H one\n"), {OPT_TPCH}, *h)
        self.expect(self.edit(TPCH, b"-- TPC-H 1\n", b"#bad\n"), None, *h)

    def test_commit_level_blockers(self):
        fixture = self.edit(TPCDS, b"-- TPC-DS 1\n", b"-- TPC-DS one\n")
        tpch = self.edit(TPCH, b"-- TPC-H 1\n", b"-- TPC-H one\n")
        helpers = (SRC / "tests" / "helpers.py").read_bytes()
        others = [
            diff("tests/helpers.py", helpers, helpers.replace(b"import os\n", b"import os\nX = 1\n")),
            diff("tests/test_optimizer.py", b"a\n", b"b\n"),
            diff("tests/dialects/test_duckdb.py", b"a\n", b"b\n"),
            diff("setup.cfg", b"a\n", b"b\n"),
            diff(".gitattributes", b"a\n", b"b\n"),
            diff("tests/fixtures/optimizer/tpc-ds/store.csv.gz", b"a\n", b"b\n"),
            tpch,
            fixture,  # the same file twice
        ]
        for other in others:
            with self.subTest(other=other[:60]):
                try:
                    got, res = self.run_patch(fixture + other)
                except impact.Everything:
                    continue
                self.assertTrue(self.readers(TPCDS) <= got)
                self.assertIn("narrowing off", " ".join(res.notes))
        # an unrelated file does not block (its own readers are added on top)
        got, res = self.run_patch(fixture + diff("README.md", b"a\n", b"b\n"))
        self.assertIn("TPC fixture: dropped", " ".join(res.notes))

    def test_pinned_tests_differ(self):
        src = tempfile.mkdtemp(prefix="br-tpc-src2-")
        self.addCleanup(shutil.rmtree, src, True)
        shutil.copytree(self.src, src, dirs_exist_ok=True)
        with open(os.path.join(src, "tests", "test_executor.py"), "ab") as fh:
            fh.write(b"\n")
        got, res = self.run_patch(self.edit(TPCDS, b"-- TPC-DS 1\n", b"-- TPC-DS one\n"), src)
        self.assertEqual(got, self.readers(TPCDS))
        self.assertIn("not the pinned file", " ".join(res.notes))

    def test_patch_shapes_that_fall_back(self):
        data = self.old[TPCDS]
        good = self.edit(TPCDS, b"-- TPC-DS 1\n", b"-- TPC-DS one\n")
        header = b"@@ -1,5 +1,5 @@"
        self.assertIn(header, good)
        shapes = [
            good.replace(b"index 1111111..2222222 100644\n", b"old mode 100644\nnew mode 100755\n"),
            good.replace(b"index 1111111..2222222 100644\n", b"index 1111111..2222222 100755\n"),
            good.replace(header, b"@@ -2,5 +2,5 @@"),  # misplaced
            good.replace(header, b"@@ -1,5 +1,6 @@"),  # line counts disagree
            good.replace(header, b"@@ -1,4 +1,4 @@"),
            good.replace(b"\n", b"\r\n"),
            diff(TPCDS, data, data.replace(b"\n", b"\r\n", 3)),
            good.replace(b"diff --git a/", b"diff --git x/").replace(b"--- a/", b"--- x/"),
        ]
        for raw in shapes:
            with self.subTest(raw=raw[:200]):
                try:
                    got, res = self.run_patch(raw)
                except impact.Everything:
                    continue
                self.assertTrue(self.readers(TPCDS) <= got)
                self.assertNotIn("TPC fixture: dropped", " ".join(res.notes))

    def test_hunk_without_trailing_context_must_end_the_file(self):
        fp = parse(self.edit(TPCDS, b"-- TPC-DS 1\n", b"-- TPC-DS one\n"))[0]
        self.assertIsNotNone(tpc_fixture._plain_new(fp, self.old[TPCDS]))
        old = b"a\nb\nc\nd\ne\nb\nc\n"
        raw = b"diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,3 +1,3 @@\n a\n b\n-c\n+C\n"
        self.assertIsNone(tpc_fixture._plain_new(parse(raw)[0], old))
        raw = b"diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -5,3 +5,3 @@\n e\n b\n-c\n+C\n"
        self.assertEqual(tpc_fixture._plain_new(parse(raw)[0], old), b"a\nb\nc\nd\ne\nb\nC\n")

    def test_all_pinned_queries_are_plain_select_or_with(self):
        for path in (TPCH, TPCDS):
            for _, sql, exp_sql in tpc_fixture.load_pairs(self.old[path]):
                self.assertTrue(tpc_fixture._is_plain_query(sql), f"{path} sql: {sql[:80]}")
                self.assertTrue(tpc_fixture._is_plain_query(exp_sql), f"{path} exp: {exp_sql[:80]}")

    def test_non_plain_query_falls_back(self):
        # Suspected miss 1 from review-tpc: COPY ... TO overwrites a test file on real duckdb.
        first_expected = tpc_fixture.load_pairs(self.old[TPCH])[0][2].encode()
        evil = b"COPY (SELECT 'x(' AS a) TO 'tests/test_executor.py' (HEADER false)"
        self.expect(self.edit(TPCH, first_expected, evil), None, TPCH)
        # Also block PRAGMA / ATTACH / DROP / INSERT / CREATE in tpc-h expected or tpc-ds sql.
        for bad in (
            b"PRAGMA disable_progress_bar",
            b"ATTACH '/tmp/x.db' AS x",
            b"DROP TABLE lineitem",
            b"WITH x AS (DELETE FROM lineitem) SELECT * FROM x",
            b"SELECT 'unclosed",
        ):
            with self.subTest(bad=bad):
                self.assertFalse(tpc_fixture._is_plain_query(bad.decode()))
        self.expect(self.edit(TPCDS, b"WITH customer_total_return\n", b"COPY (SELECT 1) TO '/tmp/x' --\n"), None, TPCDS)

    def test_pycache_under_tests_falls_back(self):
        fixture = self.edit(TPCDS, b"-- TPC-DS 1\n", b"-- TPC-DS one\n")
        pyc = diff("tests/__pycache__/helpers.cpython-313.pyc", b"a\n", b"b\n")
        got, res = self.run_patch(fixture + pyc)
        self.assertTrue(self.readers(TPCDS) <= got)
        self.assertIn("narrowing off", " ".join(res.notes))


if __name__ == "__main__":
    unittest.main()
