"""Patch paths that are not in git's normal form select every test instead of matching nothing.

Run from the repository root:  python3 -m unittest discover -s tests -t .
"""

import os
import shutil
import tempfile
import unittest
import unittest.mock

from selector import impact
from selector.patch import parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(ROOT, "work")


class Maps:
    """Just enough of selector.maps.Maps: every lookup by the normal spelling finds tests."""

    has_class_data = False
    typeprof_incomplete = None
    untrusted = 0
    class_info = {}
    names = {}
    files = ["sqlglot/dialects/tsql.py"]

    def imported(self, path):
        return 0b1111 if path == "sqlglot/dialects/tsql.py" else 0

    def call(self, path, lines):
        return 0b0011 if path == "sqlglot/dialects/tsql.py" else 0

    def reads(self, path, linecache=True):
        return 0b0100 if path == "tests/fixtures/x.sql" else 0

    def listers(self, directory):
        return 0

    def swap_is_noop(self, path, proof):
        return False


OLD = "def f():\n    return 1\n"


def patch_for(old_path, new_path, old=OLD, new=OLD.replace("1", "2")):
    return (f"diff --git a/{old_path} b/{new_path}\n--- a/{old_path}\n+++ b/{new_path}\n"
            f"@@ -1,2 +1,2 @@\n def f():\n-    return 1\n+    return 2\n").encode()


class NormalPathTest(unittest.TestCase):
    def test_normal(self):
        for p in ("sqlglot/dialects/tsql.py", "setup.py", "tests/fixtures/x.sql", "a b/c.py", ".github/x.yml",
                  "sqlglot/..x.py", "x..y"):
            with self.subTest(p=p):
                self.assertTrue(impact.normal_path(p))

    def test_not_normal(self):
        for p in ("sqlglot//dialects/tsql.py", "./sqlglot/dialects/tsql.py", "sqlglot/./dialects/tsql.py",
                  "sqlglot/dialects/../dialects/tsql.py", "../sqlglot/x.py", "/sqlglot/x.py", "//sqlglot/x.py",
                  "sqlglot\\dialects\\tsql.py", "sqlglot/dialects/", "sqlglot/", ".", "..", "", "a\0b"):
            with self.subTest(p=p):
                self.assertFalse(impact.normal_path(p))


class CommitImpactPathTest(unittest.TestCase):
    def setUp(self):
        os.makedirs(WORK, exist_ok=True)
        self.src = tempfile.mkdtemp(prefix="test-patch-paths-", dir=WORK)
        self.addCleanup(shutil.rmtree, self.src, True)
        os.makedirs(os.path.join(self.src, "sqlglot", "dialects"))
        with open(os.path.join(self.src, "sqlglot", "dialects", "tsql.py"), "w") as fh:
            fh.write(OLD)

    def impact(self, data):
        return impact.commit_impact(parse(data), self.src, Maps())

    def test_normal_spelling_selects_the_callers(self):
        res = self.impact(patch_for("sqlglot/dialects/tsql.py", "sqlglot/dialects/tsql.py"))
        self.assertEqual(res.bits, 0b0011)

    def test_unnormalised_spellings_select_everything(self):
        # before: 0 tests (every map lookup missed), a silent miss
        for p in ("sqlglot//dialects/tsql.py", "./sqlglot/dialects/tsql.py", "sqlglot/./dialects/tsql.py",
                  "sqlglot/dialects/../dialects/tsql.py"):
            for old_path, new_path in ((p, p), (p, "sqlglot/dialects/tsql.py"), ("sqlglot/dialects/tsql.py", p)):
                with self.subTest(old=old_path, new=new_path):
                    with self.assertRaises(impact.Everything):
                        self.impact(patch_for(old_path, new_path))

    def test_unnormalised_new_and_deleted_files(self):
        added = b"diff --git a/sqlglot//new.py b/sqlglot//new.py\nnew file mode 100644\n--- /dev/null\n" \
                b"+++ b/sqlglot//new.py\n@@ -0,0 +1 @@\n+X = 1\n"
        deleted = b"diff --git a/./sqlglot/dialects/tsql.py b/./sqlglot/dialects/tsql.py\ndeleted file mode 100644\n" \
                  b"--- a/./sqlglot/dialects/tsql.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-def f():\n-    return 1\n"
        for data in (added, deleted):
            with self.subTest(data=data[:40]):
                with self.assertRaises(impact.Everything):
                    self.impact(data)

    def test_one_bad_path_among_good_ones(self):
        data = patch_for("sqlglot/dialects/tsql.py", "sqlglot/dialects/tsql.py") + \
            patch_for("sqlglot//dialects/tsql.py", "sqlglot//dialects/tsql.py")
        with self.assertRaises(impact.Everything):
            self.impact(data)


class StrictPathsTest(unittest.TestCase):
    """BR_STRICT_PATHS=1: names git apply -p1 could read differently select every test."""

    BODY = "@@ -1,2 +1,2 @@\n def f():\n-    return 1\n+    return 2\n"

    def odd(self, text):
        return [fp.odd for fp in parse(text.encode())]

    def test_plain_names_are_not_odd(self):
        p = "sqlglot/dialects/tsql.py"
        self.assertEqual(self.odd(f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n" + self.BODY), [None])
        self.assertEqual(self.odd(f"--- a/{p}\t2026-01-01\n+++ b/{p}\t2026-01-01\n" + self.BODY), [None])
        self.assertEqual(self.odd(f"diff --git a/{p} b/{p}\nnew file mode 100644\n--- /dev/null\n+++ b/{p}\n"
                                  + "@@ -0,0 +1 @@\n+x = 1\n"), [None])
        self.assertEqual(self.odd(f"diff --git a/{p} b/x.py\nrename from {p}\nrename to x.py\n"
                                  f"--- a/{p}\n+++ b/x.py\n" + self.BODY), [None])

    def test_odd_names(self):
        p = "sqlglot/dialects/tsql.py"
        for text in (
            f"diff --git x/{p} y/{p}\n--- x/{p}\n+++ y/{p}\n" + self.BODY,
            f"diff --git {p} {p}\n--- {p}\n+++ {p}\n" + self.BODY,
            f"--- x/{p}\n+++ y/{p}\n" + self.BODY,
            f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}   \n" + self.BODY,
            f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/sqlglot/parser.py\n" + self.BODY,
        ):
            with self.subTest(text=text.splitlines()[:3]):
                self.assertTrue(self.odd(text)[0])

    def test_flag(self):
        src = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, src, True)
        p = "sqlglot/dialects/tsql.py"
        fps = parse((f"diff --git x/{p} y/{p}\n--- x/{p}\n+++ y/{p}\n" + self.BODY).encode())
        missing = parse((f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n" + self.BODY).encode())
        with unittest.mock.patch.dict(os.environ, {"BR_STRICT_PATHS": "1"}):
            for case, word in ((fps, "a/ and b/"), (missing, "does not have")):
                with self.assertRaises(impact.Everything) as caught:
                    impact.commit_impact(case, src, Maps())
                self.assertIn(word, str(caught.exception))
        with unittest.mock.patch.dict(os.environ):
            os.environ.pop("BR_STRICT_PATHS", None)
            self.assertFalse(impact.strict_paths())


if __name__ == "__main__":
    unittest.main()
