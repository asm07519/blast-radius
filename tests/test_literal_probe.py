"""BR_LITERAL_PROBE: literal narrowing only after the patched code is seen to import cleanly.

Run from the repository root:  python3 -m unittest discover -s tests -t .
With BR_SAMPLE_SRC=<absolute pinned sqlglot checkout> the end-to-end tests run on the real tree.
"""

import difflib
import os
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager

from selector import impact, import_probe, pyast
from selector.patch import parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(ROOT, "work")


@contextmanager
def env(**values):
    saved = {k: os.environ.get(k) for k in values}
    for k, v in values.items():
        os.environ[k] = v
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def probe_flag(on):
    return env(BR_LITERAL_PROBE="1" if on else "0")


def scratch():
    os.makedirs(WORK, exist_ok=True)
    path = tempfile.mkdtemp(prefix="test-literal-probe-", dir=WORK)
    return path


def diff(path, old, new):
    body = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), "a/" + path, "b/" + path))
    return f"diff --git a/{path} b/{path}\n" + body


class FakeMaps:
    """Just enough of selector.maps.Maps for commit_impact and refine_class_literal."""

    has_class_data = True
    typeprof_incomplete = None

    def __init__(self, class_info=None, users=None, readers=None, names=None, imported=None,
                 classes_at_import=()):
        self.class_info = class_info or {}
        self._users = users or {}
        self._readers = readers or {}
        self.names = names or {}
        self._imported = imported or {}
        self.classes_at_import = frozenset(classes_at_import)
        self.untrusted = 0
        self.files = sorted(self._imported)

    def imported(self, path):
        return self._imported.get(path, 0)

    def class_users(self, key):
        return self._users.get(key, 0)

    def name_readers(self, name):
        return self._readers.get(name, 0)

    def dynamic_readers(self, name, family_key=None):
        return 0

    def reads(self, path, linecache=True):
        return 0

    def listers(self, directory):
        return 0

    def call(self, path, lines):
        return 0

    def swap_is_noop(self, path, proof):
        return False


ALL = 0b1111_1111  # importers of the module
USERS = 0b0000_0011  # users of the class


def class_maps(key="pkg.mod:A", mro=None, **kw):
    mro = mro or (key,)
    info = {key: (True, tuple(mro))}
    info.update(kw.pop("extra_info", {}))
    kw.setdefault("readers", {"X": ALL})
    return FakeMaps(class_info=info, users={key: USERS, **kw.pop("users", {})}, imported={"pkg/mod.py": ALL},
                    **kw)


def sites_of(old, new, on):
    with probe_flag(on):
        return [s for s in pyast.analyse(old.encode(), new.encode()) if s.kind == "import"]


def refine(old, new, maps, on, path="pkg/mod.py"):
    """refine_class_literal on every import site of a change."""
    out = []
    with probe_flag(on):
        for s in pyast.analyse(old.encode(), new.encode()):
            if s.kind == "import":
                out.append(impact.refine_class_literal(path, old.encode(), new.encode(), s, maps))
    return out


# --- A. static exclusions --------------------------------------------------------------------

class SiteFactsTest(unittest.TestCase):
    def test_annotated_flag(self):
        cases = [
            ("class A:\n    X: int = 1\n", "class A:\n    X: int = 2\n", [True]),
            ("class A:\n    X = 1\n", "class A:\n    X = 2\n", [False]),
            ("class A:\n    Y = 0\n", "class A:\n    Y = 0\n    X: int = 0\n", [True]),
            ("class A:\n    Y = 0\n    X: int = 0\n", "class A:\n    Y = 0\n", [True]),
            # Assign <-> AnnAssign: a removal and an insertion, both annotated
            ("class A:\n    X = 1\n", "class A:\n    X: int = 1\n", [True, True]),
            ("class A:\n    X: int = 1\n", "class A:\n    X = 1\n", [True, True]),
        ]
        for old, new, expected in cases:
            with self.subTest(old=old, new=new):
                sites = sites_of(old, new, on=True)
                self.assertEqual([s.annotated for s in sites], expected)
                self.assertTrue(all(s.literal for s in sites))

    def test_class_heads(self):
        src = "@deco\nclass A(B, x.C, metaclass=M):\n    class N(G[int]):\n        X = 1\n"
        sites = sites_of(src, src.replace("X = 1", "X = 2"), on=True)
        self.assertEqual(len(sites), 1)
        self.assertEqual(sites[0].class_path, ["A", "N"])
        self.assertEqual(sites[0].class_heads, ((1, ("B", "x.C"), (("metaclass", "M"),), False),
                                                (0, (None,), (), False)))

    def test_non_name_targets_are_not_literal_with_the_flag(self):
        for old, new in [("class A:\n    D['k'] = 1\n", "class A:\n    D['k'] = 2\n"),
                         ("class A:\n    o.a = 1\n", "class A:\n    o.a = 2\n"),
                         ("class A:\n    P, Q = 1, 2\n", "class A:\n    P, Q = 1, 3\n")]:
            with self.subTest(old=old):
                self.assertEqual([s.literal for s in sites_of(old, new, on=False)], [True])
                self.assertEqual([s.literal for s in sites_of(old, new, on=True)], [False])


class StaticExclusionTest(unittest.TestCase):
    def assertExcluded(self, old, new, maps=None):
        maps = maps or class_maps()
        off, on = refine(old, new, maps, on=False), refine(old, new, maps, on=True)
        self.assertTrue(off and all(b is not None for b in off), off)  # today: narrows
        self.assertEqual(on, [None] * len(on))

    def test_plain_class_still_narrows(self):
        old, new = "class A(Base):\n    X = 1\n", "class A(Base):\n    X = 2\n"
        self.assertEqual(refine(old, new, class_maps(), on=True), [USERS])
        site = sites_of(old, new, on=True)[0]
        self.assertIsNone(impact.probe_exclusion(site, ["pkg.mod:A"], class_maps()))

    def test_annotated_assignment(self):
        self.assertExcluded("class A:\n    X: int = 1\n", "class A:\n    X: int = 2\n")
        self.assertExcluded("class A:\n    Y = 0\n", "class A:\n    Y = 0\n    X: int = 0\n")
        self.assertExcluded("class A:\n    Y = 0\n    X: int = 0\n", "class A:\n    Y = 0\n")

    def test_assign_annassign_conversion(self):
        # the singlestore case: `X = True` -> `X: t.ClassVar[bool] = True` (NameError on 3.13 without `t`)
        self.assertExcluded("class A:\n    X = True\n", "class A:\n    X: t.ClassVar[bool] = True\n")
        self.assertExcluded("class A:\n    X: t.ClassVar[bool] = True\n", "class A:\n    X = True\n")

    def test_decorated_classes(self):
        for deco in ("@dataclass", "@dataclass(frozen=True)", "@trait", "@register"):
            with self.subTest(deco=deco):
                self.assertExcluded(f"{deco}\nclass A:\n    X = 1\n", f"{deco}\nclass A:\n    X = 2\n")
        # a decorated outer class, change in a nested one
        maps = class_maps("pkg.mod:A.B", extra_info={"pkg.mod:A": (True, ("pkg.mod:A",))})
        self.assertExcluded("@d\nclass A:\n    class B:\n        X = 1\n",
                            "@d\nclass A:\n    class B:\n        X = 2\n", maps)

    def test_class_keywords_and_type_params(self):
        self.assertExcluded("class A(B, metaclass=M):\n    X = 1\n", "class A(B, metaclass=M):\n    X = 2\n")
        self.assertExcluded("class A(B, flag=True):\n    X = 1\n", "class A(B, flag=True):\n    X = 2\n")
        self.assertExcluded("class A(B, **kw):\n    X = 1\n", "class A(B, **kw):\n    X = 2\n")
        if sys.version_info >= (3, 12):
            self.assertExcluded("class A[T]:\n    X = 1\n", "class A[T]:\n    X = 2\n")

    def test_special_or_unclassifiable_bases(self):
        for base in ("t.NamedTuple", "NamedTuple", "TypedDict", "t.TypedDict", "Enum", "enum.IntEnum", "AutoName",
                     "t.Protocol", "Protocol", "type", "Generic", "t.Generic[T]", "make_base()", "*bases",
                     "Base[int]"):
            with self.subTest(base=base):
                self.assertExcluded(f"class A({base}):\n    X = 1\n", f"class A({base}):\n    X = 2\n")

    def test_external_mro(self):
        # plain-looking bases whose MRO (from the maps) leaves sqlglot
        for ext in ("builtins:tuple", "builtins:dict", "enum:Enum", "typing:Protocol", "typing:Generic",
                    "builtins:type", "builtins:str"):
            with self.subTest(ext=ext):
                maps = class_maps(mro=("pkg.mod:A", "pkg.mod:Base", ext),
                                  extra_info={"pkg.mod:Base": (True, ("pkg.mod:Base", ext))})
                self.assertExcluded("class A(Base):\n    X = 1\n", "class A(Base):\n    X = 2\n", maps)
        for ok in ("builtins:Exception", "builtins:BaseException", "abc:ABC", "collections.abc:Mapping"):
            with self.subTest(ok=ok):
                maps = class_maps(mro=("pkg.mod:A", "pkg.mod:Base", ok),
                                  extra_info={"pkg.mod:Base": (True, ("pkg.mod:Base", ok))})
                self.assertEqual(refine("class A(Base):\n    X = 1\n", "class A(Base):\n    X = 2\n", maps, on=True),
                                 [USERS])

    def test_default_tokenizer_family(self):
        key = "sqlglot.tokens:Tokenizer"
        maps = FakeMaps(class_info={key: (True, (key, "sqlglot.tokens:_TokenizerBase")),
                                    "sqlglot.tokens:_TokenizerBase": (True, ("sqlglot.tokens:_TokenizerBase",))},
                        users={key: USERS}, imported={"sqlglot/tokens.py": ALL}, readers={"X": ALL})
        old, new = "class Tokenizer(_TokenizerBase):\n    X = 1\n", "class Tokenizer(_TokenizerBase):\n    X = 2\n"
        self.assertEqual(refine(old, new, maps, on=False, path="sqlglot/tokens.py"), [USERS])
        self.assertEqual(refine(old, new, maps, on=True, path="sqlglot/tokens.py"), [None])

    def test_hook_reads(self):
        old, new = "class A:\n    X = 1\n", "class A:\n    X = 2\n"
        for record, narrows in [
            (("cls", "__init_subclass__"), True),          # audited: stored on cls
            (("klass", "__new__"), True),                  # the dialect metaclass on the class it creates
            (("klass.tokenizer_class", "__new__"), True),  # on the dialect owning the tokenizer
            (("gen_cls", "__new__"), False),               # gen_cls.TRANSFORMS.pop: maybe a shared dict
            (("other", "__init__"), False),                # an ordinary object built at import
            (("cls", "__class_getitem__"), False),         # receivers the tracer does not record
            (("self", "__set_name__"), False),
        ]:
            with self.subTest(record=record):
                maps = class_maps(names={"X": (0, (), (record,))})
                self.assertIsNotNone(refine(old, new, maps, on=False)[0])  # today: narrows
                got = refine(old, new, maps, on=True)[0]
                self.assertEqual(got is not None, narrows, got)

    def test_nested_family_members_add_their_owners(self):
        # P.Tokenizer changes; D.Tokenizer(P.Tokenizer) is nested in D, which is not a subclass of P.
        info = {"pkg.mod:P": (True, ("pkg.mod:P",)),
                "pkg.mod:P.Tokenizer": (True, ("pkg.mod:P.Tokenizer",)),
                "pkg.other:D": (True, ("pkg.other:D",)),
                "pkg.other:D.Tokenizer": (True, ("pkg.other:D.Tokenizer", "pkg.mod:P.Tokenizer"))}
        users = {"pkg.mod:P": 0b1, "pkg.mod:P.Tokenizer": 0b10, "pkg.other:D": 0b1000_0000,
                 "pkg.other:D.Tokenizer": 0}
        maps = FakeMaps(class_info=info, users=users, imported={"pkg/mod.py": ALL}, readers={"X": ALL})
        old = "class P:\n    class Tokenizer:\n        X = 1\n"
        new = old.replace("X = 1", "X = 2")
        self.assertEqual(refine(old, new, maps, on=False), [0b11])
        self.assertEqual(refine(old, new, maps, on=True), [0b1000_0011])


# --- B. conditional narrowing in commit_impact ------------------------------------------------

NARROW = 0b0000_0001
MOD_A = "class A:\n    X = 1\n"
MOD_B = "class B:\n    Y = 'a'\n"


def literal_refiner(path, old, new, site, maps):
    return NARROW if site.literal else None


class FakeProbe:
    def __init__(self, answer=True):
        self.answer = answer
        self.calls = []

    def __call__(self, patched):
        self.calls.append(dict(patched))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


class ResolutionTest(unittest.TestCase):
    def setUp(self):
        self.src = scratch()
        self.addCleanup(shutil.rmtree, self.src, True)
        files = {"sqlglot/a.py": MOD_A, "sqlglot/b.py": MOD_B, "sqlglot/data.json": "{}\n",
                 "tests/fixtures/x.sql": "SELECT 1\n", "tests/test_a.py": "X = 1\n"}
        for rel, text in files.items():
            os.makedirs(os.path.dirname(os.path.join(self.src, rel)), exist_ok=True)
            with open(os.path.join(self.src, rel), "w") as fh:
                fh.write(text)
        self.maps = FakeMaps(imported={"sqlglot/a.py": 0b0000_1111, "sqlglot/b.py": 0b1111_0000,
                                       "tests/test_a.py": 0b1_0000_0000})

    def impact(self, patch, probe, on=True):
        with probe_flag(on):
            return impact.commit_impact(parse(patch.encode()), self.src, self.maps, import_refiner=literal_refiner,
                                        import_probe=probe)

    LIT_A = diff("sqlglot/a.py", MOD_A, MOD_A.replace("X = 1", "X = 2"))
    LIT_B = diff("sqlglot/b.py", MOD_B, MOD_B.replace("'a'", "'b'"))

    def test_flag_off_never_probes(self):
        probe = FakeProbe(False)
        res = self.impact(self.LIT_A, probe, on=False)
        self.assertEqual(res.bits, NARROW)
        self.assertEqual(probe.calls, [])
        self.assertEqual(res.conditional, [])

    def test_probe_true_applies_narrowing(self):
        probe = FakeProbe(True)
        res = self.impact(self.LIT_A, probe)
        self.assertEqual(res.bits, NARROW)
        self.assertEqual(probe.calls, [{"sqlglot/a.py": MOD_A.replace("X = 1", "X = 2").encode()}])

    def test_anything_but_true_falls_back(self):
        for answer in (False, None, 1, "yes", RuntimeError("boom"), KeyboardInterrupt()):
            with self.subTest(answer=answer):
                probe = FakeProbe(answer)
                if isinstance(answer, KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):  # not swallowed: the run is being stopped
                        self.impact(self.LIT_A, probe)
                    continue
                res = self.impact(self.LIT_A, probe)
                self.assertEqual(res.bits, 0b0000_1111)
                self.assertEqual(len(probe.calls), 1)
                self.assertTrue(any("literal narrowing dropped" in n for n in res.notes), res.notes)

    def test_one_probe_per_commit_with_every_patched_file(self):
        probe = FakeProbe(True)
        res = self.impact(self.LIT_A + self.LIT_B, probe)
        self.assertEqual(len(probe.calls), 1)
        self.assertEqual(sorted(probe.calls[0]), ["sqlglot/a.py", "sqlglot/b.py"])
        self.assertEqual(res.bits, NARROW)
        probe = FakeProbe(False)
        res = self.impact(self.LIT_A + self.LIT_B, probe)
        self.assertEqual(len(probe.calls), 1)
        self.assertEqual(res.bits, 0b1111_1111)

    def test_whole_patch_is_seen_before_probing(self):
        # a non-literal change in another file is part of the patched dict the probe gets
        other = diff("sqlglot/b.py", MOD_B, MOD_B + "Z = f()\n")
        probe = FakeProbe(True)
        res = self.impact(self.LIT_A + other, probe)
        self.assertEqual(sorted(probe.calls[0]), ["sqlglot/a.py", "sqlglot/b.py"])
        self.assertEqual(res.bits, NARROW | 0b1111_0000)

    def test_not_called_without_conditional_narrowing(self):
        probe = FakeProbe(True)
        res = self.impact(diff("sqlglot/a.py", MOD_A, MOD_A + "Z = f()\n"), probe)
        self.assertEqual(probe.calls, [])
        self.assertEqual(res.bits, 0b0000_1111)

    def test_not_called_when_falling_back_adds_nothing(self):
        # a non-literal module-level change in the same file already selects all its importers
        probe = FakeProbe(True)
        both = diff("sqlglot/a.py", MOD_A, MOD_A.replace("X = 1", "X = 2") + "Z = f()\n")
        res = self.impact(both, probe)
        self.assertEqual(probe.calls, [])
        self.assertEqual(res.bits, 0b0000_1111)

    def test_no_probe_callable(self):
        res = self.impact(self.LIT_A, None)
        self.assertEqual(res.bits, 0b0000_1111)

    def test_unsupported_patch_parts_block_the_probe(self):
        added = "diff --git a/sqlglot/c.py b/sqlglot/c.py\nnew file mode 100644\n--- /dev/null\n" \
                "+++ b/sqlglot/c.py\n@@ -0,0 +1 @@\n+X = 1\n"
        deleted = "diff --git a/sqlglot/b.py b/sqlglot/b.py\ndeleted file mode 100644\n--- a/sqlglot/b.py\n" \
                  "+++ /dev/null\n@@ -1,2 +0,0 @@\n-class B:\n-    Y = 'a'\n"
        renamed = "diff --git a/sqlglot/b.py b/sqlglot/c.py\nsimilarity index 100%\nrename from sqlglot/b.py\n" \
                  "rename to sqlglot/c.py\n"
        binary = "diff --git a/sqlglot/b.py b/sqlglot/b.py\nindex 1..2 100644\n" \
                 "Binary files a/sqlglot/b.py and b/sqlglot/b.py differ\n"
        unapplicable = "diff --git a/sqlglot/b.py b/sqlglot/b.py\n--- a/sqlglot/b.py\n+++ b/sqlglot/b.py\n" \
                       "@@ -1,2 +1,2 @@\n class Nope:\n-    Q = 1\n+    Q = 2\n"
        syntax = diff("sqlglot/b.py", MOD_B, MOD_B + "def (\n")
        outside = diff("tests/test_a.py", "X = 1\n", "X = 2\n")
        package_data = diff("sqlglot/data.json", "{}\n", "{\"a\": 1}\n")
        twice = self.LIT_A + diff("sqlglot/a.py", MOD_A, MOD_A.replace("X = 1", "X = 3"))
        for name, extra in [("added", added), ("deleted", deleted), ("renamed", renamed), ("binary", binary),
                            ("not applicable", unapplicable), ("syntax error", syntax),
                            ("python outside sqlglot", outside), ("package data", package_data),
                            ("same path twice", twice)]:
            with self.subTest(name):
                probe = FakeProbe(True)
                patch = extra if name == "same path twice" else self.LIT_A + extra
                res = self.impact(patch, probe)
                self.assertEqual(probe.calls, [])
                self.assertTrue(res.bits & 0b0000_1110, bin(res.bits))  # fell back to a.py's importers
                self.assertTrue(res.probe_blockers)

    def test_data_files_outside_the_package_do_not_block(self):
        probe = FakeProbe(True)
        res = self.impact(self.LIT_A + diff("tests/fixtures/x.sql", "SELECT 1\n", "SELECT 2\n"), probe)
        self.assertEqual(len(probe.calls), 1)
        self.assertEqual(sorted(probe.calls[0]), ["sqlglot/a.py"])
        self.assertEqual(res.bits, NARROW)


# --- the real probe child ----------------------------------------------------------------------

PKG = {
    "sqlglot/__init__.py": "from sqlglot import core\nfrom sqlglot.core import Base\n",
    "sqlglot/core.py": ("class Base:\n    arg_types = {'this': True}\n\n"
                        "    def __init_subclass__(cls, **kw):\n        super().__init_subclass__(**kw)\n"
                        "        cls.required = {k for k, v in cls.arg_types.items() if v}\n"),
    "sqlglot/kinds.py": "from sqlglot.core import Base\n\n\nclass Kind(Base):\n    arg_types = {'this': False}\n",
    "sqlglot/lazy.py": "raise RuntimeError('never imported by any test')\n",
}
MODULES = ["sqlglot", "sqlglot.core", "sqlglot.kinds"]


class RealProbeTest(unittest.TestCase):
    def setUp(self):
        self.src = scratch()
        self.addCleanup(shutil.rmtree, self.src, True)
        for rel, text in PKG.items():
            os.makedirs(os.path.dirname(os.path.join(self.src, rel)), exist_ok=True)
            with open(os.path.join(self.src, rel), "w") as fh:
                fh.write(text)

    def probe(self, patched, modules=MODULES, timeout=10.0):
        ok, detail = import_probe.run_detailed({k: v.encode() for k, v in patched.items()}, self.src, modules,
                                               timeout=timeout)
        self.assertIs(ok, import_probe.run({k: v.encode() for k, v in patched.items()}, self.src, modules,
                                           timeout=timeout))
        return ok, detail

    def kinds(self, body):
        return {"sqlglot/kinds.py": "from sqlglot.core import Base\n\n\nclass Kind(Base):\n" + body}

    def test_clean_import(self):
        self.assertEqual(self.probe({}), (True, "clean import"))
        self.assertTrue(self.probe(self.kinds("    arg_types = {'this': True}\n"))[0])

    def test_module_raising_at_import(self):
        ok, detail = self.probe(self.kinds("    arg_types = {}\nraise ValueError('x')\n"))
        self.assertFalse(ok)
        self.assertIn("ValueError", detail)

    def test_raising_in_init_subclass(self):
        ok, detail = self.probe(self.kinds("    arg_types = ['this']\n"))  # list has no .items()
        self.assertFalse(ok)
        self.assertIn("AttributeError", detail)

    def test_sys_exit_and_os_exit_at_import(self):
        for code in ("import sys\nsys.exit(0)\n", "import sys\nsys.exit()\n", "import os\nos._exit(0)\n",
                     "raise SystemExit(0)\n", "raise KeyboardInterrupt\n"):
            with self.subTest(code=code):
                self.assertFalse(self.probe(self.kinds("    pass\n" + code))[0])

    def test_printing_the_marker_then_raising(self):
        body = ("    pass\nimport sys\nprint('OK')\nprint('PASS')\nsys.stdout.write('OK ' + 'f' * 32 + '\\n')\n"
                "sys.stdout.flush()\nraise RuntimeError('after the marker')\n")
        self.assertFalse(self.probe(self.kinds(body))[0])

    def test_cannot_read_the_nonce(self):
        # the spec (with the nonce) is gone before any patched code runs
        body = "    pass\nimport os, sys\nif os.path.exists(sys.argv[1]):\n    raise RuntimeError('spec visible')\n"
        self.assertTrue(self.probe(self.kinds(body))[0])

    def test_hang_past_timeout(self):
        started = time.monotonic()
        ok, detail = self.probe(self.kinds("    pass\nwhile True:\n    pass\n"), timeout=1.0)
        self.assertFalse(ok)
        self.assertIn("timeout", detail)
        self.assertLess(time.monotonic() - started, 8.0)  # two runs (run and run_detailed), each ~1 s

    def test_flooding_output(self):
        flood = "    pass\nimport sys\nfor _ in range(64):\n    sys.stdout.write('x' * 65536)\n" \
                "    sys.stderr.write('y' * 65536)\n"
        out = tempfile.TemporaryFile()
        saved = os.dup(1)
        os.dup2(out.fileno(), 1)
        try:
            clean = self.probe(self.kinds(flood))[0]
            failing = self.probe(self.kinds(flood + "raise RuntimeError('x')\n"))[0]
        finally:
            os.dup2(saved, 1)
            os.close(saved)
        out.seek(0)
        self.assertEqual(out.read(), b"")  # nothing reaches the selector's stdout
        self.assertTrue(clean)  # output alone does not fail a test
        self.assertFalse(failing)

    def test_recursion_error(self):
        body = "    pass\ndef f(n):\n    return f(n + 1)\nf(0)\n"
        self.assertFalse(self.probe(self.kinds(body))[0])

    def test_overlay_is_what_gets_imported(self):
        # on disk the module raises; the patched version is clean, and vice versa
        with open(os.path.join(self.src, "sqlglot/kinds.py"), "w") as fh:
            fh.write("raise RuntimeError('old source')\n")
        self.assertFalse(self.probe({})[0])
        self.assertTrue(self.probe(self.kinds("    arg_types = {'this': True}\n"))[0])
        with open(os.path.join(self.src, "sqlglot/kinds.py"), "w") as fh:
            fh.write(PKG["sqlglot/kinds.py"])
        # a patched package __init__ is overlaid too, and its submodules still resolve
        self.assertFalse(self.probe({"sqlglot/__init__.py": "raise ImportError('patched init')\n"})[0])
        self.assertTrue(self.probe({"sqlglot/__init__.py": "from sqlglot import kinds\n"})[0])
        # no bytecode written into the source tree
        self.assertFalse([d for _, ds, _ in os.walk(self.src) for d in ds if d == "__pycache__"])

    def test_every_listed_module_must_import(self):
        self.assertFalse(self.probe({}, modules=MODULES + ["sqlglot.lazy"])[0])
        self.assertFalse(self.probe({}, modules=MODULES + ["sqlglot.missing"])[0])
        # a patched module no test imports is not imported by the probe
        self.assertTrue(self.probe({"sqlglot/lazy.py": "raise RuntimeError('still not imported')\n"})[0])

    def test_no_temporary_files_left(self):
        before = {p for p in os.listdir(tempfile.gettempdir()) if p.startswith("br-probe-")}
        self.probe({})
        self.probe(self.kinds("    pass\nraise ValueError\n"))
        self.probe(self.kinds("    pass\nwhile True:\n    pass\n"), timeout=0.5)
        after = {p for p in os.listdir(tempfile.gettempdir()) if p.startswith("br-probe-")}
        self.assertEqual(after - before, set())

    def test_broken_interpreter_is_false(self):
        ok, _ = import_probe.run_detailed({}, self.src, MODULES, python=os.path.join(self.src, "no-python"))
        self.assertFalse(ok)


class ModulesFromMapsTest(unittest.TestCase):
    def test_list(self):
        maps = FakeMaps(imported={"sqlglot/__init__.py": 1, "sqlglot/b.py": 2, "sqlglot/a/__init__.py": 4,
                                  "sqlglot/a/c.py": 8, "sqlglot/never.py": 0, "sqlglot/__main__.py": 1,
                                  "tests/test_x.py": 1, "sqlglotc/setup.py": 1, "sqlglot/data.json": 1,
                                  "sqlglot/bad-name.py": 1})
        self.assertEqual(import_probe.modules_from_maps(maps), ["sqlglot", "sqlglot.a", "sqlglot.a.c", "sqlglot.b"])


# --- end to end on the pinned sqlglot ---------------------------------------------------------

def _edit(src_root, path, old, new):
    with open(os.path.join(src_root, path)) as fh:
        text = fh.read()
    assert text.count(old) == 1, (path, old, text.count(old))
    return diff(path, text, text.replace(old, new))


# (name, file, old text, new text): each makes `import sqlglot` (or the module) fail on 3.13.
FAILING_EDITS = [
    ("argtypes", "sqlglot/expressions/constraints.py",
     'class EphemeralColumnConstraint(Expression, ColumnConstraintKind):\n    arg_types = {"this": False}',
     'class EphemeralColumnConstraint(Expression, ColumnConstraintKind):\n    arg_types = ["this"]'),
    ("heredoc", "sqlglot/dialects/duckdb.py", 'HEREDOC_STRINGS = ["$"]', 'HEREDOC_STRINGS = [("$",)]'),
    ("dataclass", "sqlglot/diff.py",
     '    """Indicates that an existing node\'s position within the tree has changed"""\n\n    source: exp.Expr\n',
     '    """Indicates that an existing node\'s position within the tree has changed"""\n\n    source: exp.Expr\n'
     '    weight: int = 0\n'),
    ("namedtuple", "sqlglot/optimizer/pushdown_projections.py", "    scopes: list[Scope]\n",
     "    scopes: list[Scope]\n    depth: int = 0\n"),
    ("annot2", "sqlglot/dialects/singlestore.py", "    SUPPORTS_ORDER_BY_ALL = True\n",
     "    SUPPORTS_ORDER_BY_ALL: t.ClassVar[bool] = True\n"),
]

# Sample-style edits that import cleanly and must keep narrowing.
CLEAN_EDITS = [
    ("arg_types entry", "sqlglot/expressions/ddl.py", '        "constraint": False,', '        "constraint": True,'),
    ("NUMERIC_ESCAPES", "sqlglot/dialects/snowflake.py", '"u": (16, 4, 4, 0xFFFF)', '"u": (17, 4, 4, 0xFFFF)'),
    ("WEEK_OFFSET", "sqlglot/dialects/tsql.py", "    WEEK_OFFSET = -1\n", "    WEEK_OFFSET = -2\n"),
]


@unittest.skipUnless(os.environ.get("BR_SAMPLE_SRC"), "set BR_SAMPLE_SRC=<absolute sqlglot checkout> to run")
class PinnedSqlglotTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from selector.maps import Maps

        cls.src = os.path.abspath(os.environ["BR_SAMPLE_SRC"])
        cls.maps = Maps(os.path.join(ROOT, "maps", "maps.bin"))
        cls.maps.untrusted = 0
        cls.modules = import_probe.modules_from_maps(cls.maps)

    def select(self, patch, on, probe=True):
        calls = []

        def real_probe(patched):
            ok = import_probe.run(patched, self.src, self.modules)
            calls.append(ok)
            return ok

        with probe_flag(on):
            res = impact.commit_impact(parse(patch.encode()), self.src, self.maps,
                                       import_refiner=impact.refine_class_literal,
                                       import_probe=real_probe if probe else None)
        return res.bits, calls

    def test_modules_list(self):
        self.assertIn("sqlglot", self.modules)
        self.assertIn("sqlglot.dialects.duckdb", self.modules)  # loaded lazily through get_or_raise
        self.assertGreater(len(self.modules), 150)
        self.assertEqual(self.modules, sorted(self.modules))

    def test_pinned_tree_imports_cleanly(self):
        self.assertEqual(import_probe.run_detailed({}, self.src, self.modules), (True, "clean import"))

    def test_failing_edits_select_every_importer(self):
        for name, path, old, new in FAILING_EDITS:
            with self.subTest(name):
                patch = _edit(self.src, path, old, new)
                importers = self.maps.imported(path)
                self.assertTrue(importers)
                off, _ = self.select(patch, on=False)
                self.assertNotEqual(importers & ~off, 0, "the defect: flag off misses importers")
                on, calls = self.select(patch, on=True)
                self.assertEqual(importers & ~on, 0)
                self.assertTrue(all(c is False for c in calls), calls)  # probed only when a narrowing was left

    def test_clean_edits_still_narrow(self):
        for name, path, old, new in CLEAN_EDITS:
            with self.subTest(name):
                patch = _edit(self.src, path, old, new)
                off, _ = self.select(patch, on=False)
                on, calls = self.select(patch, on=True)
                self.assertEqual(calls, [True])
                self.assertEqual(on, off)
                self.assertNotEqual(self.maps.imported(path) & ~on, 0)  # narrower than all importers
                unwired, _ = self.select(patch, on=True, probe=False)
                self.assertEqual(self.maps.imported(path) & ~unwired, 0)  # no probe: all importers


if __name__ == "__main__":
    unittest.main()
