"""Logging proxies of tools/trace/br_cells.py: what is logged, what is silent, and that
behaviour is preserved (CPython 3.13 is the labelling interpreter; silent-path
expectations are pinned to it).

    python3 -m unittest tests.test_cells_proxy
"""

import copy
import json
import os
import pickle
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools", "trace"))

import br_cells as bc  # noqa: E402

PY313 = sys.version_info[:2] == (3, 13)


def fresh(v):
    i = bc._register(v)
    assert i is not None
    return bc._PROXIES[i], i


def reset():
    for ch in ("test", "import"):
        bc._KEYS[ch].clear()
        bc._WHOLE[ch].clear()
        bc._MUT[ch].clear()
        bc._UNHASHABLE[ch].clear()
    bc._set_channel("test")


def keys(i):
    return set(bc._KEYS["test"].get(i, ()))


def whole(i):
    return i in bc._WHOLE["test"]


def mut(i):
    return i in bc._MUT["test"]


class DictProxyTest(unittest.TestCase):
    def setUp(self):
        reset()
        self.orig = {"a": 1, "b": 2, 3: "c"}
        self.p, self.i = fresh(self.orig)

    def logged(self, fn, key=None, is_whole=False, is_mut=False):
        reset()
        result = fn()
        if key is not None:
            self.assertIn(key, keys(self.i))
        self.assertEqual(whole(self.i), is_whole)
        self.assertEqual(mut(self.i), is_mut)
        return result

    def test_behaviour(self):
        p, o = self.p, self.orig
        self.assertIsInstance(p, dict)
        self.assertEqual(p, o)
        self.assertEqual(list(dict.items(p)), list(o.items()))
        self.assertEqual(repr(p), repr(o))
        self.assertEqual(str(p), str(o))
        self.assertEqual(json.dumps({"a": 1, "b": 2}), json.dumps(fresh({"a": 1, "b": 2})[0]))
        for clone in (copy.copy(p), copy.deepcopy(p), pickle.loads(pickle.dumps(p)), p.copy(), dict(p), {**p},
                      p | {}, {} | p):
            self.assertIs(type(clone), dict)
            self.assertEqual(list(clone.items()), list(o.items()))
        for proto in range(pickle.HIGHEST_PROTOCOL + 1):
            self.assertIs(type(pickle.loads(pickle.dumps(p, proto))), dict)
        self.assertIs(type(p.fromkeys("ab")), dict)
        with self.assertRaises(TypeError):
            hash(p)
        self.assertFalse(hasattr(p, "__dict__"))

    def test_deepcopy_self_reference(self):
        o = {"x": 1}
        p, _ = fresh(o)
        dict.__setitem__(p, "self", p)
        c = copy.deepcopy(p)
        self.assertIs(c["self"], c)

    def test_keyed(self):
        p = self.p
        self.logged(lambda: p["a"], "a")
        self.logged(lambda: "zz" in p, "zz")
        self.logged(lambda: p.get("b"), "b")
        self.logged(lambda: p.get("nope", 0), "nope")
        self.logged(lambda: p.setdefault("a", 0), "a")
        self.assertRaises(KeyError, self.logged, lambda: p["missing"], "missing")

    def test_unhashable_key_is_whole(self):
        reset()
        with self.assertRaises(TypeError):
            self.p[[1]]
        self.assertIn(self.i, bc._UNHASHABLE["test"])

    def test_whole(self):
        p = self.p
        for fn in (lambda: list(p), lambda: len(p), lambda: bool(p), lambda: p.keys(), lambda: p.values(),
                   lambda: p.items(), lambda: p.copy(), lambda: dict(p), lambda: {**p}, lambda: (lambda **k: k)(**fresh({'s': 1})[0]) and p.items(),
                   lambda: set(p), lambda: frozenset(p), lambda: tuple(p), lambda: sorted(p, key=str), lambda: p == {},
                   lambda: {} == p, lambda: p != {}, lambda: p | {}, lambda: {} | p, lambda: repr(p),
                   lambda: copy.copy(p), lambda: copy.deepcopy(p), lambda: pickle.dumps(p), lambda: reversed(p),
                   lambda: json.dumps({"k": 1} if False else fresh({"q": 1})[0]) and p.items(),
                   lambda: {}.update(p), lambda: set().update(p), lambda: dict.fromkeys(p), lambda: max(p, key=str),
                   lambda: "{a}".format_map(p) and p.items(), lambda: [*p], lambda: f"{p}"):
            with self.subTest(fn=fn):
                self.logged(fn, is_whole=True)

    def test_merge_slow_path(self):
        """dict(p), {**p}, f(**p), d.update(p) go through keys()/__getitem__ because tp_iter is overridden."""
        p = self.p
        reset()
        dict(p)
        self.assertTrue(whole(self.i))
        self.assertEqual(keys(self.i), {"a", "b", 3})  # __getitem__ per key, after keys()

    def test_mutation(self):
        p, _ = fresh({"a": 1})
        i = p._br_i
        for fn in (lambda: p.__setitem__("z", 1), lambda: p.update(y=2), lambda: p.pop("z"),
                   lambda: p.__delitem__("y"), lambda: p.setdefault("new", 0), lambda: p.clear()):
            reset()
            fn()
            self.assertTrue(mut(i))
        reset()
        p["k"] = 1
        p |= {"m": 2}
        self.assertTrue(mut(i))
        self.assertIs(type(p), bc.DictProxy)

    def test_silent_paths(self):
        """C-level reads that bypass the overrides (documented holes: covered by static whole sites)."""
        p = self.p
        for fn in (lambda: dict.get(p, "a"), lambda: dict.__getitem__(p, "a"), lambda: dict.keys(p),
                   lambda: dict.__contains__(p, "a"), lambda: dict.items(p), lambda: isinstance(p, dict),
                   lambda: p.__class__):
            with self.subTest(fn=fn):
                reset()
                fn()
                self.assertFalse(whole(self.i))
                self.assertFalse(keys(self.i))
        # A keys view taken once (logged) is then read silently.
        reset()
        view = p.keys()
        reset()
        "a" in view
        self.assertFalse(whole(self.i) or keys(self.i))

    def test_import_channel(self):
        reset()
        bc._set_channel("import")
        self.p["a"]
        bc._set_channel("test")
        self.assertFalse(keys(self.i))
        self.assertIn("a", bc._KEYS["import"][self.i])


class SetProxyTest(unittest.TestCase):
    def setUp(self):
        reset()

    def check_order(self, values, kind):
        o = kind(values)
        p, _ = fresh(o)
        self.assertEqual([id(x) for x in o], [id(x) for x in set.__iter__(p) if True]
                         if kind is set else [id(x) for x in frozenset.__iter__(p)])

    def test_iteration_order_preserved(self):
        for n in (0, 1, 4, 5, 6, 11, 20, 40, 100, 333):
            for kind in (set, frozenset):
                vals = [f"v{j}" for j in range(n)]
                with self.subTest(n=n, kind=kind):
                    o = kind(vals)
                    i = bc._register(o)
                    if i is not None:
                        p = bc._PROXIES[i]
                        self.assertEqual([id(x) for x in o], [id(x) for x in kind.__iter__(p)])
                    elif n <= 5:
                        self.fail(f"expected small {kind.__name__} (n={n}) to proxy cleanly")
                    lit = set()
                    for v in vals:
                        lit.add(v)
                    o2 = lit if kind is set else frozenset(lit)
                    i2 = bc._register(o2)
                    if i2 is not None:
                        p2 = bc._PROXIES[i2]
                        self.assertEqual([id(x) for x in o2], [id(x) for x in kind.__iter__(p2)])

    def test_behaviour(self):
        for kind in (set, frozenset):
            o = kind({"a", "b", 1})
            p, i = fresh(o)
            self.assertIsInstance(p, kind)
            self.assertEqual(p, o)
            self.assertEqual(repr(p), repr(o))
            self.assertEqual(repr(fresh(kind())[0]), repr(kind()))
            for clone in (copy.copy(p), copy.deepcopy(p), pickle.loads(pickle.dumps(p)), p.copy(), p | set(),
                          kind(p), p - set(), p & o):
                self.assertIs(type(clone), kind)
                self.assertEqual(clone, o)
            if kind is frozenset:
                self.assertEqual(hash(p), hash(o))
                self.assertEqual({o: 1}[p], 1)
                self.assertEqual({p: 1}[o], 1)

    def test_keyed_and_whole(self):
        for kind in (set, frozenset):
            p, i = fresh(kind({"a", "b"}))
            reset()
            "a" in p
            "q" not in p
            self.assertEqual(keys(i), {"a", "q"})
            self.assertFalse(whole(i))
            for fn in (lambda: list(p), lambda: len(p), lambda: bool(p), lambda: sorted(p), lambda: p | {"x"},
                       lambda: p & {"a"}, lambda: p - {"a"}, lambda: p ^ {"a"}, lambda: p.union({"z"}),
                       lambda: p.issubset({"a"}), lambda: p.isdisjoint(set()), lambda: p == set(), lambda: p <= set(),
                       lambda: kind({"a"}) | p, lambda: kind({"a"}) - p, lambda: kind({"a"}) & p, lambda: repr(p),
                       lambda: copy.deepcopy(p), lambda: pickle.dumps(p), lambda: tuple(p), lambda: [*p],
                       lambda: dict.fromkeys(p), lambda: kind({"a"}) == p):
                with self.subTest(kind=kind, fn=fn):
                    reset()
                    fn()
                    self.assertTrue(whole(i))
            if kind is frozenset:
                reset()
                hash(p)
                {p: 1}
                self.assertTrue(whole(i))

    @unittest.skipUnless(PY313, "C fast paths pinned to CPython 3.13")
    def test_silent_when_argument(self):
        """The proxy as the ARGUMENT of a C set routine is read directly from its table."""
        for kind in (set, frozenset):
            p, i = fresh(kind({"a", "b"}))
            for fn in (lambda: set(p), lambda: frozenset(p), lambda: set().update(p), lambda: set().union(p),
                       lambda: {"a"}.issubset(p), lambda: {"a"}.issuperset(p),
                       lambda: {"a"}.intersection(p), lambda: {"a"}.difference(p), lambda: set().__ior__(p),
                       lambda: {"a"}.symmetric_difference(p)):
                with self.subTest(kind=kind, fn=fn):
                    reset()
                    fn()
                    self.assertFalse(whole(i) or keys(i))
            # a plain operand of the other set type is tried first
            # a plain operand of the other set type is tried first
            other = frozenset({"a"}) if kind is set else {"a"}
            for fn in (lambda: other | p, lambda: other == p, lambda: other - p, lambda: other & p):
                reset()
                fn()
                self.assertFalse(whole(i))

    def test_mutation(self):
        p, i = fresh({"a", "b"})
        for fn in (lambda: p.add("c"), lambda: p.discard("c"), lambda: p.update({"d"}), lambda: p.remove("d"),
                   lambda: p.difference_update({"x"}), lambda: p.__ior__({"e"}), lambda: p.pop()):
            reset()
            fn()
            self.assertTrue(mut(i))
        reset()
        p.remove("a") if "a" in set(p) else p.add("a")
        self.assertTrue(mut(i))


class InstallTest(unittest.TestCase):
    def test_proxify_rebinds_every_alias_and_redirects(self):
        import types

        reset()
        m = types.ModuleType("sqlglot._br_fake")
        n = types.ModuleType("sqlglot._br_fake2")
        table = {"x": 1}
        fz = frozenset({"a"})

        class Base:
            T = table
            Z = fz

        Base.__module__ = "sqlglot._br_fake"

        class Sub(Base):
            pass

        Sub.__module__ = "sqlglot._br_fake"
        m.Base, m.Sub, m.TABLE = Base, Sub, table
        n.ALIAS = table
        holder = {"inner": table}
        n.HOLDER = holder
        lst = [table]
        n.LIST = lst
        tup = (fz,)
        n.TUP = tup

        def closure():
            return table

        sys.modules["sqlglot._br_fake"] = m
        sys.modules["sqlglot._br_fake2"] = n
        try:
            bc.proxify()
            p = Base.__dict__["T"]
            self.assertIsInstance(p, bc.DictProxy)
            self.assertIs(m.TABLE, p)
            self.assertIs(n.ALIAS, p)
            self.assertIs(Sub.T, p)
            self.assertIs(n.HOLDER["inner"], p)
            self.assertIs(lst[0], p)
            self.assertIs(closure(), p)
            cells = {cell for (cell, _), (_, _, i) in bc._CELLS.items() if i == p._br_i}
            self.assertEqual(cells, {("c", "sqlglot._br_fake:" + Base.__qualname__, "T"),
                                     ("g", "sqlglot._br_fake", "TABLE"), ("g", "sqlglot._br_fake2", "ALIAS")})
            zi = Base.__dict__["Z"]._br_i
            self.assertIn("tuple", bc._LEAKS.get(zi, ()))
            reset()
            Sub.T["x"]
            self.assertIn("x", keys(p._br_i))
            bc.check_bindings("t")
            self.assertFalse(bc._ALARMS)
            m.TABLE = {"x": 2}
            bc.check_bindings("t")
            self.assertTrue(any("no longer holds" in a for a in bc._ALARMS))
        finally:
            del sys.modules["sqlglot._br_fake"], sys.modules["sqlglot._br_fake2"]
            bc._ALARMS.clear()
            for slot in [s for s in bc._CELLS if s[0][1].startswith("sqlglot._br_fake")]:
                del bc._CELLS[slot]

    def test_dynamic_family(self):
        import types

        m = types.ModuleType("sqlglot._br_dyn")

        class K:
            pass

        K.__module__ = "sqlglot._br_dyn"
        self.assertEqual(bc._family(m), "m:sqlglot._br_dyn")
        self.assertEqual(bc._family(K), "c:sqlglot._br_dyn:" + K.__qualname__)
        self.assertEqual(bc._family(K()), "i:sqlglot._br_dyn:" + K.__qualname__)
        self.assertIsNone(bc._family(object()))


if __name__ == "__main__":
    unittest.main()
