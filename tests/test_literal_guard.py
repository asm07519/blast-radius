"""BR_LITERAL_GUARD: literal narrowing needs ast.literal_eval on every changed piece (PRD §6 rule 5).

Run from the repository root:  python3 -m unittest discover -s tests -t .
"""

import ast
import itertools
import os
import unittest
from contextlib import contextmanager

from selector import pyast

# Values that raise TypeError when Python builds them, yet pass the _pure_literal shape check.
BAD = ["-'x'", "{[]: 1}", "{[]}", "{(1, []): 2}", "-(1,)"]

# Valid literals that must keep narrowing with the guard on.
GOOD = ["0", "7", "-3", "+2", "-2.5", "1e300", "+1j", "-1j", "'s'", "''", "b'raw'", "None", "True", "False",
        "...", "()", "(1,)", "(1, 'a', None)", "[]", "[1, [2, [3]]]", "{1, 'a', (2, 3)}", "{}",
        "{'a': 1, 'b': [2, {'c': (3,)}]}", "{(1, (2, 'x')): None}", "'" + "x" * 5000 + "'", str(10 ** 1000)]


@contextmanager
def guard(on):
    saved = os.environ.get("BR_LITERAL_GUARD")
    os.environ["BR_LITERAL_GUARD"] = "1" if on else "0"
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("BR_LITERAL_GUARD", None)
        else:
            os.environ["BR_LITERAL_GUARD"] = saved


def class_src(value, name="X"):
    return f"class A:\n    Y = 0\n    {name} = {value}\n".encode()


def module_src(value, name="X"):
    return f"Y = 0\n{name} = {value}\n".encode()


def import_sites(old, new):
    return [s for s in pyast.analyse(old, new) if s.kind == "import"]


def literal_of(old, new, on):
    """Site.literal of the single import site of a change."""
    with guard(on):
        sites = pyast.analyse(old, new)
    kinds = [s.kind for s in sites]
    assert kinds == ["import"], (old, new, sites)
    return sites[0].literal


def change(old_value, new_value, on, where=class_src):
    return literal_of(where(old_value), where(new_value), on)


def builds(src):
    """Python itself executes the source without raising."""
    try:
        exec(compile(src, "<snippet>", "exec"), {"f": lambda *a: 0, "g": lambda *a: 0})
    except Exception:
        return False
    return True


class FlagTest(unittest.TestCase):
    def test_flag_read_per_call(self):
        with guard(True):
            self.assertTrue(pyast.literal_guard())
        with guard(False):
            self.assertFalse(pyast.literal_guard())
        saved = os.environ.pop("BR_LITERAL_GUARD", None)
        try:
            self.assertFalse(pyast.literal_guard())  # default off
        finally:
            if saved is not None:
                os.environ["BR_LITERAL_GUARD"] = saved

    def test_only_exact_one_turns_it_on(self):
        for value in ("", "0", "true", "yes", " 1", "2"):
            os.environ["BR_LITERAL_GUARD"] = value
            try:
                self.assertFalse(pyast.literal_guard(), value)
            finally:
                os.environ.pop("BR_LITERAL_GUARD", None)


class BadValuesTest(unittest.TestCase):
    """The five values from the PRD finding, in every position."""

    def test_python_really_raises(self):
        for value in BAD:
            self.assertFalse(builds(f"X = {value}"), value)

    def test_as_new_value(self):
        for where in (class_src, module_src):
            for value in BAD:
                with self.subTest(value=value, where=where.__name__):
                    self.assertTrue(change("1", value, on=False, where=where))  # today: narrows
                    self.assertFalse(change("1", value, on=True, where=where))

    def test_as_old_value(self):
        for where in (class_src, module_src):
            for value in BAD:
                with self.subTest(value=value, where=where.__name__):
                    self.assertTrue(change(value, "1", on=False, where=where))
                    self.assertFalse(change(value, "1", on=True, where=where))

    def test_inserted_and_removed_statement(self):
        for value in BAD:
            with self.subTest(value=value):
                base = b"class A:\n    Y = 0\n"
                added = base + f"    X = {value}\n".encode()
                annotated = base + f"    X: int = {value}\n".encode()
                for old, new in ((base, added), (added, base), (base, annotated)):
                    self.assertTrue(literal_of(old, new, on=False))
                    self.assertFalse(literal_of(old, new, on=True))

    def test_inside_mixed_containers(self):
        # (old, new): the container holds non-literal pieces, so only the changed piece is checked.
        cases = [
            ("{'a': f(), 'b': 1}", "{'a': f(), 'b': -'x'}"),          # changed value
            ("{'a': f(), 'b': 1}", "{'a': f(), 'b': -(1,)}"),
            ("{'a': f(), 'b': 1}", "{'a': f(), 'b': {[]: 1}}"),        # nested in a changed value
            ("{'a': f(), 'b': 1}", "{'a': f(), 'b': [{[]}]}"),
            ("{'a': f()}", "{'a': f(), []: 1}"),                       # added unhashable key
            ("{'a': f()}", "{'a': f(), (1, []): 2}"),
            ("{'a': f()}", "{'a': f(), (1, (2, [])): 2}"),             # tuple-of-list key, deeper
            ("{'a': f()}", "{'a': f(), 'c': {[]}}"),                   # added entry, bad value
            ("{'a': f()}", "{'a': f(), 'c': -'x'}"),
            ("{'a': f(), 'c': -'x'}", "{'a': f()}"),                   # removed entry, bad old value
            ("{'a': f(), (1, []): 2}", "{'a': f()}"),                  # removed entry, bad old key
            ("{'a': {'b': f(), 'c': 1}}", "{'a': {'b': f(), 'c': 1, []: 2}}"),  # nested mixed dict
            ("{f(), 1}", "{f(), []}"),                                 # changed set element
            ("{f(), 1}", "{f(), (1, [])}"),
            ("{f(), (1, 2)}", "{f(), (1, [2])}"),
            ("{f(), (1, (2, 3))}", "{f(), (1, (2, []))}"),
            ("{f(), 1}", "{f(), 1, {}}"),                              # inserted set element
            ("{f(), 1}", "{f(), 1, ((), [])}"),
            ("{(f(), 1)}", "{(f(), [])}"),                             # tuple that is a set element
            ("{(f(), 1)}", "{(f(), 1, [])}"),
            ("[{f(), 1}]", "[{f(), []}]"),
            ("[f(), 1]", "[f(), 1, -'x']"),                            # inserted list element
            ("[f(), 1]", "[f(), -(1,)]"),
            ("(f(), 1)", "(f(), {[]})"),
            ("[f(), -'x']", "[f(), 1]"),                               # bad old element
            ("{f(): 1, 'k': 2}", "{f(): 1, 'k': {[]}}"),
        ]
        for old, new in cases:
            with self.subTest(old=old, new=new):
                self.assertTrue(change(old, new, on=False))
                self.assertFalse(change(old, new, on=True))

    def test_repeated_dict_key_hides_a_changed_value(self):
        # The earlier "a" entry is still evaluated by Python; key matching only saw the later one.
        self.assertFalse(builds("X = {'a': -'x', 'a': f()}"))
        self.assertTrue(change("{'a': 1, 'a': f()}", "{'a': -'x', 'a': f()}", on=False))
        self.assertFalse(change("{'a': 1, 'a': f()}", "{'a': -'x', 'a': f()}", on=True))

    def test_unary_on_mixed_container(self):
        for op in ("-", "+", "not "):
            with self.subTest(op=op):
                self.assertTrue(change(f"{op}(f(), 1)", f"{op}(f(), 2)", on=False))
                self.assertFalse(change(f"{op}(f(), 1)", f"{op}(f(), 2)", on=True))


class NotAndStrictnessTest(unittest.TestCase):
    def test_not_is_rejected(self):
        for old, new in [("not 1", "not 0"), ("1", "not 0"), ("not []", "1"), ("[not 1]", "[not 0]"),
                         ("{'a': f(), 'b': not 1}", "{'a': f(), 'b': not 0}"), ("{'a': f()}", "{'a': f(), 'b': not 0}")]:
            with self.subTest(old=old, new=new):
                self.assertTrue(change(old, new, on=False))
                self.assertFalse(change(old, new, on=True))

    def test_literal_eval_stricter_than_python_only_widens(self):
        # Python builds these, literal_eval refuses: a fallback to all importers, never a miss.
        for value in ("- -1", "-True", "+False", "-(-2)"):
            with self.subTest(value=value):
                self.assertTrue(builds(f"X = {value}"))
                self.assertTrue(change("1", value, on=False))
                self.assertFalse(change("1", value, on=True))


class GoodValuesTest(unittest.TestCase):
    def test_python_builds_them(self):
        for value in GOOD:
            self.assertTrue(builds(f"X = {value}"), value[:40])

    def test_scalar_and_container_swaps_still_narrow(self):
        for where in (class_src, module_src):
            for old, new in itertools.product(["1", "'a'", "None"], GOOD):
                if old == new:
                    continue
                with self.subTest(old=old, new=new[:40], where=where.__name__):
                    self.assertTrue(change(old, new, on=True, where=where))
                    self.assertTrue(change(new, old, on=True, where=where))

    def test_inserted_and_removed_statement(self):
        base = b"class A:\n    Y = 0\n"
        for value in GOOD:
            added = base + f"    X = {value}\n".encode()
            self.assertTrue(literal_of(base, added, on=True), value[:40])
            self.assertTrue(literal_of(added, base, on=True), value[:40])

    def test_mixed_containers_still_narrow(self):
        cases = [
            # the sqlglot arg_types / table pattern: one literal entry changes beside non-literal values
            ("{'this': True, 'kind': exp.Foo, 'x': f()}", "{'this': False, 'kind': exp.Foo, 'x': f()}"),
            ("{'a': TokenType.A, 'b': 1}", "{'a': TokenType.A, 'b': -1}"),
            ("{'a': f(), 'b': 1}", "{'a': f(), 'b': 1, 'c': (1, 'x')}"),     # added entry
            ("{'a': f(), 'b': 1}", "{'a': f()}"),                            # removed entry
            ("{'a': f(), 'b': 1}", "{'a': f(), 'b': 1, (1, (2, None)): [3]}"),  # hashable tuple key
            ("{'a': f(), 'b': 1}", "{'a': f(), 'b': {'c': [1, {2}], 'd': b'z'}}"),
            ("{**base, 'b': 1}", "{**base, 'b': 2}"),
            ("{'a': {'b': f(), 'c': 1}}", "{'a': {'b': f(), 'c': 2, 'd': 3}}"),  # nested mixed dict
            ("{f(), 1}", "{f(), 2}"),
            ("{f(), 1}", "{f(), 1, (2, 'x'), None, ...}"),
            ("{(f(), 1)}", "{(f(), (2, 3))}"),
            ("[f(), 1]", "[f(), 1, [2], {}]"),
            ("[f(), 1, g()]", "[f(), g()]"),
            ("(f(), 'a')", "(f(), 'b', 'c')"),
            ("[{'a': f(), 'b': 1}]", "[{'a': f(), 'b': 2}]"),
        ]
        for old, new in cases:
            with self.subTest(old=old, new=new):
                self.assertTrue(change(old, new, on=False))
                self.assertTrue(change(old, new, on=True))

    def test_non_literal_changes_never_narrow(self):
        for old, new in [("1", "f()"), ("{'a': f()}", "{'a': g()}"), ("[1]", "[1] * 2"), ("set()", "{1}")]:
            with self.subTest(old=old, new=new):
                self.assertFalse(change(old, new, on=False))
                self.assertFalse(change(old, new, on=True))


class EvaluatesTest(unittest.TestCase):
    """The helper itself, on expression nodes."""

    @staticmethod
    def node(src):
        return ast.parse(src, mode="eval").body

    def test_values(self):
        for src in GOOD:
            self.assertTrue(pyast._evaluates(self.node(src)), src[:40])
        for src in BAD + ["not 1", "- -1", "f()"]:
            self.assertFalse(pyast._evaluates(self.node(src)), src)
        # literal_eval accepts set() (a call, and `set` may be shadowed); the _pure_literal shape
        # check, which the guard adds to rather than replaces, is what rejects it.
        self.assertTrue(pyast._evaluates(self.node("set()")))
        self.assertFalse(pyast._pure_literal(self.node("set()")))

    def test_hashable_context(self):
        for src in ("1", "'a'", "(1, (2, 'x'))", "None", "...", "b''", "-1j"):
            self.assertTrue(pyast._evaluates(self.node(src), hashable=True), src)
        for src in ("[]", "{}", "{1}", "(1, [])", "((1, ()), {})", "(1, (2, (3, [])))"):
            self.assertTrue(pyast._evaluates(self.node(src)), src)
            self.assertFalse(pyast._evaluates(self.node(src), hashable=True), src)

    def test_any_exception_is_no(self):
        deep = ast.Constant(1)
        for _ in range(100_000):
            deep = ast.Tuple(elts=[deep], ctx=ast.Load())
        self.assertFalse(pyast._evaluates(deep))  # RecursionError inside literal_eval
        self.assertFalse(pyast._evaluates(ast.Name("x", ast.Load())))
        self.assertFalse(pyast._evaluates(ast.Dict(keys=[None], values=[ast.Name("x", ast.Load())])))

    def test_deep_nesting_does_not_crash(self):
        # Deep enough that something in parse/compare/literal_eval may recurse out: whatever
        # happens, analyse returns sites, and a narrowed one is a value Python can build.
        for depth in (50, 300, 2000):
            deep = "[" * depth + "]" * depth
            with guard(True):
                sites = pyast.analyse(class_src("1"), class_src(deep))
            self.assertTrue(sites, depth)
            if any(s.literal for s in sites):
                self.assertTrue(builds(f"X = {deep}"), depth)


class SoundnessSweepTest(unittest.TestCase):
    """Over a grid of atoms placed in many container positions: with the guard on, a site
    marked literal must belong to old and new code that Python builds without raising, and
    the guard never accepts a change that today rejects."""

    ATOMS = ["1", "-1", "'x'", "b'y'", "None", "True", "+1j", "...", "()", "(1, 2)", "[]", "{}", "{1}",
             "(1, [])", "((2,), [3])", "{'k': []}", "-'x'", "-(1,)", "{[]}", "{[]: 1}", "{(1, []): 2}",
             "not 1", "- -1", "-True", "f()", "frozenset()", "[{[]}]", "({},)"]
    TEMPLATES = ["{}", "[{}]", "({},)", "{{{}}}", "{{'k': {}}}", "{{{}: 1}}", "{{'a': f(), 'b': {}}}",
                 "{{'a': f(), {}: 2}}", "{{f(), {}}}", "[f(), {}]", "(f(), {})", "{{(f(), {})}}",
                 "{{'a': {{'b': f(), 'c': {}}}}}", "[{{f(), {}}}]", "-({})", "not {}", "{{**d, 'b': {}}}",
                 "{{'a': 1, 'a': {}}}", "{{'a': {}, 'a': f()}}"]
    BASE = "1"

    def test_sweep(self):
        checked = accepted = unsound_today = 0
        for template, atom in itertools.product(self.TEMPLATES, self.ATOMS):
            old_v, new_v = template.format(self.BASE), template.format(atom)
            if old_v == new_v:
                continue
            for o, n in ((old_v, new_v), (new_v, old_v)):
                old, new = class_src(o), class_src(n)
                with guard(True):
                    on = [s.literal for s in import_sites(old, new)]
                with guard(False):
                    off = [s.literal for s in import_sites(old, new)]
                self.assertEqual(len(on), len(off), (o, n))
                for a, b in zip(on, off):
                    self.assertFalse(a and not b, f"guard widened: {o!r} -> {n!r}")
                    if b and not (builds(f"d = {{}}\nX = {o}") and builds(f"d = {{}}\nX = {n}")):
                        unsound_today += 1
                    if a:
                        accepted += 1
                        self.assertTrue(builds(f"d = {{}}\nX = {o}") and builds(f"d = {{}}\nX = {n}"),
                                        f"narrowed a change that raises: {o!r} -> {n!r}")
                checked += 1
        self.assertGreater(checked, 500)
        self.assertGreater(accepted, 100)  # the sweep exercises acceptance, not only rejection
        self.assertGreater(unsound_today, 50)  # and it has teeth: guard off narrows raising changes


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@unittest.skipUnless(os.environ.get("BR_SAMPLE_SRC"), "set BR_SAMPLE_SRC=<absolute sqlglot checkout> to run")
class SampleRegressionTest(unittest.TestCase):
    """The selector's output on the 30 sample commits is identical with the guard on and off."""

    def run_selector(self, flag):
        import subprocess
        import sys
        env = dict(os.environ, BR_LITERAL_GUARD=flag, BR_ASSUME_TRUSTED="1", BR_DATA=os.path.join(ROOT, "sample"),
                   BR_SRC=os.path.abspath(os.environ["BR_SAMPLE_SRC"]), PYTHONDONTWRITEBYTECODE="1", BR_VERBOSE="0")
        proc = subprocess.run([sys.executable, os.path.join(ROOT, "run.py")], env=env, cwd=ROOT,
                              capture_output=True, timeout=600)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        return proc.stdout

    def test_guard_does_not_change_sample_output(self):
        off, on = self.run_selector("0"), self.run_selector("1")
        self.assertEqual(len(off.splitlines()), 30)
        self.assertEqual(off, on)


if __name__ == "__main__":
    unittest.main()
