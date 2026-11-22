"""Import-state diff (selector/state_probe.py) and key fingerprints (selector/keyfp.py).

Run from the repository root:  python3 -m unittest discover -s tests -t .
The end-to-end tests need the pinned sqlglot checkout: BR_SAMPLE_SRC=<absolute path>, else
work/sqlglot next to this repository (or of the main checkout when run from a worktree).
"""

import enum
import os
import sys
import tempfile
import unittest
from collections import namedtuple

from selector import import_probe, state_probe
from selector.keyfp import key_fp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _pinned_src():
    env = os.environ.get("BR_SAMPLE_SRC")
    if env:
        return os.path.abspath(env)
    for cand in (os.path.join(ROOT, "work", "sqlglot"),
                 os.path.join(ROOT.split(os.sep + ".claude" + os.sep)[0], "work", "sqlglot")):
        if os.path.isdir(os.path.join(cand, "sqlglot")):
            return cand
    return None


SRC = _pinned_src()


class Color(enum.Enum):
    RED = 1


class Mixed(str, enum.Enum):
    A = "a"


class Num(enum.IntEnum):
    ONE = 1


class Odd(enum.Enum):
    X = 1

    def __eq__(self, other):
        return self is other

    __hash__ = enum.Enum.__hash__


class Sub(str):
    pass


class EqSub(str):
    def __eq__(self, other):
        return True

    __hash__ = str.__hash__


class KeyFpTest(unittest.TestCase):
    def test_primitives(self):
        self.assertEqual(key_fp(None), "n")
        self.assertEqual(key_fp(1), "i:1")
        self.assertEqual(key_fp(True), "i:1")
        self.assertEqual(key_fp(1.0), "i:1")
        self.assertEqual(key_fp(-0.0), "i:0")
        self.assertEqual(key_fp(1.5), "f:1.5")
        self.assertEqual(key_fp(float("inf")), "f:inf")
        self.assertIsNone(key_fp(float("nan")))
        self.assertEqual(key_fp(complex(2, 0)), "i:2")
        self.assertEqual(key_fp("a,b"), "s:a,b")
        self.assertEqual(key_fp(b"\x00\xff"), "y:00ff")

    def test_containers(self):
        self.assertEqual(key_fp((1, "a")), "t:('i:1','s:a')")
        self.assertEqual(key_fp(("a,b",)), "t:('s:a,b')")
        self.assertNotEqual(key_fp(("a,b",)), key_fp(("a", "b")))
        self.assertEqual(key_fp(frozenset({"b", "a"})), key_fp(frozenset({"a", "b"})))
        self.assertEqual(key_fp((True,)), key_fp((1.0,)))
        self.assertIsNone(key_fp((1, object())))
        P = namedtuple("P", "x y")
        self.assertEqual(key_fp(P(1, 2)), key_fp((1, 2)))

    def test_enums_and_classes(self):
        self.assertEqual(key_fp(Color.RED), f"e:{__name__}:Color.RED")
        self.assertEqual(key_fp(Mixed.A), "s:a")  # equal to "a" and hash-equal
        self.assertEqual(key_fp(Num.ONE), "i:1")
        self.assertIsNone(key_fp(Odd.X))
        self.assertEqual(key_fp(Color), f"c:{__name__}:Color")
        self.assertEqual(key_fp(int), "c:builtins:int")

    def test_subclasses_and_others(self):
        self.assertEqual(key_fp(Sub("x")), "s:x")
        self.assertIsNone(key_fp(EqSub("x")))
        self.assertIsNone(key_fp(object()))
        self.assertIsNone(key_fp(len))

    def test_equal_keys_collide(self):
        for a, b in ((1, True), (1, 1.0), (Mixed.A, "a"), (Num.ONE, 1), ((1,), (True,))):
            self.assertEqual(a, b)
            self.assertEqual(hash(a), hash(b))
            self.assertEqual(key_fp(a), key_fp(b))


@unittest.skipUnless(SRC, "pinned sqlglot checkout not found (set BR_SAMPLE_SRC)")
class PinnedStateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from selector.maps import Maps

        cls.modules = import_probe.modules_from_maps(Maps(os.path.join(ROOT, "maps", "maps.bin")))
        cls.base = state_probe.build_baseline(SRC, cls.modules, runs=3)

    def read(self, path):
        with open(os.path.join(SRC, path), "rb") as fh:
            return fh.read().decode()

    def probe(self, edits):
        """edits: [(path, old, new)] applied with exact, single-occurrence replacement."""
        patched = {}
        for path, old, new in edits:
            text = patched.get(path, self.read(path))
            if old is None:
                text = text + new
            else:
                self.assertEqual(text.count(old), 1, (path, old))
                text = text.replace(old, new)
            patched[path] = text
        return state_probe.run({p: s.encode() for p, s in patched.items()}, SRC, self.modules, None,
                               baseline=self.base)

    def stable_cells(self, d):
        return {k: v for k, v in d.cells.items() if not v.get("unstable")}

    def test_baseline_is_reproducible(self):
        # Every child has its own random hash seed (-I ignores PYTHONHASHSEED).
        self.assertEqual(self.base["opaque"], [])
        self.assertEqual(self.base["warm"], [])
        self.assertGreater(len(self.base["cells"]), 10000)
        for _ in range(2):
            d = self.probe([])
            self.assertTrue(d.ok, d.why)
            self.assertEqual(self.stable_cells(d), {})
            self.assertEqual(d.env_changed, [])
            self.assertEqual((d.new_modules, d.lost_modules, d.opaque, d.warm_caches), ([], [], [], []))
        # The few cells that differ across seeds are class-body loop variables over sets.
        for key in self.base["unstable"]:
            self.assertIn(key[2], ("text_type", "target_type"), key)

    def test_function_parsers_key_rename(self):
        d = self.probe([("sqlglot/parser.py", '"GAP_FILL": lambda self: self._parse_gap_fill(),',
                         '"GAP_FILLX": lambda self: self._parse_gap_fill(),')])
        self.assertTrue(d.ok, d.why)
        cells = self.stable_cells(d)
        self.assertIn(("c", "sqlglot.parser:Parser", "FUNCTION_PARSERS"), cells)
        self.assertGreater(len(cells), 1)  # every dialect parser that copies the table
        for key, detail in cells.items():
            self.assertEqual(key[0], "c")
            self.assertEqual(key[2], "FUNCTION_PARSERS", key)
            self.assertEqual(detail["kind"], "dict")
            self.assertEqual(detail["keys"], ["s:GAP_FILL", "s:GAP_FILLX"], key)
            self.assertEqual(detail["code_only_keys"], [])
        self.assertEqual(d.env_changed, [])

    def test_lambda_body_only(self):
        d = self.probe([("sqlglot/parser.py", '"CHR": lambda self: self._parse_char(),',
                         '"CHR": lambda self: self._parse_decode(),')])
        self.assertTrue(d.ok, d.why)
        cells = self.stable_cells(d)
        self.assertIn(("c", "sqlglot.parser:Parser", "FUNCTION_PARSERS"), cells)
        for key, detail in cells.items():
            self.assertEqual(key[2], "FUNCTION_PARSERS", key)
            self.assertEqual(detail["keys"], ["s:CHR"])
            self.assertEqual(detail["code_only_keys"], ["s:CHR"])
            self.assertTrue(detail["code_only"])

    def test_module_dict_value(self):
        d = self.probe([("sqlglot/dialects/dialect.py", '    "SUNDAY": 7,\n', '    "SUNDAY": 0,\n')])
        self.assertTrue(d.ok, d.why)
        cells = self.stable_cells(d)
        key = ("g", "sqlglot.dialects.dialect", "WEEK_START_DAY_TO_DOW")
        self.assertEqual(cells[key]["keys"], ["s:SUNDAY"])
        # Modules that import the dict by name hold the same object: their globals differ too.
        for k, detail in cells.items():
            self.assertEqual(k[0], "g", k)
            self.assertEqual(k[2], "WEEK_START_DAY_TO_DOW", k)
            self.assertEqual(detail["keys"], ["s:SUNDAY"])
        self.assertIn(("g", "sqlglot.generators.duckdb", "WEEK_START_DAY_TO_DOW"), cells)

    def test_tuple_of_classes_element_removed(self):
        d = self.probe([("sqlglot/parser.py",
                         "KEY_VALUE_DEFINITIONS: t.ClassVar = (exp.Alias, exp.EQ, exp.PropertyEQ, exp.Slice)",
                         "KEY_VALUE_DEFINITIONS: t.ClassVar = (exp.Alias, exp.EQ, exp.PropertyEQ)")])
        self.assertTrue(d.ok, d.why)
        cells = self.stable_cells(d)
        detail = cells[("c", "sqlglot.parser:Parser", "KEY_VALUE_DEFINITIONS")]
        self.assertEqual(detail["kind"], "seq")
        self.assertEqual(len(detail["keys"]), 1)
        self.assertTrue(detail["keys"][0].startswith("c:sqlglot.expressions") and detail["keys"][0].endswith(":Slice"))
        self.assertTrue(detail["len"])
        for k in cells:
            self.assertEqual(k[2], "KEY_VALUE_DEFINITIONS", k)

    def test_annotation_only(self):
        self.assertIn("from __future__ import annotations", self.read("sqlglot/generator.py"))
        d = self.probe([("sqlglot/generator.py", 'SELECT_KINDS: t.ClassVar[tuple[str, ...]] = ("STRUCT", "VALUE")',
                         'SELECT_KINDS: object = ("STRUCT", "VALUE")')])
        self.assertTrue(d.ok, d.why)
        self.assertEqual(set(self.stable_cells(d)), {("c", "sqlglot.generator:Generator", "__annotations__")})
        detail = d.cells[("c", "sqlglot.generator:Generator", "__annotations__")]
        self.assertEqual(detail["keys"], ["s:SELECT_KINDS"])

    def test_unused_module_name(self):
        d = self.probe([("sqlglot/jsonpath.py", None, "\n_BR_UNUSED_NAME = 1\n")])
        self.assertTrue(d.ok, d.why)
        self.assertEqual(set(self.stable_cells(d)), {("g", "sqlglot.jsonpath", "_BR_UNUSED_NAME")})
        self.assertTrue(d.cells[("g", "sqlglot.jsonpath", "_BR_UNUSED_NAME")]["added"])
        self.assertEqual(d.env_changed, [])

    def test_side_effects_outside_the_module_graph(self):
        d = self.probe([("sqlglot/jsonpath.py", None,
                         "\nimport sys as _s\n_s.setrecursionlimit(500)\nimport os as _o\n_o.environ['BR_X'] = '1'\n")])
        self.assertTrue(d.ok, d.why)
        self.assertIn("recursionlimit", d.env_changed)
        self.assertIn("environ", d.env_changed)
        self.assertEqual(set(self.stable_cells(d)), {("g", "sqlglot.jsonpath", "_s"), ("g", "sqlglot.jsonpath", "_o")})

    def test_stdlib_monkeypatch(self):
        d = self.probe([("sqlglot/jsonpath.py", None, "\nimport json as _j\n_j.dumps = lambda *a, **k: ''\n")])
        self.assertTrue(d.ok, d.why)
        self.assertIn("module:json", d.env_changed)

    def test_broken_import(self):
        d = self.probe([("sqlglot/jsonpath.py", None, "\nraise ImportError('broken on purpose')\n")])
        self.assertFalse(d.ok)
        self.assertIn("ImportError", d.why)
        d = self.probe([("sqlglot/jsonpath.py", None, "\nimport os\nos._exit(0)\n")])
        self.assertFalse(d.ok)

    def test_tokenizer_keywords(self):
        d = self.probe([("sqlglot/tokens.py", '        "CLUSTER BY": TokenType.CLUSTER_BY,\n',
                         '        "CLUSTER BYX": TokenType.CLUSTER_BY,\n')])
        self.assertTrue(d.ok, d.why)
        cells = self.stable_cells(d)
        kw = cells[("c", "sqlglot.tokens:Tokenizer", "KEYWORDS")]
        self.assertEqual(kw["keys"], ["s:CLUSTER BY", "s:CLUSTER BYX"])
        tries = [k for k in cells if k[2] == "_KEYWORD_TRIE"]
        self.assertIn(("c", "sqlglot.tokens:Tokenizer", "_KEYWORD_TRIE"), tries)
        self.assertGreater(len(tries), 5)  # every dialect tokenizer's metaclass-built trie
        self.assertEqual(cells[("c", "sqlglot.tokens:Tokenizer", "_KEYWORD_TRIE")]["keys"], ["s:C"])

    def test_refusals(self):
        d = state_probe.run({}, SRC, self.modules[:-1], None, baseline=self.base)
        self.assertFalse(d.ok)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.bin")
            state_probe.write_baseline(self.base, path)
            d = state_probe.run({}, SRC, self.modules, path)
            self.assertTrue(d.ok, d.why)
            d = state_probe.run({}, SRC, self.modules, os.path.join(tmp, "missing.bin"))
            self.assertFalse(d.ok)
        d = state_probe.run({}, SRC, self.modules, None, baseline=self.base, python=os.path.join(SRC, "no-python"))
        self.assertFalse(d.ok)


if __name__ == "__main__":
    unittest.main()
