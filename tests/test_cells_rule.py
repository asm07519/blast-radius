"""Unit tests for BR_CELLS selection rule (selector/cells_rule.py) with fake maps and diffs."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from selector import cells_rule, impact
from selector.patch import parse
from selector.state_probe import StateDiff


class FakeMaps:
    def __init__(self, has_cells=False):
        self.ids = ["t0", "t1", "t2", "t3", "t4"]
        self.files = ["sqlglot/mod.py", "sqlglot/other.py"]
        self.file_index = {f: i for i, f in enumerate(self.files)}
        self.untrusted = 0
        self.has_class_data = True
        self.has_cells = has_cells
        self.cells_untrusted = 0b10000 if has_cells else 0
        self.class_info = {
            "sqlglot.mod:Foo": (True, ("sqlglot.mod:Foo", "builtins:object")),
            "sqlglot.mod:Sub": (True, ("sqlglot.mod:Sub", "sqlglot.mod:Foo", "builtins:object")),
            "sqlglot.mod:Hidden": (False, ("sqlglot.mod:Hidden", "builtins:object")),
        }
        self.classes_at_import = frozenset()
        self.names = {}
        self.typeprof_incomplete = None
        self._imported = {"sqlglot/mod.py": 0b11111, "sqlglot/other.py": 0b11111}
        self._class_users = {"sqlglot.mod:Foo": 0b00111, "sqlglot.mod:Sub": 0b00010}
        self._name_readers = {"TABLE": 0b01011, "GLOB": 0b00101, "Foo": 0, "Sub": 0}
        self._dyn_readers = {}
        self._tracked = {("c", "sqlglot.mod:Foo", "TABLE"), ("g", "sqlglot.mod", "GLOB")}
        self._key_readers = {
            ("c", "sqlglot.mod:Foo", "TABLE"): {"s:A": 0b00001, "s:B": 0b00010},
            ("g", "sqlglot.mod", "GLOB"): {"s:K": 0b00100},
        }
        self._whole_readers = {"TABLE": 0b00000, "GLOB": 0b00000}

    def file_known(self, path):
        return path in self.file_index

    def imported(self, path):
        return self._imported.get(path, 0)

    def reads(self, path, linecache=True):
        return 0

    def call(self, path, lines):
        return 0

    def class_users(self, key):
        return self._class_users.get(key, 0)

    def name_readers(self, name):
        return self._name_readers.get(name, 0)

    def dynamic_readers(self, name, family_key=None):
        return self._dyn_readers.get((family_key, name), 0)

    def cell_tracked(self, cell):
        return cell in self._tracked

    def key_readers(self, cell, kf):
        return self._key_readers.get(cell, {}).get(kf, 0)

    def whole_readers(self, name):
        return self._whole_readers.get(name, 0)

    def dyn_name_readers(self, name, family_key=None):
        return self._dyn_readers.get((family_key, name), 0)


def _patch(path, old_line, new_line):
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -1,3 +1,3 @@\n"
        f" class Foo:\n"
        f"-{old_line}\n"
        f"+{new_line}\n"
        f"     pass\n"
    ).encode()


class CellsRuleTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        p = Path(self.td.name) / "sqlglot" / "mod.py"
        p.parent.mkdir(parents=True)
        p.write_bytes(b"class Foo:\n    TABLE = {'A': 1}\n    pass\n")

    def run_impact(self, raw, maps, diff):
        called = []

        def fake_probe(patched):
            called.append(patched)
            return diff

        with mock.patch.dict(os.environ, {"BR_CELLS": "1"}):
            res = impact.commit_impact(parse(raw), self.td.name, maps, state_probe=fake_probe)
        return res, called

    def test_tracked_class_dict_key_narrowing(self):
        maps = FakeMaps(has_cells=True)
        diff = StateDiff(True, "compared", cells={
            ("c", "sqlglot.mod:Foo", "TABLE"): {"kind": "dict", "keys": ["s:A"], "order": False}
        })
        raw = _patch("sqlglot/mod.py", "    TABLE = {'A': 1}", "    TABLE = {'A': 2}")
        res, called = self.run_impact(raw, maps, diff)
        self.assertEqual(len(called), 1)
        # key_readers("s:A") = 0b00001 | cells_untrusted = 0b10000
        self.assertEqual(res.bits, 0b10001)
        self.assertIn("cells narrowing kept", res.notes[0])

    def test_untracked_or_no_cells_bin_uses_name_and_class_users(self):
        maps = FakeMaps(has_cells=False)
        diff = StateDiff(True, "compared", cells={
            ("c", "sqlglot.mod:Foo", "TABLE"): {"kind": "dict", "keys": ["s:A"], "order": False}
        })
        raw = _patch("sqlglot/mod.py", "    TABLE = {'A': 1}", "    TABLE = {'A': 2}")
        res, _ = self.run_impact(raw, maps, diff)
        # users(Foo)=0b00111 & name_readers(TABLE)=0b01011 -> 0b00011
        self.assertEqual(res.bits, 0b00011)

    def test_env_changed_or_dunder_or_at_cell_falls_back(self):
        maps = FakeMaps(has_cells=True)
        raw = _patch("sqlglot/mod.py", "    TABLE = {'A': 1}", "    TABLE = {'A': 2}")
        bad_diffs = [
            StateDiff(False, "import failed"),
            StateDiff(True, "compared", cells={}, env_changed=["environ"]),
            StateDiff(True, "compared", cells={}, new_modules=["sqlglot.x"]),
            StateDiff(True, "compared", cells={}, opaque=["x"]),
            StateDiff(True, "compared", cells={}, warm_caches=["x"]),
            StateDiff(True, "compared", cells={("c", "sqlglot.mod:Foo", "__bases__"): {"kind": "seq"}}),
            StateDiff(True, "compared", cells={("c", "sqlglot.mod:Foo", "__eq__"): {"kind": "func"}}),
            StateDiff(True, "compared", cells={("c", "sqlglot.mod:Foo@anchor", "TABLE"): {"kind": "dict"}}),
            StateDiff(True, "compared", cells={("c", "sqlglot.mod:Hidden", "TABLE"): {"kind": "dict"}}),
        ]
        for d in bad_diffs:
            with self.subTest(diff=d):
                res, _ = self.run_impact(raw, maps, d)
                self.assertEqual(res.bits, 0b11111)
                self.assertIn("cells narrowing dropped", res.notes[0])

    def test_unused_global_selects_zero(self):
        maps = FakeMaps(has_cells=False)
        (Path(self.td.name) / "sqlglot" / "mod.py").write_bytes(b"X = 1\n")
        raw = (
            b"diff --git a/sqlglot/mod.py b/sqlglot/mod.py\n"
            b"--- a/sqlglot/mod.py\n+++ b/sqlglot/mod.py\n"
            b"@@ -1,1 +1,2 @@\n X = 1\n+_UNUSED = 2\n"
        )
        diff = StateDiff(True, "compared", cells={
            ("g", "sqlglot.mod", "_UNUSED"): {"kind": "other", "keys": None, "added": True}
        })
        res, _ = self.run_impact(raw, maps, diff)
        self.assertEqual(res.bits, 0)
        self.assertIn("cells narrowing kept", res.notes[0])

    def test_flag_off_does_not_call_state_probe(self):
        maps = FakeMaps(has_cells=True)
        called = []
        raw = _patch("sqlglot/mod.py", "    TABLE = {'A': 1}", "    TABLE = {'A': 2}")
        with mock.patch.dict(os.environ):
            os.environ.pop("BR_CELLS", None)
            res = impact.commit_impact(parse(raw), self.td.name, maps, state_probe=lambda p: called.append(p))
        self.assertEqual(called, [])
        self.assertEqual(res.bits, 0b11111)


if __name__ == "__main__":
    unittest.main()
