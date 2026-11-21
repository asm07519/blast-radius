"""selector/overlay.py: layout, changed content, bytecode, refusals and safe removal.

    python3 -m unittest tests.test_overlay
"""

import glob
import os
import pkgutil
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from selector import overlay  # noqa: E402

PYTHON = os.environ.get("BR_MODE0_PYTHON") or sys.executable

FILES = {
    "sqlglot/__init__.py": "from sqlglot.core import value\n",
    "sqlglot/core.py": "def value():\n    return 1\n",
    "sqlglot/sub/__init__.py": "",
    "sqlglot/sub/deep/__init__.py": "",
    "sqlglot/sub/deep/leaf.py": "X = 1\n",
    "sqlglot/data.json": "{}\n",
    "tests/__init__.py": "",
    "tests/test_a.py": "import unittest\n",
    "tests/fixtures/a/one.sql": "SELECT 1\n",
    "tests/fixtures/b/two.sql": "SELECT 2\n",
    "setup.py": "# setup\n",
    "docs/index.md": "# docs\n",
}


def tree(top):
    """Relative paths of everything os.walk sees (dirs end with /)."""
    out = []
    for root, dirs, names in os.walk(top):
        rel = os.path.relpath(root, top)
        out += [os.path.normpath(os.path.join(rel, d)) + "/" for d in dirs]
        out += [os.path.normpath(os.path.join(rel, n)) for n in names]
    return sorted(out)


class Base(unittest.TestCase):
    def setUp(self):
        base = os.path.join(ROOT, "work")
        os.makedirs(base, exist_ok=True)
        self.tmp = tempfile.mkdtemp(prefix="ov-test-", dir=base)
        self.src = os.path.join(self.tmp, "repo")
        for rel, text in FILES.items():
            path = os.path.join(self.src, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write(text)
        os.symlink("setup.py", os.path.join(self.src, "LINK.md"))
        subprocess.run([PYTHON, "-m", "compileall", "-q", os.path.join(self.src, "sqlglot")],
                       check=True, env={k: v for k, v in os.environ.items() if k != "SOURCE_DATE_EPOCH"})
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.work)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, changes=None, **kw):
        return overlay.build(self.src, changes or {}, self.work, **kw)


class TestLayout(Base):
    def test_mirror_structure(self):
        root = self.build()
        for rel in ("sqlglot", "sqlglot/sub/deep", "tests/fixtures/a", "sqlglot/__pycache__"):
            st = os.lstat(os.path.join(root, rel))
            self.assertTrue(stat.S_ISDIR(st.st_mode), rel)  # a real directory, not a link
        for rel in ("sqlglot/core.py", "tests/fixtures/a/one.sql", "sqlglot/data.json"):
            path = os.path.join(root, rel)
            self.assertTrue(os.path.islink(path), rel)
            self.assertEqual(os.readlink(path), os.path.join(os.path.realpath(self.src), rel))
        for rel in ("setup.py", "docs", "LINK.md"):  # other top-level entries: plain links
            self.assertTrue(os.path.islink(os.path.join(root, rel)), rel)
        self.assertEqual(sorted(os.listdir(root)), sorted(os.listdir(self.src)))
        self.assertEqual(os.path.basename(root), "repo")

    def test_walk_glob_iter_modules_equivalence(self):
        root = self.build()
        for d in ("sqlglot", "tests"):
            self.assertEqual(tree(os.path.join(root, d)), tree(os.path.join(self.src, d)))
            self.assertEqual(
                sorted(os.path.relpath(p, root) for p in glob.glob(os.path.join(root, d, "**"), recursive=True)),
                sorted(os.path.relpath(p, self.src) for p in glob.glob(os.path.join(self.src, d, "**"), recursive=True)))
            self.assertEqual(sorted(str(p.relative_to(root)) for p in Path(root, d).rglob("*")),
                             sorted(str(p.relative_to(self.src)) for p in Path(self.src, d).rglob("*")))
        for sub in ("sqlglot", "sqlglot/sub"):
            self.assertEqual(sorted(m.name for m in pkgutil.iter_modules([os.path.join(root, sub)])),
                             sorted(m.name for m in pkgutil.iter_modules([os.path.join(self.src, sub)])))

    def test_changed_content_wins_and_pyc_not_mirrored(self):
        new = b"def value():\n    return 2\n"
        root = self.build({"sqlglot/core.py": new, "tests/fixtures/a/one.sql": b"SELECT 9\n"})
        core = os.path.join(root, "sqlglot", "core.py")
        self.assertFalse(os.path.islink(core))
        with open(core, "rb") as fh:
            self.assertEqual(fh.read(), new)
        with open(os.path.join(root, "tests/fixtures/a/one.sql"), "rb") as fh:
            self.assertEqual(fh.read(), b"SELECT 9\n")
        with open(os.path.join(self.src, "sqlglot", "core.py"), "rb") as fh:
            self.assertEqual(fh.read(), FILES["sqlglot/core.py"].encode())  # pinned untouched
        cache = os.listdir(os.path.join(root, "sqlglot", "__pycache__"))
        self.assertFalse([n for n in cache if n.startswith("core.")], cache)
        self.assertTrue([n for n in cache if n.startswith("__init__.")], cache)
        info = overlay.info(root)
        self.assertEqual(info["shadowed"], sorted(os.path.join(os.path.realpath(self.src), r)
                                                  for r in ("sqlglot/core.py", "tests/fixtures/a/one.sql")))
        fields = overlay.spec_fields(root)
        self.assertEqual(fields["src"], root)
        self.assertEqual(fields["patched"], {})
        self.assertEqual(fields["overlay"], "fs")
        # Imports from the overlay use the patched source and no stale bytecode, even an
        # unchecked-hash pyc that would never be validated.
        subprocess.run([PYTHON, "-m", "compileall", "-q", "--invalidation-mode", "unchecked-hash",
                        os.path.join(self.src, "sqlglot", "core.py")], check=True)
        root2 = self.build({"sqlglot/core.py": new})
        out = subprocess.run([PYTHON, "-S", "-B", "-c",
                              "import sys; sys.path.insert(0, %r); import sqlglot, sqlglot.sub.deep.leaf;"
                              " print(sqlglot.value(), sqlglot.core.__cached__ is not None)" % root2],
                             capture_output=True, text=True)
        self.assertEqual(out.stdout.split()[0], "2", out.stderr)

    def test_unchanged_pyc_is_used(self):
        root = self.build({"sqlglot/core.py": b"def value():\n    return 2\n"})
        code = ("import sys; sys.path.insert(0, %r); import sqlglot.sub.deep.leaf as m;"
                " print(m.__spec__.cached)" % root)
        out = subprocess.run([PYTHON, "-S", "-B", "-v", "-c", code], capture_output=True, text=True)
        self.assertIn("matches " + os.path.join(root, "sqlglot", "sub", "deep", "leaf.py"), out.stderr)

    def test_new_top_level_dir_change_is_mirrored(self):
        root = self.build({"docs/index.md": b"# new\n", "setup.py": b"# new setup\n"})
        self.assertTrue(stat.S_ISDIR(os.lstat(os.path.join(root, "docs")).st_mode))
        self.assertFalse(os.path.islink(os.path.join(root, "docs", "index.md")))
        self.assertFalse(os.path.islink(os.path.join(root, "setup.py")))
        with open(os.path.join(root, "setup.py"), "rb") as fh:
            self.assertEqual(fh.read(), b"# new setup\n")

    def test_mode_preserved(self):
        os.chmod(os.path.join(self.src, "setup.py"), 0o755)
        root = self.build({"setup.py": b"#!/bin/sh\n"})
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(root, "setup.py")).st_mode), 0o755)


class TestRefusals(Base):
    def assertRefused(self, changes, **kw):
        with self.assertRaises(overlay.OverlayError):
            self.build(changes, **kw)
        self.assertEqual(os.listdir(self.work), [])  # nothing left behind

    def test_path_escapes(self):
        for rel in ("../evil.py", "/etc/passwd", "sqlglot/../setup.py", "sqlglot//core.py",
                    "./setup.py", "sqlglot/core.py/", "a\0b", "sqlglot\\core.py", ""):
            self.assertRefused({rel: b"x"})
        os.symlink(os.path.join(self.src, "sqlglot"), os.path.join(self.src, "alias"))
        self.assertRefused({"alias/core.py": b"x"})  # crosses a symlink

    def test_deletion_and_addition_not_supported(self):
        self.assertRefused({"sqlglot/core.py": None})
        self.assertRefused({"sqlglot/new.py": b"x"})
        self.assertRefused({"sqlglot/sub": b"x"})  # a directory
        self.assertRefused({"sqlglot/core.py": "text, not bytes"})

    def test_oversize(self):
        self.assertRefused({"sqlglot/core.py": b"x" * 2000}, max_bytes=1000)
        self.assertRefused({}, max_entries=5)

    def test_no_space(self):
        self.assertRefused({"sqlglot/core.py": b"x"}, reserve=1 << 62)

    def test_unexpected_entries(self):
        os.symlink("core.py", os.path.join(self.src, "sqlglot", "alias.py"))
        self.assertRefused({})
        os.remove(os.path.join(self.src, "sqlglot", "alias.py"))
        fifo = os.path.join(self.src, "tests", "fifo")
        os.mkfifo(fifo)
        self.assertRefused({})
        os.remove(fifo)
        sock, cwd = socket.socket(socket.AF_UNIX), os.getcwd()
        try:
            os.chdir(os.path.join(self.src, "tests"))
            sock.bind("s")  # relative: AF_UNIX paths are short
            self.assertRefused({})
        finally:
            os.chdir(cwd)
            sock.close()
            os.remove(os.path.join(self.src, "tests", "s"))
        os.symlink("LOOP", os.path.join(self.src, "LOOP"))
        self.assertRefused({})

    def test_workdir_inside_src(self):
        with self.assertRaises(overlay.OverlayError):
            overlay.build(self.src, {}, os.path.join(self.src, "docs"))


class TestRemove(Base):
    def test_remove_never_follows_links(self):
        victim = os.path.join(self.tmp, "victim")
        os.makedirs(victim)
        with open(os.path.join(victim, "keep"), "w") as fh:
            fh.write("x")
        root = self.build()
        os.symlink(victim, os.path.join(root, "sqlglot", "planted"))  # e.g. made by a test
        overlay.remove(root)
        self.assertFalse(os.path.exists(os.path.dirname(root)))
        self.assertTrue(os.path.isfile(os.path.join(victim, "keep")))
        self.assertTrue(os.path.isfile(os.path.join(self.src, "sqlglot", "core.py")))
        self.assertEqual(os.listdir(self.work), [])

    def test_remove_refuses_non_overlays(self):
        with self.assertRaises(overlay.OverlayError):
            overlay.remove(self.src)
        root = self.build()
        link = os.path.join(self.tmp, "br-ov-link")
        os.symlink(os.path.dirname(root), link)
        with self.assertRaises(overlay.OverlayError):
            overlay.remove(os.path.join(link, "repo"))  # container reached through a link
        overlay.remove(root)
        self.assertTrue(os.path.isdir(os.path.join(self.src, "sqlglot")))


if __name__ == "__main__":
    unittest.main()
